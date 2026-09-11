#!/usr/bin/env python3
"""
ssg.py — a tiny, zero-dependency static site generator.

Uses only the Python standard library. No pip, no gems, no venvs, no
dependency hell. Copy this single file into a site directory and run it.

Site layout:
    config.yml          site configuration
    content/posts/      blog posts / articles (markdown, YAML front matter)
    content/pages/      standalone pages (markdown or html)
    content/*.html|md   root pages (index.html, 404.html, ...)
    templates/          html templates ({{ var }}, {% if %}, {% for %},
                        {% include %}, {% extends %}/{% block %})
    static/             copied verbatim into the output
    output/             the built site (never edit by hand)

Commands:
    ./ssg.py build [--drafts]     build the site into output/
    ./ssg.py serve [-p PORT]      build, serve locally, rebuild on change
                                  (preview: ads & analytics tags are omitted)
    ./ssg.py new "Post title"     create a new post skeleton
    ./ssg.py clean                remove the output directory
"""

import argparse
import datetime as dt
import html
import http.server
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from pathlib import Path

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def slugify(text):
    text = unicodedata.normalize('NFKD', str(text))
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s_]+', '-', text)


def xml_escape(text):
    return html.escape(str(text), quote=True)


def parse_date(value):
    """Accept datetime, date, or common string formats."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    s = str(value).strip()
    # tolerate trailing timezone info ("+0100", "+01:00", "Z", "UTC")
    s = re.sub(r'\s*(?:[+-]\d{2}:?\d{2}|Z|UTC)$', '', s)
    s = s.replace('T', ' ', 1) if re.match(r'^\d{4}-\d{2}-\d{2}T', s) else s
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
                '%Y/%m/%d %H:%M', '%Y/%m/%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError('unrecognised date: %r' % value)


# --------------------------------------------------------------------------
# Mini YAML (enough for config files and front matter)
# --------------------------------------------------------------------------

def _split_inline_list(s):
    parts, depth, buf, quote = [], 0, '', None
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            buf += ch
        elif ch == '[':
            depth += 1
            buf += ch
        elif ch == ']':
            depth -= 1
            buf += ch
        elif ch == ',' and depth == 0:
            parts.append(buf)
            buf = ''
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _yaml_scalar(tok):
    t = tok.strip()
    if not t:
        return ''
    if len(t) >= 2 and t[0] in '"\'' and t.endswith(t[0]):
        return t[1:-1]
    low = t.lower()
    if low in ('true', 'yes', 'on'):
        return True
    if low in ('false', 'no', 'off'):
        return False
    if low in ('null', 'none', '~'):
        return None
    if re.fullmatch(r'-?\d+', t):
        return int(t)
    if re.fullmatch(r'-?\d+\.\d+', t):
        return float(t)
    if t.startswith('[') and t.endswith(']'):
        inner = t[1:-1].strip()
        return [_yaml_scalar(p) for p in _split_inline_list(inner)] if inner else []
    return t


def parse_yaml(text):
    """Parse a practical subset of YAML: nested dicts, lists, scalars."""
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        lines.append((indent, raw.strip()))

    def parse_block(i, indent):
        # decide list vs dict from first line
        if i < len(lines) and lines[i][0] >= indent and lines[i][1].startswith('- '):
            return parse_list(i, lines[i][0])
        return parse_dict(i, indent)

    def parse_dict(i, indent):
        result = {}
        while i < len(lines):
            ind, line = lines[i]
            if ind < indent:
                break
            if line.startswith('- '):
                break
            m = re.match(r'^([^:]+):\s*(.*)$', line)
            if not m:
                i += 1
                continue
            key, rest = m.group(1).strip().strip('"\''), m.group(2)
            if rest == '' or rest == '|' or rest == '>':
                # nested block (or empty value)
                if i + 1 < len(lines) and lines[i + 1][0] > ind:
                    value, i = parse_block(i + 1, lines[i + 1][0])
                    result[key] = value
                else:
                    result[key] = ''
                    i += 1
            else:
                result[key] = _yaml_scalar(rest)
                i += 1
        return result, i

    def parse_list(i, indent):
        result = []
        while i < len(lines):
            ind, line = lines[i]
            if ind != indent or not line.startswith('- '):
                if ind < indent or not line.startswith('- '):
                    break
            item = line[2:].strip()
            if re.match(r'^[^:]+:\s*', item) and not item.startswith('http'):
                # list of dicts: "- key: value" plus following deeper lines
                sub = [(indent + 2, item)]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent and not lines[j][1].startswith('- '):
                    sub.append(lines[j])
                    j += 1
                saved = lines[i:j]
                # parse the collected fragment as its own dict
                value = _parse_fragment(sub)
                result.append(value)
                i = j
            else:
                result.append(_yaml_scalar(item))
                i += 1
        return result, i

    def _parse_fragment(fragment):
        nonlocal lines
        backup = lines
        try:
            lines = fragment
            value, _ = parse_dict(0, 0)
            return value
        finally:
            lines = backup

    value, _ = parse_block(0, 0)
    return value


def split_front_matter(text):
    """Return (front_matter_dict, body)."""
    if text.startswith('---'):
        m = re.match(r'^---\s*\n(.*?)\n---\s*\n?', text, re.S)
        if m:
            return parse_yaml(m.group(1)), text[m.end():]
    return {}, text


# --------------------------------------------------------------------------
# Markdown  (headings, emphasis, links, images, code, lists, quotes,
#            tables, hr, raw-HTML passthrough)
# --------------------------------------------------------------------------

_BLOCK_TAGS = {
    'address', 'article', 'aside', 'blockquote', 'canvas', 'dd', 'div', 'dl',
    'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2',
    'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'iframe', 'li', 'main', 'nav',
    'noscript', 'ol', 'p', 'pre', 'script', 'section', 'style', 'table',
    'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul', 'video', 'img',
    'span', 'a', 'em', 'strong', 'center', 'audio', 'source', 'object',
}


class Markdown:
    def convert(self, text, smart=False):
        self._stash = []
        self._smart = smart
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        return '\n'.join(self._blocks(lines)).strip()

    # -- block level --------------------------------------------------------

    def _blocks(self, lines):
        out = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            # fenced code block
            m = re.match(r'^\s*(```+|~~~+)\s*([\w+-]*)\s*$', line)
            if m:
                fence, lang = m.group(1), m.group(2)
                code, i = [], i + 1
                close_re = r'^\s*%s{3,}\s*$' % re.escape(fence[0])
                while i < n and not re.match(close_re, lines[i]):
                    code.append(lines[i])
                    i += 1
                i += 1  # skip closing fence
                cls = ' class="language-%s"' % lang if lang else ''
                out.append('<pre><code%s>%s</code></pre>' %
                           (cls, html.escape('\n'.join(code))))
                continue

            # raw HTML block
            m = re.match(r'^\s*<(/?)([a-zA-Z][a-zA-Z0-9-]*)', line)
            if (m and m.group(2).lower() in _BLOCK_TAGS) or stripped.startswith('<!--'):
                chunk = []
                while i < n and lines[i].strip():
                    chunk.append(lines[i])
                    i += 1
                out.append('\n'.join(chunk))
                continue

            # heading
            m = re.match(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$', line)
            if m:
                level = len(m.group(1))
                out.append('<h%d>%s</h%d>' % (level, self._inline(m.group(2)), level))
                i += 1
                continue

            # horizontal rule
            if re.match(r'^\s{0,3}([-*_])(\s*\1){2,}\s*$', line):
                out.append('<hr>')
                i += 1
                continue

            # blockquote
            if re.match(r'^\s{0,3}>', line):
                quote = []
                while i < n and (re.match(r'^\s{0,3}>', lines[i]) or
                                 (lines[i].strip() and quote and not self._is_block_start(lines[i]))):
                    quote.append(re.sub(r'^\s{0,3}>\s?', '', lines[i]))
                    i += 1
                out.append('<blockquote>\n%s\n</blockquote>' %
                           '\n'.join(self._blocks(quote)))
                continue

            # list
            m = re.match(r'^(\s*)([-*+]|\d+[.)])\s+', line)
            if m:
                block, i = self._collect_list(lines, i)
                out.append(block)
                continue

            # table
            if '|' in line and i + 1 < n and \
                    re.match(r'^\s*\|?[\s:|-]+\|?\s*$', lines[i + 1]) and '-' in lines[i + 1]:
                out_tbl, i = self._table(lines, i)
                out.append(out_tbl)
                continue

            # paragraph
            para = []
            while i < n and lines[i].strip() and not self._is_block_start(lines[i]):
                para.append(lines[i].strip())
                i += 1
            if para:
                out.append('<p>%s</p>' % self._inline('\n'.join(para)))
            else:
                i += 1
        return out

    def _is_block_start(self, line):
        if re.match(r'^\s*(```|~~~)', line):
            return True
        if re.match(r'^\s{0,3}#{1,6}\s', line):
            return True
        if re.match(r'^\s{0,3}>', line):
            return True
        if re.match(r'^(\s*)([-*+]|\d+[.)])\s+', line):
            return True
        if re.match(r'^\s{0,3}([-*_])(\s*\1){2,}\s*$', line):
            return True
        m = re.match(r'^\s*<(/?)([a-zA-Z][a-zA-Z0-9-]*)', line)
        if m and m.group(2).lower() in _BLOCK_TAGS:
            return True
        return False

    def _collect_list(self, lines, i):
        n = len(lines)
        item_re = re.compile(r'^(\s*)([-*+]|\d+[.)])\s+(.*)$')
        m = item_re.match(lines[i])
        base_indent = len(m.group(1))
        ordered = m.group(2)[0].isdigit()
        items, current = [], None
        while i < n:
            line = lines[i]
            m = item_re.match(line)
            if m and len(m.group(1)) <= base_indent:
                if m.group(2)[0].isdigit() != ordered and len(m.group(1)) == base_indent:
                    break  # list type switches
                if current is not None:
                    items.append(current)
                current = [m.group(3)]
                i += 1
            elif line.strip() and (len(line) - len(line.lstrip())) > base_indent:
                current.append(line.strip() if not item_re.match(line) else line)
                i += 1
            elif not line.strip():
                # blank: list continues only if next line is indented or a new item
                if i + 1 < n and (item_re.match(lines[i + 1]) or
                                  (lines[i + 1].strip() and
                                   len(lines[i + 1]) - len(lines[i + 1].lstrip()) > base_indent)):
                    current.append('')
                    i += 1
                else:
                    break
            else:
                break
        if current is not None:
            items.append(current)
        tag = 'ol' if ordered else 'ul'
        rendered = []
        for item in items:
            body = '\n'.join(item)
            if re.search(r'^(\s*)([-*+]|\d+[.)])\s+', body, re.M) and len(item) > 1:
                inner = '\n'.join(self._blocks(item))
                inner = re.sub(r'^<p>(.*?)</p>', r'\1', inner, count=1, flags=re.S)
                rendered.append('<li>%s</li>' % inner)
            else:
                rendered.append('<li>%s</li>' % self._inline(body.strip()))
        return '<%s>\n%s\n</%s>' % (tag, '\n'.join(rendered), tag), i

    def _table(self, lines, i):
        header = [c.strip() for c in lines[i].strip().strip('|').split('|')]
        aligns = []
        for c in lines[i + 1].strip().strip('|').split('|'):
            c = c.strip()
            if c.startswith(':') and c.endswith(':'):
                aligns.append(' style="text-align:center"')
            elif c.endswith(':'):
                aligns.append(' style="text-align:right"')
            else:
                aligns.append('')
        rows = []
        i += 2
        while i < len(lines) and '|' in lines[i] and lines[i].strip():
            rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
            i += 1
        out = ['<table>', '<thead>', '<tr>']
        for j, h in enumerate(header):
            out.append('<th%s>%s</th>' % (aligns[j] if j < len(aligns) else '', self._inline(h)))
        out += ['</tr>', '</thead>', '<tbody>']
        for row in rows:
            out.append('<tr>')
            for j, c in enumerate(row):
                out.append('<td%s>%s</td>' % (aligns[j] if j < len(aligns) else '', self._inline(c)))
            out.append('</tr>')
        out += ['</tbody>', '</table>']
        return '\n'.join(out), i

    # -- inline level -------------------------------------------------------

    def _stash_it(self, s):
        self._stash.append(s)
        return '\x02%d\x03' % (len(self._stash) - 1)

    def _inline(self, text):
        # code spans first — nothing inside them is processed
        text = re.sub(r'(`+)(.+?)\1',
                      lambda m: self._stash_it('<code>%s</code>' % html.escape(m.group(2).strip())),
                      text, flags=re.S)
        # autolinks
        text = re.sub(r'<(https?://[^>\s]+)>',
                      lambda m: self._stash_it('<a href="%s">%s</a>' % (m.group(1), m.group(1))),
                      text)
        # raw inline HTML tags — protect from emphasis mangling
        text = re.sub(r'</?[a-zA-Z][^>]*>', lambda m: self._stash_it(m.group(0)), text)
        # escape bare ampersands (existing entities are left alone)
        text = re.sub(r'&(?![a-zA-Z][a-zA-Z0-9]*;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', text)
        # images
        text = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)',
                      lambda m: self._stash_it('<img src="%s" alt="%s"%s>' %
                                               (m.group(2), html.escape(m.group(1)),
                                                ' title="%s"' % html.escape(m.group(3)) if m.group(3) else '')),
                      text)
        # links
        text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)',
                      lambda m: self._stash_it('<a href="%s"%s>' %
                                               (m.group(2),
                                                ' title="%s"' % html.escape(m.group(3)) if m.group(3) else ''))
                      + self._inline(m.group(1)) + self._stash_it('</a>'),
                      text)
        # emphasis
        text = re.sub(r'\*\*(?!\s)(.+?)(?<!\s)\*\*', r'<strong>\1</strong>', text, flags=re.S)
        text = re.sub(r'(?<![\w*])__(?!\s)(.+?)(?<!\s)__(?![\w*])', r'<strong>\1</strong>', text, flags=re.S)
        text = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'<em>\1</em>', text, flags=re.S)
        text = re.sub(r'(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])', r'<em>\1</em>', text, flags=re.S)
        text = re.sub(r'~~(?!\s)(.+?)(?<!\s)~~', r'<del>\1</del>', text, flags=re.S)
        if self._smart:
            # smart typography (code spans and tags are already stashed)
            text = re.sub(r"(?<=\w)'(?=\w)", '’', text)          # don't -> don’t
            text = re.sub(r"(?<![\w])'(?=\S)([^']+?)(?<=\S)'(?![\w])",
                          '‘\\1’', text)                    # 'x' -> ‘x’
            text = re.sub(r"(?<=\S)'", '’', text)            # nations' -> nations’
            text = re.sub(r"'(?=\S)", '‘', text)             # stray opening quote
            text = re.sub(r'"([^"\n]+)"', '“\\1”', text)     # "x" -> “x”
            text = text.replace('...', '…')
            text = re.sub(r'(?<!-)---(?!-)', '—', text)
            text = re.sub(r'(?<!-)--(?!-)', '–', text)
        # hard line breaks (two trailing spaces)
        text = re.sub(r'  \n', '<br>\n', text)
        # restore stashed fragments (may be nested)
        while re.search(r'\x02(\d+)\x03', text):
            text = re.sub(r'\x02(\d+)\x03', lambda m: self._stash[int(m.group(1))], text)
        return text


_md = Markdown()


def markdown_to_html(text, smart=False):
    return _md.convert(text, smart=smart)


def truncate_html_words(s, num, end_text='…'):
    """Truncate HTML to `num` words, closing open tags (Pelican/Django
    algorithm: a word is \\w[\\w-]*, entities and tags don't count).
    Returns (truncated_html, was_truncated)."""
    length = int(num)
    if length <= 0:
        return '', True
    html4_singlets = ('br', 'col', 'link', 'base', 'img', 'param', 'area',
                      'hr', 'input')
    re_words = re.compile(r'&.*?;|<.*?>|(\w[\w\'-]*)', re.U | re.S)
    re_tag = re.compile(r'<(/)?([^ >]+?)(?:(\s*/)| .*?)?>', re.S)
    pos, end_text_pos, words = 0, 0, 0
    open_tags = []
    while words <= length:
        m = re_words.search(s, pos)
        if not m:
            break
        pos = m.end(0)
        if m.group(1):
            words += 1
            if words == length:
                end_text_pos = pos
            continue
        tag = re_tag.match(m.group(0))
        if not tag or end_text_pos:
            continue
        closing_tag, tagname, self_closing = tag.groups()
        tagname = tagname.lower()
        if self_closing or tagname in html4_singlets:
            pass
        elif closing_tag:
            try:
                i = open_tags.index(tagname)
                open_tags = open_tags[i + 1:]
            except ValueError:
                pass
        else:
            open_tags.insert(0, tagname)
    if words <= length:
        return s, False
    out = s[:end_text_pos]
    if end_text:
        out += ' ' + end_text
    for tag in open_tags:
        out += '</%s>' % tag
    return out, True


# --------------------------------------------------------------------------
# Template engine  (Jinja-like: {{ expr|filter }}, {% if %}, {% for %},
#                   {% include %}, {% extends %} / {% block %})
# --------------------------------------------------------------------------

class TemplateError(Exception):
    pass


_TOKEN_RE = re.compile(r'({[{%#].*?[}%#]})', re.S)
_EXPR_TOKEN = re.compile(r'''
    (?P<ws>\s+)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<num>\d+\.\d+|\d+)
  | (?P<op>==|!=|<=|>=|<|>|\||\(|\)|\[|\]|,|\.)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
''', re.X)


def _tokenize_expr(src):
    tokens, pos = [], 0
    while pos < len(src):
        m = _EXPR_TOKEN.match(src, pos)
        if not m:
            raise TemplateError('bad expression: %r at %d' % (src, pos))
        pos = m.end()
        if m.lastgroup == 'ws':
            continue
        tokens.append((m.lastgroup, m.group(0)))
    return tokens


class ExprParser:
    def __init__(self, src):
        self.tokens = _tokenize_expr(src)
        self.pos = 0
        self.src = src

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expect(self, value):
        kind, val = self.next()
        if val != value:
            raise TemplateError('expected %r in %r' % (value, self.src))

    def parse(self):
        fn = self.parse_or()
        if self.pos != len(self.tokens):
            raise TemplateError('trailing tokens in %r' % self.src)
        return fn

    def parse_or(self):
        left = self.parse_and()
        while self.peek() == ('name', 'or'):
            self.next()
            right = self.parse_and()
            left = (lambda l, r: lambda ctx: l(ctx) or r(ctx))(left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.peek() == ('name', 'and'):
            self.next()
            right = self.parse_not()
            left = (lambda l, r: lambda ctx: l(ctx) and r(ctx))(left, right)
        return left

    def parse_not(self):
        if self.peek() == ('name', 'not'):
            self.next()
            inner = self.parse_not()
            return lambda ctx: not inner(ctx)
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_pipe()
        kind, val = self.peek()
        if val in ('==', '!=', '<', '>', '<=', '>='):
            self.next()
            right = self.parse_pipe()
            ops = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b,
                   '<': lambda a, b: (a or 0) < (b or 0), '>': lambda a, b: (a or 0) > (b or 0),
                   '<=': lambda a, b: (a or 0) <= (b or 0), '>=': lambda a, b: (a or 0) >= (b or 0)}
            op = ops[val]
            return (lambda l, r, o: lambda ctx: o(l(ctx), r(ctx)))(left, right, op)
        if self.peek() == ('name', 'in'):
            self.next()
            right = self.parse_pipe()
            return (lambda l, r: lambda ctx: l(ctx) in (r(ctx) or []))(left, right)
        if self.peek() == ('name', 'not') and self.pos + 1 < len(self.tokens) \
                and self.tokens[self.pos + 1] == ('name', 'in'):
            self.next()
            self.next()
            right = self.parse_pipe()
            return (lambda l, r: lambda ctx: l(ctx) not in (r(ctx) or []))(left, right)
        return left

    def parse_pipe(self):
        value = self.parse_atom()
        while self.peek() == ('op', '|'):
            self.next()
            kind, name = self.next()
            if kind != 'name':
                raise TemplateError('bad filter in %r' % self.src)
            args = []
            if self.peek() == ('op', '('):
                self.next()
                while self.peek() != ('op', ')'):
                    args.append(self.parse_or())
                    if self.peek() == ('op', ','):
                        self.next()
                self.expect(')')
            value = (lambda v, n, a: lambda ctx: apply_filter(n, v(ctx), [f(ctx) for f in a]))(value, name, args)
        return value

    def parse_atom(self):
        kind, val = self.peek()
        if kind == 'str':
            self.next()
            s = val[1:-1]
            return lambda ctx: s
        if kind == 'num':
            self.next()
            n = float(val) if '.' in val else int(val)
            return lambda ctx: n
        if val == '(':
            self.next()
            inner = self.parse_or()
            self.expect(')')
            base = inner
        elif kind == 'name':
            self.next()
            if val in ('true', 'True'):
                return lambda ctx: True
            if val in ('false', 'False'):
                return lambda ctx: False
            if val in ('none', 'None', 'null'):
                return lambda ctx: None
            name = val
            base = lambda ctx: ctx_lookup(ctx, name)
        else:
            raise TemplateError('unexpected %r in %r' % (val, self.src))
        # trailing .attr / [index] accessors
        while True:
            kind, val = self.peek()
            if val == '.':
                self.next()
                kind2, attr = self.next()
                base = (lambda b, a: lambda ctx: attr_get(b(ctx), a))(base, attr)
            elif val == '[':
                self.next()
                idx = self.parse_or()
                self.expect(']')
                base = (lambda b, ix: lambda ctx: attr_get(b(ctx), ix(ctx)))(base, idx)
            else:
                break
        return base


def ctx_lookup(scopes, name):
    for scope in reversed(scopes):
        if name in scope:
            return scope[name]
    return None


def attr_get(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    if isinstance(obj, (list, tuple)):
        try:
            return obj[int(key)]
        except (ValueError, IndexError, TypeError):
            return None
    return getattr(obj, str(key), None)


_FILTERS = {}


def filter_(name):
    def deco(fn):
        _FILTERS[name] = fn
        return fn
    return deco


def apply_filter(name, value, args):
    fn = _FILTERS.get(name)
    if fn is None:
        raise TemplateError('unknown filter %r' % name)
    return fn(value, *args)


@filter_('e')
@filter_('escape')
def _f_escape(v):
    return html.escape(str(v if v is not None else ''), quote=True)


@filter_('date')
def _f_date(v, fmt='%Y-%m-%d'):
    if v is None:
        return ''
    return parse_date(v).strftime(fmt)


@filter_('date_iso')
def _f_date_iso(v):
    if v is None:
        return ''
    d = parse_date(v)
    return d.strftime('%Y-%m-%dT%H:%M:%S+00:00')


@filter_('slugify')
def _f_slugify(v):
    return slugify(v)


@filter_('lower')
def _f_lower(v):
    return str(v or '').lower()


@filter_('upper')
def _f_upper(v):
    return str(v or '').upper()


@filter_('title')
def _f_title(v):
    return str(v or '').title()


@filter_('capitalize')
def _f_capitalize(v):
    s = str(v or '')
    return s[:1].upper() + s[1:]


@filter_('length')
@filter_('count')
def _f_length(v):
    return len(v) if v is not None else 0


@filter_('join')
def _f_join(v, sep=', ', attr=None):
    items = v or []
    if attr:
        items = [attr_get(x, attr) for x in items]
    return sep.join(str(x) for x in items)


@filter_('default')
def _f_default(v, d=''):
    return v if v not in (None, '', [], {}) else d


@filter_('striptags')
def _f_striptags(v):
    return re.sub(r'<[^>]+>', '', str(v or ''))


@filter_('truncate')
def _f_truncate(v, n=140, suffix='…'):
    s = str(v or '')
    return s if len(s) <= n else s[:n].rsplit(' ', 1)[0] + suffix


@filter_('truncatewords')
def _f_truncatewords(v, n=25, suffix='…'):
    words = str(v or '').split()
    return ' '.join(words) if len(words) <= n else ' '.join(words[:n]) + suffix


@filter_('replace')
def _f_replace(v, old, new):
    return str(v or '').replace(str(old), str(new))


@filter_('json')
def _f_json(v):
    return json.dumps(v, ensure_ascii=False)


@filter_('first')
def _f_first(v):
    return (v or [None])[0]


@filter_('last')
def _f_last(v):
    return (v or [None])[-1]


@filter_('sort')
def _f_sort(v, attr=None, reverse=False):
    items = list(v or [])
    if attr:
        items.sort(key=lambda x: (attr_get(x, attr) is None, attr_get(x, attr)), reverse=bool(reverse))
    else:
        items.sort(reverse=bool(reverse))
    return items


@filter_('strip')
def _f_strip(v):
    return str(v or '').strip()


@filter_('absolute')
def _f_absolute(v, base=''):
    s = str(v or '')
    if s.startswith('http://') or s.startswith('https://'):
        return s
    return str(base).rstrip('/') + '/' + s.lstrip('/')


# -- template nodes ----------------------------------------------------------

class _Text:
    def __init__(self, s):
        self.s = s

    def render(self, ctx, out, engine, blocks):
        out.append(self.s)


class _Var:
    def __init__(self, expr):
        self.fn = ExprParser(expr).parse()

    def render(self, ctx, out, engine, blocks):
        v = self.fn(ctx)
        out.append('' if v is None else str(v))


class _If:
    def __init__(self):
        self.branches = []   # list of (cond_fn or None, nodes)

    def render(self, ctx, out, engine, blocks):
        for cond, nodes in self.branches:
            if cond is None or cond(ctx):
                for node in nodes:
                    node.render(ctx, out, engine, blocks)
                return


class _For:
    def __init__(self, var, expr, nodes, else_nodes):
        self.var, self.nodes, self.else_nodes = var, nodes, else_nodes
        self.fn = ExprParser(expr).parse()

    def render(self, ctx, out, engine, blocks):
        items = self.fn(ctx)
        if isinstance(items, dict):
            items = list(items.items())
        items = list(items) if items else []
        if not items:
            for node in self.else_nodes:
                node.render(ctx, out, engine, blocks)
            return
        n = len(items)
        for i, item in enumerate(items):
            scope = {self.var: item,
                     'loop': {'index': i + 1, 'index0': i, 'first': i == 0,
                              'last': i == n - 1, 'length': n,
                              'prev': items[i - 1] if i > 0 else None,
                              'next': items[i + 1] if i + 1 < n else None}}
            if ',' in self.var:  # for a, b in pairs
                names = [x.strip() for x in self.var.split(',')]
                scope = {'loop': scope['loop']}
                for name, val in zip(names, item):
                    scope[name] = val
            ctx.append(scope)
            for node in self.nodes:
                node.render(ctx, out, engine, blocks)
            ctx.pop()


class _Include:
    def __init__(self, name):
        self.name = name.strip().strip('"\'')

    def render(self, ctx, out, engine, blocks):
        tpl = engine.get(self.name)
        for node in tpl.nodes:
            node.render(ctx, out, engine, blocks)


class _Block:
    def __init__(self, name, nodes):
        self.name, self.nodes = name, nodes

    def render(self, ctx, out, engine, blocks):
        nodes = blocks.get(self.name, self.nodes)
        for node in nodes:
            node.render(ctx, out, engine, blocks)


class Template:
    def __init__(self, source, name='<template>'):
        self.name = name
        self.extends = None
        self.blocks = {}
        self.nodes = self._parse(self._lex(source))
        self._collect_blocks(self.nodes)

    def _lex(self, source):
        parts = _TOKEN_RE.split(source)
        tokens = []
        for part in parts:
            if not part:
                continue
            if part.startswith('{#'):
                continue
            if part.startswith('{{'):
                tokens.append(('var', part[2:-2].strip().strip('-').strip()))
            elif part.startswith('{%'):
                inner = part[2:-2].strip()
                if inner.startswith('-'):
                    inner = inner[1:].strip()
                    if tokens and tokens[-1][0] == 'text':
                        tokens[-1] = ('text', tokens[-1][1].rstrip())
                trim_after = inner.endswith('-')
                if trim_after:
                    inner = inner[:-1].strip()
                tokens.append(('tag', inner, trim_after))
            else:
                tokens.append(('text', part))
        return tokens

    def _parse(self, tokens):
        self._tokens = tokens
        self._pos = 0
        return self._parse_nodes(end=None)

    def _parse_nodes(self, end):
        nodes = []
        while self._pos < len(self._tokens):
            tok = self._tokens[self._pos]
            if tok[0] == 'text':
                nodes.append(_Text(tok[1]))
                self._pos += 1
            elif tok[0] == 'var':
                nodes.append(_Var(tok[1]))
                self._pos += 1
            else:
                tag = tok[1]
                word = tag.split(None, 1)[0]
                if end and word in end:
                    return nodes
                self._pos += 1
                rest = tag[len(word):].strip()
                if word == 'if':
                    node = _If()
                    cond = ExprParser(rest).parse()
                    while True:
                        body = self._parse_nodes(end=('elif', 'else', 'endif'))
                        node.branches.append((cond, body))
                        closing = self._tokens[self._pos][1]
                        self._pos += 1
                        cword = closing.split(None, 1)[0]
                        if cword == 'elif':
                            cond = ExprParser(closing[4:].strip()).parse()
                        elif cword == 'else':
                            body = self._parse_nodes(end=('endif',))
                            node.branches.append((None, body))
                            self._pos += 1
                            break
                        else:
                            break
                    nodes.append(node)
                elif word == 'for':
                    m = re.match(r'^(.+?)\s+in\s+(.+)$', rest)
                    if not m:
                        raise TemplateError('bad for tag: %r' % tag)
                    body = self._parse_nodes(end=('endfor', 'else'))
                    else_nodes = []
                    closing = self._tokens[self._pos][1].split(None, 1)[0]
                    self._pos += 1
                    if closing == 'else':
                        else_nodes = self._parse_nodes(end=('endfor',))
                        self._pos += 1
                    nodes.append(_For(m.group(1).strip(), m.group(2).strip(), body, else_nodes))
                elif word == 'include':
                    nodes.append(_Include(rest))
                elif word == 'extends':
                    self.extends = rest.strip().strip('"\'')
                elif word == 'block':
                    name = rest.strip()
                    body = self._parse_nodes(end=('endblock',))
                    self._pos += 1
                    nodes.append(_Block(name, body))
                elif word in ('endblock', 'endif', 'endfor'):
                    raise TemplateError('unexpected %r in %s' % (word, self.name))
                else:
                    raise TemplateError('unknown tag %r in %s' % (word, self.name))
        if end:
            raise TemplateError('missing %s in %s' % (end, self.name))
        return nodes

    def _collect_blocks(self, nodes):
        for node in nodes:
            if isinstance(node, _Block):
                self.blocks[node.name] = node.nodes
                self._collect_blocks(node.nodes)
            elif isinstance(node, _If):
                for _, body in node.branches:
                    self._collect_blocks(body)
            elif isinstance(node, _For):
                self._collect_blocks(node.nodes)


class Engine:
    def __init__(self, template_dir):
        self.dir = Path(template_dir)
        self.cache = {}

    def get(self, name):
        if name not in self.cache:
            path = self.dir / name
            if not path.exists():
                raise TemplateError('template not found: %s' % name)
            self.cache[name] = Template(path.read_text(encoding='utf-8'), name)
        return self.cache[name]

    def has(self, name):
        return (self.dir / name).exists()

    def render(self, name, context):
        tpl = self.get(name)
        # resolve inheritance chain
        chain = [tpl]
        while chain[-1].extends:
            chain.append(self.get(chain[-1].extends))
        blocks = {}
        for t in chain:  # child first: its blocks win
            for bname, bnodes in t.blocks.items():
                blocks.setdefault(bname, bnodes)
        root = chain[-1]
        out = []
        ctx = [context] if isinstance(context, dict) else context
        for node in root.nodes:
            node.render(ctx, out, self, blocks)
        return ''.join(out)

    def render_string(self, source, context):
        tpl = Template(source)
        out = []
        ctx = [context] if isinstance(context, dict) else context
        for node in tpl.nodes:
            node.render(ctx, out, self, tpl.blocks)
        return ''.join(out)


# --------------------------------------------------------------------------
# Site builder
# --------------------------------------------------------------------------

DEFAULTS = {
    'title': 'My Site',
    'url': 'http://localhost:8000',
    'description': '',
    'author': '',
    'post_url': '/{year}/{month}/{day}/{slug}/',
    'page_url': '/{slug}/',
    'post_template': 'post.html',
    'page_template': 'page.html',
    'paginate': 0,
    'paginate_first': '/',
    'paginate_path': '/page/{num}/',
    'feed_limit': 20,
    'sitemap': False,
}


class Site:
    def __init__(self, root, drafts=False, preview=False, preview_url=None):
        self.root = Path(root)
        cfg_path = self.root / 'config.yml'
        if not cfg_path.exists():
            sys.exit('error: no config.yml in %s' % self.root)
        self.cfg = dict(DEFAULTS)
        self.cfg.update(parse_yaml(cfg_path.read_text(encoding='utf-8')) or {})
        # Preview builds (./ssg.py serve) never emit ad or analytics tags, so
        # editing/previewing locally can't trigger real ad impressions or
        # tracking hits. Production builds (./ssg.py build) honour config.yml.
        if preview:
            self.cfg['ads'] = False
            self.cfg['analytics'] = False
            # Templates build every internal link as {{ site.url }}{{ ... }}. In
            # production site.url is the real domain, but locally that points all
            # links (and undeployed posts especially) at the live server → 404.
            # Repoint site.url at the local server so preview navigation stays local.
            if preview_url is not None:
                self.cfg['url'] = preview_url.rstrip('/')
        self.out = self.root / 'output'
        self.engine = Engine(self.root / 'templates')
        self.drafts = drafts
        self.posts = []
        self.pages = []
        self.written = 0

    # -- loading ------------------------------------------------------------

    def load(self):
        posts_dir = self.root / 'content' / 'posts'
        if posts_dir.exists():
            for path in sorted(posts_dir.rglob('*')):
                if path.suffix.lower() in ('.md', '.markdown', '.html'):
                    post = self._load_post(path)
                    if post:
                        self.posts.append(post)
        self.posts.sort(key=lambda p: p['date'], reverse=True)
        for i, post in enumerate(self.posts):
            post['newer'] = self.posts[i - 1] if i > 0 else None
            post['older'] = self.posts[i + 1] if i + 1 < len(self.posts) else None

        pages_dir = self.root / 'content' / 'pages'
        if pages_dir.exists():
            for path in sorted(pages_dir.rglob('*')):
                if path.suffix.lower() in ('.md', '.markdown', '.html'):
                    self.pages.append(self._load_page(path))

        self.root_pages = []
        content_dir = self.root / 'content'
        if content_dir.exists():
            for path in sorted(content_dir.glob('*')):
                if path.is_file() and path.suffix.lower() in ('.md', '.markdown', '.html', '.xml', '.txt', '.json'):
                    self.root_pages.append(self._load_root_page(path))

        self._build_taxonomies()

    def _read(self, path):
        fm, body = split_front_matter(path.read_text(encoding='utf-8'))
        return fm, body

    def _render_body(self, path, body):
        if path.suffix.lower() in ('.md', '.markdown'):
            return markdown_to_html(body, smart=bool(self.cfg.get('smart_quotes')))
        return body

    def _load_post(self, path):
        fm, body = self._read(path)
        if not self.drafts and (fm.get('draft') or str(fm.get('status', '')).lower() == 'draft'):
            return None
        name = path.stem
        m = re.match(r'^(\d{4}-\d{2}-\d{2})-(.*)$', name)
        date = fm.get('date') or (m.group(1) if m else None)
        if date is None:
            sys.exit('error: post %s has no date' % path)
        date = parse_date(date)
        slug = fm.get('slug') or slugify(m.group(2) if m else name)
        url = fm.get('permalink') or self.cfg['post_url'].format(
            year=date.strftime('%Y'), month=date.strftime('%m'),
            day=date.strftime('%d'), month_abbr=date.strftime('%b'),
            month_name=date.strftime('%B'), slug=slug)
        content = self._render_body(path, body)
        if fm.get('summary'):
            summary, truncated = fm['summary'], True
        else:
            summary, truncated = truncate_html_words(
                content, int(self.cfg.get('summary_words', 50)))
        post = dict(fm)
        post.update({
            'title': fm.get('title', slug.replace('-', ' ').title()),
            'date': date,
            'slug': slug,
            'url': url,
            'content': content,
            'description': fm.get('description') or fm.get('summary') or '',
            'summary': summary,
            'truncated': truncated,
            'tags': [{'name': t, 'slug': slugify(t),
                      'url': self.cfg['tag_url'].format(slug=slugify(t), name=t, num='')
                             if self.cfg.get('tag_url') else None}
                     for t in dict.fromkeys(_as_list(fm.get('tags')))],
            'category': ({'name': fm['category'], 'slug': slugify(fm['category']),
                          'url': self.cfg['category_url'].format(slug=slugify(fm['category']), name=fm['category'], num='')
                                 if self.cfg.get('category_url') else None}
                         if fm.get('category') else None),
            'author': fm.get('author') or self.cfg.get('author', ''),
            'template': fm.get('template') or self.cfg['post_template'],
            'is_post': True,
        })
        return post

    def _load_page(self, path):
        fm, body = self._read(path)
        slug = fm.get('slug') or slugify(path.stem)
        url = fm.get('permalink') or self.cfg['page_url'].format(slug=slug)
        page = dict(fm)
        page.update({
            'title': fm.get('title', slug.replace('-', ' ').title()),
            'slug': slug,
            'url': url,
            'content': self._render_body(path, body),
            'template': fm.get('template') or self.cfg['page_template'],
            'is_post': False,
        })
        return page

    def _load_root_page(self, path):
        fm, body = self._read(path)
        url = fm.get('permalink') or '/' + path.name
        page = dict(fm)
        page.update({
            'title': fm.get('title', ''),
            'url': url,
            'source_suffix': path.suffix.lower(),
            'raw_body': body,
            'template': fm.get('template'),   # optional wrapper layout
            'paginate': fm.get('paginate', False),
            'is_post': False,
        })
        return page

    def _build_taxonomies(self):
        tags, cats, authors, years = {}, {}, {}, {}
        # key tags by slug (merges "Tag"/"tag") or by raw name (keeps them apart)
        by_name = self.cfg.get('taxonomy_key') == 'name'
        for post in self.posts:
            for t in post['tags']:
                key = t['name'] if by_name else t['slug']
                entry = tags.setdefault(key, {'name': t['name'], 'slug': t['slug'], 'posts': []})
                entry['posts'].append(post)
            if post['category']:
                c = post['category']
                entry = cats.setdefault(c['slug'], {'name': c['name'], 'slug': c['slug'], 'posts': []})
                entry['posts'].append(post)
            if post['author']:
                a = str(post['author'])
                entry = authors.setdefault(slugify(a), {'name': a, 'slug': slugify(a), 'posts': []})
                entry['posts'].append(post)
            y = post['date'].strftime('%Y')
            mon = post['date'].strftime('%b')
            yentry = years.setdefault(y, {'year': y, 'posts': [], 'months': {}})
            yentry['posts'].append(post)
            mentry = yentry['months'].setdefault(mon, {'name': mon, 'month_num': post['date'].strftime('%m'), 'posts': []})
            mentry['posts'].append(post)

        def finish(d, url_pattern):
            items = []
            for slug in sorted(d):
                entry = d[slug]
                entry['count'] = len(entry['posts'])
                if url_pattern:
                    entry['url'] = url_pattern.format(slug=entry['slug'], name=entry['name'], num='')
                items.append(entry)
            return items

        self.tags = finish(tags, self.cfg.get('tag_url'))
        self.categories = finish(cats, self.cfg.get('category_url'))
        self.authors = finish(authors, self.cfg.get('author_url'))
        self.years = []
        for y in sorted(years, reverse=True):
            entry = years[y]
            if self.cfg.get('year_archive_url'):
                entry['url'] = self.cfg['year_archive_url'].format(year=y)
            months = []
            for mon, mentry in entry['months'].items():
                if self.cfg.get('month_archive_url'):
                    mentry['url'] = self.cfg['month_archive_url'].format(year=y, month_abbr=mon, month=mentry['month_num'])
                months.append(mentry)
            months.sort(key=lambda m: m['month_num'], reverse=True)
            entry['months'] = months
            self.years.append(entry)

    # -- writing ------------------------------------------------------------

    def _outpath(self, url):
        path = url.split('#')[0].split('?')[0]
        if path.endswith('/'):
            path += 'index.html'
        out_path = (self.out / path.lstrip('/')).resolve()
        out_root = self.out.resolve()
        # A `permalink:` in front matter (or a page_url/post_url pattern) that
        # contains `..` would otherwise let a write land outside output/ —
        # e.g. permalink: ../../../.bashrc. Fail the build loudly rather than
        # silently writing outside the site.
        if out_root != out_path and out_root not in out_path.parents:
            raise SystemExit('error: url %r escapes the output directory' % url)
        return out_path

    def write(self, url, content):
        path = self._outpath(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        self.written += 1

    def base_context(self):
        site = dict(self.cfg)
        site.update({
            'posts': self.posts,
            'pages': self.pages,
            'tags': self.tags,
            'categories': self.categories,
            'authors': self.authors,
            'years': self.years,
            'time': dt.datetime.now(),
        })
        return {'site': site}

    def render_page(self, template, extra):
        ctx = self.base_context()
        ctx.update(extra)
        return self.engine.render(template, ctx)

    # -- pagination ---------------------------------------------------------

    def paginate_urls(self, first, pattern, total):
        urls = []
        for num in range(1, total + 1):
            if num == 1:
                urls.append(first)
            else:
                urls.append(pattern.format(num=num))
        return urls

    def make_paginator(self, posts, urls, page_num):
        total = len(urls)
        return {
            'posts': posts,
            'page': page_num,
            'total_pages': total,
            'previous_page': page_num - 1 if page_num > 1 else None,
            'next_page': page_num + 1 if page_num < total else None,
            'previous_page_path': urls[page_num - 2] if page_num > 1 else None,
            'next_page_path': urls[page_num] if page_num < total else None,
            'first_page_path': urls[0],
            'page_list': [{'num': i + 1, 'url': u, 'current': (i + 1) == page_num}
                          for i, u in enumerate(urls)],
        }

    def render_listing(self, items, per_page, first, pattern, template, extra):
        """Render a paginated listing (index, category, tag, author)."""
        per_page = per_page or len(items) or 1
        chunks = [items[i:i + per_page] for i in range(0, len(items), per_page)] or [[]]
        urls = self.paginate_urls(first, pattern or first, len(chunks))
        for num, chunk in enumerate(chunks, 1):
            paginator = self.make_paginator(chunk, urls, num)
            ctx = dict(extra)
            ctx['paginator'] = paginator
            ctx['articles'] = chunk
            out = self.render_page(template, ctx)
            self.write(urls[num - 1], out)

    # -- build --------------------------------------------------------------

    def build(self):
        start = time.time()
        self.load()
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)

        # posts
        for post in self.posts:
            html_out = self.render_page(post['template'], {'page': post, 'content': post['content']})
            self.write(post['url'], html_out)

        # pages
        for page in self.pages:
            html_out = self.render_page(page['template'], {'page': page, 'content': page['content']})
            self.write(page['url'], html_out)

        # root content files (index, 404, ...) — rendered as templates
        for page in self.root_pages:
            if page['paginate']:
                per = int(self.cfg.get('paginate') or 0)
                tpl_src = page['raw_body']
                per_page = per or len(self.posts) or 1
                chunks = [self.posts[i:i + per_page] for i in range(0, len(self.posts), per_page)] or [[]]
                urls = self.paginate_urls(self.cfg['paginate_first'], self.cfg['paginate_path'], len(chunks))
                for num, chunk in enumerate(chunks, 1):
                    paginator = self.make_paginator(chunk, urls, num)
                    ctx = self.base_context()
                    ctx.update({'page': page, 'paginator': paginator, 'articles': chunk})
                    body = self.engine.render_string(tpl_src, ctx)
                    if page['template']:
                        ctx['content'] = body
                        body = self.engine.render(page['template'], ctx)
                    self.write(urls[num - 1], body)
            else:
                ctx = self.base_context()
                ctx.update({'page': page})
                if page['source_suffix'] in ('.md', '.markdown'):
                    body = markdown_to_html(self.engine.render_string(page['raw_body'], ctx),
                                            smart=bool(self.cfg.get('smart_quotes')))
                else:
                    body = self.engine.render_string(page['raw_body'], ctx)
                if page['template']:
                    ctx['content'] = body
                    body = self.engine.render(page['template'], ctx)
                self.write(page['url'], body)

        # tag pages
        if self.cfg.get('tag_url') and self.engine.has('tag.html'):
            per = int(self.cfg.get('tag_paginate') or self.cfg.get('paginate') or 0)
            for tag in self.tags:
                first = self.cfg['tag_url'].format(slug=tag['slug'], name=tag['name'], num='')
                pattern = self.cfg.get('tag_url_paged', '').format(slug=tag['slug'], name=tag['name'], num='{num}') \
                    if self.cfg.get('tag_url_paged') else first
                self.render_listing(tag['posts'], per if self.cfg.get('tag_url_paged') else 0,
                                    first, pattern, 'tag.html', {'tag': tag, 'page': tag})

        # category pages
        if self.cfg.get('category_url') and self.engine.has('category.html'):
            per = int(self.cfg.get('category_paginate') or self.cfg.get('paginate') or 0)
            for cat in self.categories:
                first = self.cfg['category_url'].format(slug=cat['slug'], num='')
                pattern = self.cfg.get('category_url_paged', '').format(slug=cat['slug'], num='{num}') \
                    if self.cfg.get('category_url_paged') else first
                self.render_listing(cat['posts'], per if self.cfg.get('category_url_paged') else 0,
                                    first, pattern, 'category.html', {'category': cat, 'page': cat})

        # author pages
        if self.cfg.get('author_url') and self.engine.has('author.html'):
            per = int(self.cfg.get('author_paginate') or self.cfg.get('paginate') or 0)
            for author in self.authors:
                first = self.cfg['author_url'].format(slug=author['slug'], num='')
                pattern = self.cfg.get('author_url_paged', '').format(slug=author['slug'], num='{num}') \
                    if self.cfg.get('author_url_paged') else first
                self.render_listing(author['posts'], per if self.cfg.get('author_url_paged') else 0,
                                    first, pattern, 'author.html', {'author': author, 'page': author})

        # period archives (year / month)
        if self.cfg.get('year_archive_url') and self.engine.has('period_archives.html'):
            for year in self.years:
                url = self.cfg['year_archive_url'].format(year=year['year'])
                out = self.render_page('period_archives.html',
                                       {'period': [year['year']], 'articles': year['posts'],
                                        'page': {'title': year['year'], 'url': url}})
                self.write(url, out)
                if self.cfg.get('month_archive_url'):
                    for month in year['months']:
                        murl = self.cfg['month_archive_url'].format(
                            year=year['year'], month_abbr=month['name'], month=month['month_num'])
                        out = self.render_page('period_archives.html',
                                               {'period': [year['year'], month['name']],
                                                'articles': month['posts'],
                                                'page': {'title': '%s %s' % (month['name'], year['year']), 'url': murl}})
                        self.write(murl, out)

        # feeds / sitemap / search
        if self.cfg.get('atom_feed'):
            self.write(self.cfg['atom_feed'], self.atom_feed())
        if self.cfg.get('json_feed'):
            self.write(self.cfg['json_feed'], self.json_feed())
        if self.cfg.get('search_index'):
            self.write(self.cfg['search_index'], self.search_index())
        if self.cfg.get('sitemap'):
            self.write('/sitemap.xml', self.sitemap())

        # static files
        static_dir = self.root / 'static'
        if static_dir.exists():
            shutil.copytree(static_dir, self.out, dirs_exist_ok=True)

        elapsed = time.time() - start
        print('built %d pages (%d posts, %d pages) in %.2fs -> %s' %
              (self.written, len(self.posts), len(self.pages), elapsed, self.out))

    # -- generated documents --------------------------------------------------

    def _abs(self, url):
        return self.cfg['url'].rstrip('/') + url

    def atom_feed(self):
        posts = self.posts[:int(self.cfg['feed_limit'])]
        updated = posts[0]['date'] if posts else dt.datetime.now()
        e = xml_escape
        out = ['<?xml version="1.0" encoding="utf-8"?>',
               '<feed xmlns="http://www.w3.org/2005/Atom">',
               '  <title>%s</title>' % e(self.cfg['title']),
               '  <link href="%s" rel="alternate"/>' % e(self._abs('/')),
               '  <link href="%s" rel="self"/>' % e(self._abs(self.cfg['atom_feed'])),
               '  <id>%s</id>' % e(self._abs('/')),
               '  <updated>%s</updated>' % _f_date_iso(updated)]
        if self.cfg.get('author'):
            out.append('  <author><name>%s</name></author>' % e(self.cfg['author']))
        for post in posts:
            out += ['  <entry>',
                    '    <title>%s</title>' % e(post['title']),
                    '    <link href="%s" rel="alternate"/>' % e(self._abs(post['url'])),
                    '    <id>%s</id>' % e(self._abs(post['url'])),
                    '    <published>%s</published>' % _f_date_iso(post['date']),
                    '    <updated>%s</updated>' % _f_date_iso(post['date']),
                    '    <author><name>%s</name></author>' % e(post['author'] or self.cfg['author']),
                    '    <content type="html">%s</content>' % e(post['content']),
                    '  </entry>']
        out.append('</feed>')
        return '\n'.join(out)

    def json_feed(self):
        posts = self.posts[:int(self.cfg['feed_limit'])]
        feed = {
            'version': 'https://jsonfeed.org/version/1.1',
            'title': self.cfg['title'],
            'home_page_url': self._abs('/'),
            'feed_url': self._abs(self.cfg['json_feed']),
            'description': self.cfg.get('description', ''),
            'items': [{
                'id': self._abs(p['url']),
                'url': self._abs(p['url']),
                'title': p['title'],
                'date_published': _f_date_iso(p['date']),
                'content_html': p['content'],
                'tags': [t['name'] for t in p['tags']],
            } for p in posts],
        }
        return json.dumps(feed, ensure_ascii=False, indent=2)

    def search_index(self):
        items = [{
            'title': p['title'],
            'url': p['url'],
            'category': p['category']['name'] if p['category'] else '',
            'tags': ', '.join(t['name'] for t in p['tags']),
            'date': p['date'].strftime('%Y-%m-%d %H:%M:%S'),
            'description': _f_striptags(p.get('description', '')),
        } for p in self.posts]
        return json.dumps(items, ensure_ascii=False, indent=2)

    def sitemap(self):
        e = xml_escape
        out = ['<?xml version="1.0" encoding="utf-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        seen = set()
        entries = [('/', None)]
        for p in self.posts:
            entries.append((p['url'], p['date']))
        for p in self.pages:
            entries.append((p['url'], None))
        for url, lastmod in entries:
            if url in seen:
                continue
            seen.add(url)
            out.append('  <url>')
            out.append('    <loc>%s</loc>' % e(self._abs(url)))
            if lastmod:
                out.append('    <lastmod>%s</lastmod>' % lastmod.strftime('%Y-%m-%d'))
            out.append('  </url>')
        out.append('</urlset>')
        return '\n'.join(out)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [t.strip() for t in str(value).split(',') if t.strip()]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_build(args):
    Site(args.dir, drafts=args.drafts).build()


def cmd_clean(args):
    out = Path(args.dir) / 'output'
    if out.exists():
        shutil.rmtree(out)
        print('removed', out)


def cmd_new(args):
    root = Path(args.dir)
    today = dt.date.today()
    slug = slugify(args.title)
    target_dir = root / 'content' / ('pages' if args.page else 'posts')
    target_dir.mkdir(parents=True, exist_ok=True)
    if args.page:
        path = target_dir / ('%s.md' % slug)
        body = '---\ntitle: %s\n---\n\nWrite here.\n' % args.title
    else:
        path = target_dir / ('%s-%s.md' % (today.isoformat(), slug))
        body = ('---\ntitle: %s\ndate: %s 10:00\ntags: []\n'
                'description: ""\n---\n\nWrite here.\n' % (args.title, today.isoformat()))
    if path.exists():
        sys.exit('error: %s already exists' % path)
    path.write_text(body, encoding='utf-8')
    print('created', path)


def cmd_serve(args):
    root = Path(args.dir)

    preview_url = 'http://127.0.0.1:%d' % args.port

    def rebuild():
        try:
            Site(root, drafts=args.drafts, preview=True,
                 preview_url=preview_url).build()
        except Exception as exc:      # keep serving on build errors
            print('build error:', exc)

    rebuild()

    watched = ['config.yml', 'content', 'templates', 'static', 'ssg.py']

    def snapshot():
        state = {}
        for name in watched:
            p = root / name
            if p.is_file():
                state[str(p)] = p.stat().st_mtime
            elif p.is_dir():
                for f in p.rglob('*'):
                    if f.is_file():
                        state[str(f)] = f.stat().st_mtime
        return state

    def watch():
        last = snapshot()
        while True:
            time.sleep(0.7)
            now = snapshot()
            if now != last:
                last = now
                print('change detected, rebuilding...')
                rebuild()

    threading.Thread(target=watch, daemon=True).start()

    outdir = root / 'output'

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(outdir), **kw)

        def log_message(self, fmt, *a):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('serving %s at http://127.0.0.1:%d/ (ctrl-c to stop)' % (outdir, args.port))
    print('preview mode: ads & analytics are disabled')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


def main():
    parser = argparse.ArgumentParser(description='tiny zero-dependency static site generator')
    parser.add_argument('-C', '--dir', default='.', help='site directory (default: .)')
    sub = parser.add_subparsers(dest='command')

    p_build = sub.add_parser('build', help='build the site')
    p_build.add_argument('--drafts', action='store_true', help='include drafts')

    sub.add_parser('clean', help='remove the output directory')

    p_new = sub.add_parser('new', help='create a new post')
    p_new.add_argument('title')
    p_new.add_argument('--page', action='store_true', help='create a page instead')

    p_serve = sub.add_parser('serve', help='serve locally and rebuild on change')
    p_serve.add_argument('-p', '--port', type=int, default=8000)
    p_serve.add_argument('--drafts', action='store_true', help='include drafts')

    args = parser.parse_args()
    if args.command == 'build':
        cmd_build(args)
    elif args.command == 'clean':
        cmd_clean(args)
    elif args.command == 'new':
        cmd_new(args)
    elif args.command == 'serve':
        cmd_serve(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
