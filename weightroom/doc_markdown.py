"""Documentation written in Markdown, shown with the console's own cards, tables and code blocks.

A document's `# ` heading is its title and each `## ` heading starts one of its sections, which the
docs pages show as cards; what comes before the first section is its overview. Tables take the
console's table style, numbered `### 1. Step` headings become step headings, and every code block
is labelled with its language and has a Copy button. Raw HTML in the source is shown as text, never run.
A `links` function, when given, decides where each link goes: a new address, or None to keep only its text.
"""

import re
from html import escape

from markdown_it import MarkdownIt
from markupsafe import Markup

STEP = re.compile(r"^(\d+)\.\s+")
LANGUAGES = {"bash": "Shell", "sh": "Shell", "shell": "Shell", "console": "Shell", "toml": "TOML", "python": "Python",
             "py": "Python", "json": "JSON", "ini": "INI", "yaml": "YAML", "text": "Text", "": "Text"}


def slug(title: str) -> str:
    """The anchor a heading is linked by: its words in lower case, joined by dashes."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _table_open(self, tokens, idx, options, env) -> str:
    """A table scrolls on its own on a narrow screen and looks like every other table in the console."""
    return '<div class="table-responsive docs-table-frame" tabindex="0" role="region" aria-label="Table">\n<table class="table table-sm docs-table">\n'


def _table_close(self, tokens, idx, options, env) -> str:
    """Close what `_table_open` opened."""
    return "</table>\n</div>\n"


def _code(self, tokens, idx, options, env) -> str:
    """A code block with its language above it and a button that copies it."""
    token = tokens[idx]
    env["code"] = env.get("code", 0) + 1
    ident = f"{env.get('prefix', 'doc')}-code-{env['code']}"
    language = token.info.strip().split(" ", 1)[0].lower() if token.info else ""
    label = LANGUAGES.get(language, language)
    return (f'<div class="docs-code"><div class="docs-code-head"><span>{escape(label)}</span>'
            f'<button type="button" class="btn btn-sm docs-code-copy" data-copy-target="{ident}" aria-label="Copy this {escape(label)} block">Copy</button></div>'
            f'<pre><code id="{ident}">{escape(token.content)}</code></pre></div>\n')


RENDERER = MarkdownIt("commonmark", {"html": False}).enable("table")
for rule, function in (("table_open", _table_open), ("table_close", _table_close), ("fence", _code), ("code_block", _code)):
    RENDERER.add_render_rule(rule, function)


def _relink(tokens, links) -> None:
    """Point each link where `links` says, or drop the link and keep its text."""
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        kept, dropping = [], False
        for child in token.children:
            if child.type == "link_open":
                target = links(str(child.attrGet("href") or ""))
                if target is None:
                    dropping = True
                    continue
                child.attrSet("href", target)
            elif child.type == "link_close" and dropping:
                dropping = False
                continue
            kept.append(child)
        token.children = kept


def render(text: str, prefix: str = "doc", links=None) -> Markup:
    """One part of a document as HTML. `prefix` keeps its heading and code-block ids apart from other parts' on the same page."""
    env = {"prefix": prefix}
    tokens = RENDERER.parse(text, env)
    if links is not None:
        _relink(tokens, links)
    for number, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        inline = tokens[number + 1]
        if token.tag in ("h1", "h2"):
            token.tag = tokens[number + 2].tag = "h3"  # a section's own headings sit below its card's title
        step = STEP.match(inline.content)
        if step and token.tag == "h3":
            inline.content = inline.content[step.end():]
            first = inline.children[0] if inline.children else None
            if first is not None and first.type == "text":
                first.content = STEP.sub("", first.content, count=1)
            token.attrSet("class", "docs-step")
            token.attrSet("data-step", step[1])
        token.attrSet("id", f"{prefix}-{slug(inline.content)}")
    return Markup(RENDERER.renderer.render(tokens, RENDERER.options, env))


def document(text: str, links=None) -> dict:
    """A whole Markdown document: its title, its overview and its sections, each rendered, with the links to them."""
    lines = text.splitlines()
    title, title_lines, starts = "", range(0), []
    for number, token in enumerate(tokens := RENDERER.parse(text)):
        if token.type == "heading_open" and token.map and token.tag == "h1" and not title and not starts:
            title, title_lines = tokens[number + 1].content.strip(), range(*token.map)
        elif token.type == "heading_open" and token.map and token.tag == "h2":
            starts.append(token.map)
    first = starts[0][0] if starts else len(lines)
    overview = "\n".join(line for number, line in enumerate(lines[:first]) if number not in title_lines)
    sections = []
    headings = [token.content.strip() for number, token in enumerate(tokens)
                if token.type == "inline" and tokens[number - 1].type == "heading_open" and tokens[number - 1].tag == "h2"]
    for place, ((start, after), heading) in enumerate(zip(starts, headings)):
        end = starts[place + 1][0] if place + 1 < len(starts) else len(lines)
        ident = slug(heading) or f"section-{place + 1}"
        sections.append({"id": ident, "title": heading, "body": render("\n".join(lines[after:end]), ident, links)})
    return {"title": title, "overview": render(overview, "overview", links) if overview.strip() else Markup(""),
            "sections": sections, "contents": [(section["id"], section["title"]) for section in sections]}
