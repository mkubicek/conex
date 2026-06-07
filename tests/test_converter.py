"""Tests for HTML to markdown conversion."""

from confluence_export.converter import convert_page, sanitize_filename, _preprocess_html
from confluence_export.paths import safe_attachment_name
from confluence_export.types import Attachment, Page, Version


def test_sanitize_filename():
    assert sanitize_filename("Hello World") == "Hello-World"
    assert sanitize_filename("Page/With:Special*Chars") == "PageWithSpecialChars"
    assert sanitize_filename("  spaces  ") == "spaces"
    assert sanitize_filename("") == "untitled"
    assert sanitize_filename("a" * 200) == "a" * 100


def test_sanitize_filename_preserves_hyphens():
    assert sanitize_filename("my-page-title") == "my-page-title"


def test_convert_page_basic():
    page = Page(
        id="123",
        title="Test Page",
        space_id="100",
        version=Version(created_at="2025-01-01T00:00:00Z", number=5),
        body_storage="<p>Hello <strong>world</strong></p>",
        webui="/spaces/TEST/pages/123/Test+Page",
    )
    md = convert_page(page, base_url="https://example.atlassian.net", space_key="TEST", path="/Test Page")

    assert "---" in md
    assert "title: Test Page" in md
    assert "page_id: '123'" in md or 'page_id: "123"' in md or "page_id: '123'" in md
    assert "# Test Page" in md
    assert "**world**" in md


def test_convert_page_decision_list_renders_as_markdown_list():
    # #40: an ADF decision list renders as a real markdown bullet list, with decided
    # items marked, instead of flattening to plain text.
    page = Page(
        id="124",
        title="Decisions",
        space_id="100",
        version=Version(created_at="2025-01-01T00:00:00Z", number=1),
        body_storage=(
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>Ship it</p></ac:adf-node>'
            '<ac:adf-node type="decisionItem" state="UNDECIDED"><p>Maybe later</p></ac:adf-node>'
            "</ac:adf-node>"
        ),
    )
    md = convert_page(page, base_url="https://x.atlassian.net", space_key="TEST", path="/Decisions")
    assert "* ✓ Ship it" in md  # decided item, marked, as a markdown list item
    assert "* Maybe later" in md  # undecided item, no marker
    assert "✓ Maybe later" not in md


def test_convert_page_frontmatter_has_attachments():
    page = Page(
        id="123",
        title="With Attachments",
        body_storage="<p>Content</p>",
        version=Version(number=1),
    )
    attachments = [
        Attachment(id="a1", title="file.pdf", media_type="application/pdf", file_size=1000),
    ]
    md = convert_page(page, base_url="", space_key="TEST", path="/With Attachments", attachments=attachments)
    assert "file.pdf" in md
    assert "application/pdf" in md


def test_preprocess_ac_image():
    html = '<ac:image><ri:attachment ri:filename="screenshot.png"/></ac:image>'
    result = _preprocess_html(html, [])
    assert ".media/screenshot.png" in result
    assert "ac:image" not in result


def test_preprocess_ac_image_uses_sanitized_attachment_name():
    html = '<ac:image><ri:attachment ri:filename="a/b.png"/></ac:image>'
    result = _preprocess_html(html, [Attachment(id="a1", title="a/b.png")])
    assert ".media/a-b.png" in result
    assert ".media/a/b.png" not in result


def test_preprocess_ac_image_uses_planned_name_for_casefold_reference():
    html = '<ac:image><ri:attachment ri:filename="report.pdf"/></ac:image>'
    result = _preprocess_html(html, [Attachment(id="a1", title="Report.pdf")])
    assert ".media/Report.pdf" in result
    assert ".media/report.pdf" not in result


def test_preprocess_ac_link():
    html = (
        '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
        "<ac:plain-text-link-body>My Document</ac:plain-text-link-body>"
        "</ac:link>"
    )
    result = _preprocess_html(html, [])
    assert ".media/doc.pdf" in result
    assert "My Document" in result


def test_preprocess_ac_link_uses_sanitized_attachment_name():
    raw = "../../../x.png"
    html = (
        f'<ac:link><ri:attachment ri:filename="{raw}"/>'
        "<ac:plain-text-link-body>Unsafe</ac:plain-text-link-body>"
        "</ac:link>"
    )
    result = _preprocess_html(html, [Attachment(id="a1", title=raw)])
    assert f".media/{safe_attachment_name(raw)}" in result
    assert "../" not in result


def test_attachment_link_markdown_escapes_url_and_label_injection():
    title = "file name](x).pdf"
    page = Page(
        id="p1",
        title="P",
        body_storage=(
            f'<ac:link><ri:attachment ri:filename="{title}"/>'
            "<ac:plain-text-link-body>x](javascript:alert(1))</ac:plain-text-link-body>"
            "</ac:link>"
        ),
    )
    md = convert_page(
        page,
        base_url="https://example.atlassian.net/wiki",
        space_key="TEST",
        path="P",
        attachments=[Attachment(id="a1", title=title)],
    )

    assert ".media/file%20name%5D%28x%29.pdf" in md
    assert "](javascript:" not in md


def test_preprocess_panel():
    html = (
        '<ac:structured-macro ac:name="info">'
        '<ac:parameter ac:name="title">Important</ac:parameter>'
        "<ac:rich-text-body><p>Some info here</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "<blockquote>" in result
    assert "Important" in result
    assert "Some info here" in result


def test_preprocess_code_block():
    html = (
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[print('hello')]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "<pre>" in result
    assert "<code" in result
    assert "print('hello')" in result


def test_preprocess_drawio_not_rendered_falls_back():
    # No rendered PNG and no attachment: graceful "not rendered" note, never a
    # raw [drawio:NAME] sentinel that could leak to the output (#8).
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">architecture</ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "Draw.io diagram not rendered: architecture.drawio" in result
    assert "[drawio:" not in result
