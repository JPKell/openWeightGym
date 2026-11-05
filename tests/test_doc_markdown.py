"""Docs written in Markdown are shown with the console's own cards, tables, steps and code blocks."""

import re

from weightroom import doc_markdown
from weightroom.system_docs import ROOT, SECTIONS, written
from test_weightroom import desk, sign_in  # noqa: F401

SAMPLE = """# Sample guide

What this is for.

## First part

| Part | Need |
|---|---|
| Gateway | `toollocker` |

### 1. Check it

```bash
echo '<b>hello</b>'
```

## Second part

### Plain heading

    indented code

<script>alert(1)</script>
"""


def test_a_document_is_its_title_overview_and_one_section_per_second_level_heading():
    found = doc_markdown.document(SAMPLE)
    assert found["title"] == "Sample guide" and "Sample guide" not in found["overview"]
    assert found["overview"] == "<p>What this is for.</p>\n"
    assert found["contents"] == [("first-part", "First part"), ("second-part", "Second part")]
    first, second = (section["body"] for section in found["sections"])
    # Tables are the console's, in a frame that scrolls by itself on a phone.
    assert '<div class="table-responsive docs-table-frame" tabindex="0" role="region" aria-label="Table">' in first
    assert '<table class="table table-sm docs-table">' in first and "<td><code>toollocker</code></td>" in first
    # A numbered heading is a step: its number moves out of the text into the circle.
    assert '<h3 class="docs-step" data-step="1" id="first-part-check-it">Check it</h3>' in first
    # Code is labelled, copyable by its own id, and shown as text.
    assert '<span>Shell</span>' in first and 'data-copy-target="first-part-code-1"' in first
    assert '<code id="first-part-code-1">echo &#x27;&lt;b&gt;hello&lt;/b&gt;&#x27;\n</code>' in first
    assert '<h3 id="second-part-plain-heading">Plain heading</h3>' in second
    assert 'data-copy-target="second-part-code-1"' in second and "<span>Text</span>" in second
    assert "<script>" not in second and "&lt;script&gt;" in second  # raw HTML is never run


def test_a_heading_inside_a_code_block_does_not_start_a_section():
    found = doc_markdown.document("## Only\n\n```text\n## not a heading\n```\n")
    assert found["contents"] == [("only", "Only")] and "## not a heading" in found["sections"][0]["body"]


def test_every_written_guide_is_found_and_has_sections():
    for section in SECTIONS:
        for topic in section["topics"]:
            if not topic["doc"]:
                continue
            assert (ROOT / topic["doc"]).is_file(), topic["url"]
            found = written(topic)
            assert found["sections"], topic["url"]
            # A renamed heading in the Markdown file would otherwise drop its part from the page without a word.
            assert set(topic["parts"]) <= {part["title"] for part in found["sections"]}, topic["url"]
            assert len({ident for ident, _ in found["contents"]}) == len(found["contents"]), topic["url"]  # every link reaches one section


def test_links_to_other_files_go_to_their_docs_page_or_become_text():
    resolve = {"guide.md": "/docs/guide", "a/b.md": "/docs/b"}.get
    found = doc_markdown.document("## Part\n\n[guide](guide.md#setup), [other](other.md), [web](https://example.com/) and [here](#part)\n",
                                  links=lambda href: href if href.startswith(("https:", "#")) else resolve(href.split("#")[0]))
    body = found["sections"][0]["body"]
    assert '<a href="/docs/guide">guide</a>' in body and '<a href="https://example.com/">web</a>' in body and '<a href="#part">here</a>' in body
    assert "other" in body and 'href="other.md"' not in body  # a file the docs do not show keeps only its text
    page = written(next(topic for section in SECTIONS for topic in section["topics"] if topic["doc"] == "docs/installation.md"))
    shown = str(page["overview"]) + "".join(str(part["body"]) for part in page["sections"])
    assert 'href="/docs/modules/toollocker"' in shown
    assert all(href.startswith(("/", "#", "https:", "http:")) for href in re.findall(r'href="([^"]+)"', shown))  # no link is left relative


def test_doc_pages_put_each_section_in_a_card_with_links_to_them(desk):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    pages = {"/docs/modules/toollocker": ("overview", "requirements", "install-toollocker-on-front", "troubleshooting", "source"),
             "/docs/modules/mirrorwall": ("overview", "workflow", "models", "adding", "references", "trouble", "source"),
             "/docs/api": ("api-connection", "topics"),
             "/docs/api/openai": ("api-compatible", "api-unsupported"),
             "/docs/api/headers": ("api-headers", "api-response-headers"),
             "/docs/modules/loadcoach": ("source",),
             "/docs": ("system-map", "docs-sections", "topics"),
             "/docs/operations/installation": ("overview", "what-you-need", "adding-a-gpu-node"),
             "/docs/benchmarks/measures": ("what-the-benchmarks-measure", "tool-use-and-recovery-p4"),
             "/docs/web-app/chat": ("chat", "chat-options", "chat-list")}
    for path, sections in pages.items():
        page = browser.get(path)
        assert page.status_code == 200, path
        cards = re.findall(r'<section class="card mb-3 docs-section" id="([^"]+)" aria-labelledby="\1-title">', page.text)
        assert set(sections) <= set(cards), (path, cards)
        assert 'class="card-body docs-prose"' in page.text
        for ident in sections[:-1]:
            assert f'href="#{ident}"' in page.text or ident == "overview", (path, ident)  # the links at the top reach the sections
    toollocker = browser.get("/docs/modules/toollocker").text
    assert toollocker.count('class="docs-step"') >= 4 and toollocker.count("data-copy-target=") >= 8
    assert '<nav class="docs-crumbs" aria-label="Documentation breadcrumb"><a href="/docs/modules">Modules</a>' in toollocker
