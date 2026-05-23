"""Tests for HTML to markdown conversion."""

from confluence_export.converter import (
    convert_page,
    inspect_macros,
    sanitize_filename,
    _preprocess_html,
)
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
    assert ".media/screenshot.png" in result
    assert "ac:image" not in result


def test_preprocess_ac_link():
    html = (
        '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
        "<ac:plain-text-link-body>My Document</ac:plain-text-link-body>"
        "</ac:link>"
    )
    result = _preprocess_html(html, [])
    assert ".media/doc.pdf" in result
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
    diagnostics = []
    result = _preprocess_html(html, [], diagnostics=diagnostics)
    assert "Draw.io diagram could not be rendered: architecture" in result
    assert "[drawio:architecture]" not in result
    assert diagnostics[0].code == "drawio_source_missing"


def test_drawio_rendered_before_markdown_handles_underscore(tmp_path):
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">system_arch.drawio</ac:parameter>'
        "</ac:structured-macro>"
    )
    page = Page(id="p1", title="Arch", body_storage=html, version=Version(number=1))
    png = tmp_path / "system_arch.drawio.png"
    png.write_bytes(b"png")
    diagnostics = []
    md = convert_page(
        page,
        base_url="",
        space_key="TEST",
        path="/Arch",
        attachments=[Attachment(id="a1", title="system_arch.drawio")],
        diagnostics=diagnostics,
        drawio_rendered={"system_arch.drawio": png},
    )

    assert "system_arch.drawio.png" in md
    assert "Draw.io source:" in md
    assert "[drawio:" not in md
    assert diagnostics == []


def test_drawio_render_failure_fallback_warns():
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
        "</ac:structured-macro>"
    )
    page = Page(id="p1", title="Arch", body_storage=html, version=Version(number=1))
    diagnostics = []
    md = convert_page(
        page,
        base_url="",
        space_key="TEST",
        path="/Arch",
        attachments=[Attachment(id="a1", title="arch.drawio")],
        diagnostics=diagnostics,
        drawio_failures={"arch.drawio": "render failed"},
    )

    assert "Draw.io diagram could not be rendered: arch.drawio" in md
    assert ".media/arch.drawio" in md
    assert "[drawio:" not in md
    assert diagnostics[0].severity == "warning"
    assert diagnostics[0].code == "drawio_render_failed"


def test_profile_macro_preserves_user_details():
    html = (
        '<ac:structured-macro ac:name="profile">'
        '<ri:user ri:account-id="abc"/>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(
        html,
        [],
        user_resolver=lambda _: {"displayName": "Alice", "email": "alice@example.com"},
    )
    assert "<li>Alice (alice@example.com)</li>" in result


def test_profile_picture_macro_renders_user_mention():
    html = (
        '<ac:structured-macro ac:name="profile-picture">'
        '<ac:parameter ac:name="User"><ri:user ri:account-id="abc"/></ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda _: {"displayName": "Alice"})
    assert "@Alice" in result
    assert "Confluence dynamic content" not in result


def test_unsupported_macro_visible_and_warns():
    diagnostics = []
    html = '<ac:structured-macro ac:name="future-widget"></ac:structured-macro>'
    result = _preprocess_html(html, [], diagnostics=diagnostics)
    assert "Confluence dynamic content: future-widget" in result
    assert diagnostics[0].severity == "warning"
    assert diagnostics[0].code == "unsupported_macro"


def test_unsupported_macro_with_body_preserves_body_and_warns():
    diagnostics = []
    html = (
        '<ac:structured-macro ac:name="custom-wrapper">'
        '<ac:rich-text-body><p>Keep this content</p></ac:rich-text-body>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [], diagnostics=diagnostics)
    assert "Keep this content" in result
    assert "Confluence dynamic content: custom-wrapper" not in result
    assert diagnostics[0].severity == "warning"
    assert diagnostics[0].code == "unsupported_macro_body_preserved"


def test_drawio_sketch_visible_and_warns():
    diagnostics = []
    html = '<ac:structured-macro ac:name="drawio-sketch"></ac:structured-macro>'
    result = _preprocess_html(html, [], diagnostics=diagnostics)
    assert "Unsupported drawio-sketch macro preserved as placeholder" in result
    assert diagnostics[0].severity == "warning"
    assert diagnostics[0].code == "unsupported_drawio_sketch"


def test_inspect_macros_reports_drawio_needs():
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
        "</ac:structured-macro>"
        '<ac:structured-macro ac:name="drawio-sketch">'
        '<ac:parameter ac:name="diagramName">sketch</ac:parameter>'
        "</ac:structured-macro>"
    )
    needs = inspect_macros(html, [Attachment(id="a1", title="arch.drawio")])
    assert needs.drawio_names == {"arch"}
