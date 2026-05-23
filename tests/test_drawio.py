"""Tests for draw.io diagram detection and processing."""

from confluence_export.drawio import (
    detect_drawio_macros,
    drawio_name_candidates,
    find_drawio_attachment,
    find_drawio_attachments,
    find_drawio_macro_refs,
)
from confluence_export.types import Attachment


def test_find_drawio_attachments():
    attachments = [
        Attachment(id="1", title="image.png", media_type="image/png"),
        Attachment(id="2", title="arch.drawio", media_type="application/x-drawio"),
        Attachment(id="3", title="doc.pdf", media_type="application/pdf"),
        Attachment(id="4", title="flow.drawio", media_type="application/octet-stream"),
    ]
    result = find_drawio_attachments(attachments)
    assert len(result) == 2
    titles = {a.title for a in result}
    assert titles == {"arch.drawio", "flow.drawio"}


def test_find_drawio_attachments_empty():
    assert find_drawio_attachments([]) == []


def test_detect_drawio_macros():
    html = (
        '<p>Some text</p>'
        '<ac:structured-macro ac:name="drawio" ac:schema-version="1">'
        '<ac:parameter ac:name="diagramName">architecture</ac:parameter>'
        '<ac:parameter ac:name="width">800</ac:parameter>'
        '</ac:structured-macro>'
        '<p>More text</p>'
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">sequence-flow</ac:parameter>'
        '</ac:structured-macro>'
    )
    names = detect_drawio_macros(html)
    assert names == ["architecture", "sequence-flow"]


def test_detect_drawio_macros_none():
    html = "<p>No drawio here</p>"
    assert detect_drawio_macros(html) == []


def test_find_drawio_macro_refs_includes_inc_and_sketch():
    html = (
        '<ac:structured-macro ac:name="inc-drawio">'
        '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
        '</ac:structured-macro>'
        '<ac:structured-macro ac:name="drawio-sketch">'
        '<ac:parameter ac:name="diagramName">sketch</ac:parameter>'
        '</ac:structured-macro>'
    )
    refs = find_drawio_macro_refs(html)
    assert [(r.macro_name, r.diagram_name) for r in refs] == [
        ("inc-drawio", "arch.drawio"),
        ("drawio-sketch", "sketch"),
    ]


def test_drawio_name_candidates():
    assert drawio_name_candidates("arch") == ["arch", "arch.drawio"]
    assert drawio_name_candidates("arch.drawio") == ["arch.drawio", "arch"]


def test_find_drawio_attachment_by_macro_name():
    attachments = [Attachment(id="1", title="arch.drawio", media_type="")]
    assert find_drawio_attachment(attachments, "arch") == attachments[0]


def test_find_drawio_attachment_prefers_exact_drawio_source_over_bare_collision():
    bare = Attachment(id="1", title="arch", media_type="application/x-drawio")
    exact = Attachment(id="2", title="arch.drawio", media_type="application/x-drawio")
    assert find_drawio_attachment([bare, exact], "arch.drawio") == exact


def test_find_drawio_attachment_ignores_non_drawio_bare_collision():
    bare = Attachment(id="1", title="arch", media_type="application/octet-stream")
    exact = Attachment(id="2", title="arch.drawio", media_type="application/x-drawio")
    assert find_drawio_attachment([bare, exact], "arch.drawio") == exact


def test_find_drawio_attachment_does_not_guess_wrong_single_attachment():
    attachments = [Attachment(id="1", title="other.drawio", media_type="application/x-drawio")]
    assert find_drawio_attachment(attachments, "arch") is None
