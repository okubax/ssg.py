# ssg.py

A tiny, zero-dependency static site generator. One file, the Python
standard library, nothing else. No `pip install`, no `node_modules`, no
Ruby, no gems, no lockfiles, no build tool for the build tool.

```sh
curl -O https://raw.githubusercontent.com/okubax/ssg.py/main/ssg.py
chmod +x ssg.py
./ssg.py new "My first post"
./ssg.py serve
```

If `python3` runs, the site builds. That's the whole pitch.

I wrote this to run my own sites (a personal blog, a small African news
site, a school site, a charity site) after getting tired of each one
depending on a different tool, a different Ruby or Node version, and a
different set of half-remembered quirks. There's a longer version of
that story [on my blog](https://okubax.co.uk/2026/08/15/building-my-own-static-site-generator/),
and a walkthrough of [how the templates and themes actually work](https://okubax.co.uk/2026/09/11/ssg-py-templates-and-themes/)
if you want the background; this README is the reference for actually
using the thing.

## Quick start

Clone this repo and look at `example/` — it's a complete, tiny site:

```sh
git clone https://github.com/okubax/ssg.py.git
cd ssg.py/example
python3 ../ssg.py build
python3 -m http.server -d output
```

Or start from nothing:

```sh
./ssg.py new "Hello world"          # writes content/posts/2026-01-01-hello-world.md
./ssg.py new "About" --page         # writes content/pages/about.md
./ssg.py serve                      # build + serve at :8000, rebuild on save
./ssg.py build                      # build once into output/
```

`serve` runs in preview mode: nothing that looks like ads or analytics
gets injected, regardless of what your templates do, so you can't
accidentally fire a real pageview or ad impression while you're just
looking at a draft. `build` is the real thing.

## Site layout

```
config.yml          site-wide settings
content/
  posts/             blog posts — Markdown, YAML front matter
  pages/             standalone pages — Markdown or HTML
  index.html         root-level files, rendered as templates
  404.html           (front matter decides the URL — see below)
templates/           HTML templates
static/              copied into output/ byte-for-byte
output/              the built site — generated, never edit by hand
```

Nothing here is mandatory except `config.yml`. No posts, no pages, no
static folder — the build just produces less. There's no plugin system,
no config option you have to discover to turn a feature on; if a template
file with the right name exists, ssg.py renders the pages that need it.

## Front matter

Every post and page starts with a YAML block:

```markdown
---
title: "My post"
date: 2026-01-05 10:00
tags: [linux, notes]
category: Linux
description: "One line for meta tags and search"
---

The rest of the file is Markdown.
```

For posts, the date and the last part of the URL both default to the
filename (`2026-01-05-my-post.md`), so for most posts you don't need a
`date:` field at all. Set one explicitly to override it, or set `slug:`
to control the URL independently of the filename — useful when a
filename and a good URL want to be different things.

Recognised keys: `title`, `date`, `slug`, `tags` (a list), `category` (a
single value — ssg.py has one category per post, not many), `author`,
`description`, `summary`, `template` (which template file renders this
post/page), `permalink` (an exact URL, overriding the `post_url`/`page_url`
pattern), `draft: true` (excluded from `build`, included with
`build --drafts`). Anything else you put in front matter also becomes
available in your templates as `page.whatever_you_called_it`.

## Templates

The template language is small and deliberately Jinja-shaped, so if
you've used Jinja2, Liquid, or Nunjucks, you already know it:

```
{{ variable }}
{{ variable | filter }}
{{ variable | filter(arg) }}

{% if condition %} ... {% elif other %} ... {% else %} ... {% endif %}
{% for item in items %} ... {% else %} ... {% endfor %}
{% for key, value in a_dict %} ... {% endfor %}

{% include "partial/thing.html" %}
{% extends "base.html" %}
{% block content %} ... {% endblock %}
```

Inside a `{% for %}` loop, `loop.index` (1-based), `loop.index0`,
`loop.first`, `loop.last`, `loop.length`, `loop.prev`, and `loop.next` are
all available. `{%-` / `-%}` trims adjacent whitespace, same convention
as Jinja.

Filters ship built in: `e`/`escape`, `date(fmt)`, `date_iso`, `slugify`,
`lower`, `upper`, `title`, `capitalize`, `length`/`count`, `join(sep,
attr)`, `default(fallback)`, `striptags`, `truncate(n)`,
`truncatewords(n)`, `replace(old, new)`, `json`, `first`, `last`,
`sort(attr, reverse)`, `strip`, `absolute(base)`. Adding your own is a
five-line function:

```python
@filter_('shout')
def _f_shout(v):
    return str(v).upper() + '!'
```

— dropped anywhere in `ssg.py` itself, since there's no plugin loader to
configure; the file you're editing *is* the whole program.

### Which template renders what

| Content | Template | Notes |
|---|---|---|
| A post | `page.template` if set, else `post_template` in config | |
| A page | `page.template` if set, else `page_template` in config | |
| The paginated post index | whatever `template:` a root `content/index.html` declares | needs `paginate: true` in its front matter |
| A tag archive | `tag.html` | only built if this file exists |
| A category archive | `category.html` | only built if this file exists |
| An author archive | `author.html` | only built if this file exists |
| A year/month archive | `period_archives.html` | only built if this file exists and `year_archive_url` is set |

That "only built if the template exists" rule is the whole extension
mechanism. Don't want tag pages? Don't create `tag.html`. Want an
author archive? Add `author.html` and `author_url` to `config.yml`, and
it starts appearing — no other configuration.

### Building a theme from scratch

A minimal theme is three files:

**`templates/base.html`** — the shell every page shares:

```html
<!DOCTYPE html>
<html>
<head><title>{{ site.title }}</title></head>
<body>
  {% block content %}{% endblock %}
</body>
</html>
```

**`templates/index.html`** — the post list, extending the shell:

```html
{% extends "base.html" %}
{% block content %}
  {% for post in paginator.posts %}
    <a href="{{ post.url }}">{{ post.title }}</a>
  {% endfor %}
{% endblock %}
```

**`templates/post.html`** — a single post:

```html
{% extends "base.html" %}
{% block content %}
  <h1>{{ page.title }}</h1>
  {{ content }}
{% endblock %}
```

That's a working, if plain, site. `example/` in this repo takes it a bit
further (tags, an about page, a stylesheet) but the shape doesn't change
as a site grows — everything else is templates including more `{% for %}`
loops and more filters, not new mechanism.

## config.yml

The generator only requires a `config.yml` to exist; everything in it is
optional and just shows up as `site.whatever` in templates. The keys
ssg.py itself understands:

```yaml
title: My Site
url: https://example.com
description: ...
author: ...

post_url: /{year}/{month}/{day}/{slug}/
page_url: /{slug}/
tag_url: /tag/{slug}.html
category_url: /category/{slug}.html
author_url: /author/{slug}.html
year_archive_url: /{year}/
month_archive_url: /{year}/{month}/

paginate: 10                 # posts per page on the index
paginate_first: /
paginate_path: /page{num}/

atom_feed: /atom.xml
json_feed: /feed.json
search_index: /search.json    # {title, url, tags, ...} per post — wire up your own client-side search
sitemap: true

smart_quotes: true            # curly quotes, en/em dashes, in Markdown output
```

Anything else — `navigation`, `social`, an AdSense client ID, a Matomo
site ID, whatever your templates want to read as `site.x` — is yours to
invent. `config.yml` is passed to every template basically verbatim.

## What it deliberately doesn't do

No asset pipeline (bring your own CSS, or a `Makefile` if you want
Sass). No plugin system (see above — you edit the file). No incremental
build cache (`build` always starts from a clean `output/`; it's fast
enough on real sites — a few hundred pages in well under a second — that
this has never mattered in practice). No multi-language/i18n support.
If you need any of these, a mature tool like Hugo, Zola, or Eleventy will
serve you better — this project's whole reason to exist is being small
enough to read start to finish in one sitting, not being the most capable
generator available.

## License

MIT — see [LICENSE](LICENSE).
