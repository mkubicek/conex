"""Tests for HTML to markdown conversion."""

from confluence_export.converter import convert_page, sanitize_filename, _preprocess_html
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
    assert "media/screenshot.png" in result
    assert "ac:image" not in result


def test_preprocess_ac_link():
    html = (
        '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
        "<ac:plain-text-link-body>My Document</ac:plain-text-link-body>"
        "</ac:link>"
    )
    result = _preprocess_html(html, [])
    assert "media/doc.pdf" in result
    assert "My Document" in result


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


def test_preprocess_drawio_placeholder():
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">architecture</ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "[drawio:architecture]" in result
