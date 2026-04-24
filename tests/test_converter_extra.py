"""Converter tests: Confluence HTML in, verify transformed output."""

import pytest

from confluence_export.converter import _preprocess_html, convert_page
from confluence_export.types import Attachment, Page, Version


# -- Parametrized preprocessing tests ----------------------------------------
# Each case: (description, input HTML, expected substrings, forbidden substrings)

PREPROCESS_CASES = [
    # Emoticons
    ("known emoticon tick", '<ac:emoticon ac:name="tick"/>', ["\u2705"], []),
    ("unknown emoticon removed", '<ac:emoticon ac:name="zzz-unknown"/>', [], ["ac:emoticon"]),
    ("emoji shortname fallback", '<ac:emoticon ac:name="" ac:emoji-shortname=":tick:"/>', ["\u2705"], []),

    # Time tags
    ("datetime preserved", '<time datetime="2025-03-15"/>', ["2025-03-15"], []),
    ("empty time removed", "<time/>", [], ["<time"]),

    # Task lists
    (
        "complete task",
        "<ac:task-list><ac:task><ac:task-status>complete</ac:task-status>"
        "<ac:task-body>Done item</ac:task-body></ac:task></ac:task-list>",
        ["[x]", "Done item"], [],
    ),
    (
        "incomplete task",
        "<ac:task-list><ac:task><ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>Todo item</ac:task-body></ac:task></ac:task-list>",
        ["[ ]", "Todo item"], [],
    ),

    # Images
    ("attachment image", '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>', [".media/shot.png"], ["ac:image"]),
    ("external image", '<ac:image><ri:url ri:value="https://example.com/img.png"/></ac:image>', ["https://example.com/img.png"], []),
    ("image no source removed", "<ac:image></ac:image>", [], ["ac:image"]),

    # Links
    (
        "attachment link",
        '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
        "<ac:plain-text-link-body>My Doc</ac:plain-text-link-body></ac:link>",
        [".media/doc.pdf", "My Doc"], [],
    ),
    (
        "page link preserves label",
        '<ac:link><ri:page ri:content-title="Other Page"/>'
        "<ac:plain-text-link-body>See here</ac:plain-text-link-body></ac:link>",
        ["See here"], [],
    ),

    # Macros: panels, code, status, expand, jira, view-file, drawio
    (
        "info panel",
        '<ac:structured-macro ac:name="info">'
        '<ac:parameter ac:name="title">Important</ac:parameter>'
        "<ac:rich-text-body><p>Details here</p></ac:rich-text-body></ac:structured-macro>",
        ["Important", "Details here", "<blockquote>"], [],
    ),
    (
        "code block",
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body>print('hi')</ac:plain-text-body></ac:structured-macro>",
        ["<pre>", "<code", "print('hi')"], [],
    ),
    ("status macro", '<ac:structured-macro ac:name="status"><ac:parameter ac:name="title">DONE</ac:parameter></ac:structured-macro>', ["DONE"], []),
    ("status macro empty", '<ac:structured-macro ac:name="status"></ac:structured-macro>', [], ["ac:structured-macro"]),
    (
        "expand macro",
        '<ac:structured-macro ac:name="expand">'
        '<ac:parameter ac:name="title">More</ac:parameter>'
        "<ac:rich-text-body><p>Details</p></ac:rich-text-body></ac:structured-macro>",
        ["More", "Details"], [],
    ),
    ("jira macro", '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PROJ-42</ac:parameter></ac:structured-macro>', ["PROJ-42"], []),
    ("jira macro empty", '<ac:structured-macro ac:name="jira"></ac:structured-macro>', [], ["ac:structured-macro"]),
    ("view-file via ri:attachment", '<ac:structured-macro ac:name="view-file"><ri:attachment ri:filename="report.pdf"/></ac:structured-macro>', [".media/report.pdf"], []),
    ("view-file via name param", '<ac:structured-macro ac:name="view-file"><ac:parameter ac:name="name">doc.pdf</ac:parameter></ac:structured-macro>', [".media/doc.pdf"], []),
    ("view-file empty", '<ac:structured-macro ac:name="view-file"></ac:structured-macro>', [], ["ac:structured-macro"]),
    ("drawio placeholder", '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">arch</ac:parameter></ac:structured-macro>', ["[drawio:arch]"], []),

    # Structural macros
    ("toc removed", '<ac:structured-macro ac:name="toc"></ac:structured-macro>', [], []),
    ("excerpt preserves body", '<ac:structured-macro ac:name="excerpt"><ac:rich-text-body><p>Summary</p></ac:rich-text-body></ac:structured-macro>', ["Summary"], []),
    (
        "section/column unwrapped",
        '<ac:structured-macro ac:name="section"><ac:rich-text-body>'
        '<ac:structured-macro ac:name="column"><ac:rich-text-body><p>Col</p></ac:rich-text-body></ac:structured-macro>'
        "</ac:rich-text-body></ac:structured-macro>",
        ["Col"], [],
    ),
    ("unknown macro keeps body", '<ac:structured-macro ac:name="xyz"><ac:rich-text-body><p>Keep</p></ac:rich-text-body></ac:structured-macro>', ["Keep"], []),
    ("unknown macro no body removed", '<ac:structured-macro ac:name="xyz"></ac:structured-macro>', [], []),

    # Decision lists
    ("decision item with text", '<ac:adf-node type="decisionItem" state="DECIDED"><p>Decided X</p></ac:adf-node>', ["Decided X"], []),
    ("decision item empty removed", '<ac:adf-node type="decisionItem"></ac:adf-node>', [], []),

    # Layout & cleanup
    ("layout tags unwrapped", "<ac:layout><ac:layout-section><ac:layout-cell><p>Inner</p></ac:layout-cell></ac:layout-section></ac:layout>", ["Inner"], ["ac:layout"]),
    ("inline comment unwrapped", '<p>Before <ac:inline-comment-marker ac:ref="x">noted</ac:inline-comment-marker> after</p>', ["noted"], ["ac:inline-comment-marker"]),
    ("placeholder removed", "<ac:placeholder>Type here</ac:placeholder>", [], ["Type here"]),
    ("adf-fallback removed", "<ac:adf-content><p>Real</p></ac:adf-content><ac:adf-fallback><p>Dupe</p></ac:adf-fallback>", ["Real"], ["Dupe"]),

    # Profile macro (ri:user consumed by mention handler first)
    ("profile macro no user", '<ac:structured-macro ac:name="profile"></ac:structured-macro>', ["Unknown user"], []),
]


@pytest.mark.parametrize("desc,html,expected,forbidden", PREPROCESS_CASES, ids=[c[0] for c in PREPROCESS_CASES])
def test_preprocess(desc, html, expected, forbidden):
    result = _preprocess_html(html, [])
    for s in expected:
        assert s in result, f"expected '{s}' in output for: {desc}"
    for s in forbidden:
        assert s not in result, f"forbidden '{s}' found in output for: {desc}"


# -- Tests that need a user resolver -----------------------------------------

def test_user_mention_resolved():
    html = '<ac:link><ri:user ri:account-id="abc"/></ac:link>'
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result


def test_user_mention_unresolved_shows_id():
    html = '<ac:link><ri:user ri:account-id="abc"/></ac:link>'
    result = _preprocess_html(html, [])
    assert "@abc" in result


# -- Full pipeline test: HTML → markdown with frontmatter --------------------

def test_full_conversion_pipeline():
    """End-to-end: Confluence storage HTML → final markdown string."""
    page = Page(
        id="42", title="Architecture Overview", space_id="1",
        version=Version(created_at="2025-06-15T10:00:00Z", number=7),
        body_storage=(
            "<h2>Summary</h2>"
            "<p>This page describes the <strong>system architecture</strong>.</p>"
            '<ac:structured-macro ac:name="info">'
            '<ac:parameter ac:name="title">Note</ac:parameter>'
            "<ac:rich-text-body><p>Updated for v2.</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            '<ac:image><ri:attachment ri:filename="diagram.png"/></ac:image>'
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">yaml</ac:parameter>'
            "<ac:plain-text-body>services:\n  api: true</ac:plain-text-body>"
            "</ac:structured-macro>"
        ),
        webui="/spaces/TEST/pages/42",
    )
    att = Attachment(id="a1", title="diagram.png", media_type="image/png",
                     file_size=5000, page_id="42")

    md = convert_page(page, base_url="https://x.atlassian.net",
                       space_key="TEST", path="/Architecture Overview",
                       attachments=[att])

    # Frontmatter
    assert "title: Architecture Overview" in md
    assert "page_id:" in md
    assert "version: 7" in md

    # Content conversion
    assert "## Summary" in md
    assert "**system architecture**" in md

    # Info panel became blockquote
    assert "Note" in md
    assert "Updated for v2" in md

    # Image converted to markdown
    assert ".media/diagram.png" in md

    # Code block preserved
    assert "services:" in md
    assert "api: true" in md

    # No Confluence markup leaked
    assert "ac:structured-macro" not in md
    assert "ac:image" not in md
    assert "ri:attachment" not in md
