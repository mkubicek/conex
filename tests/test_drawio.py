"""Tests for draw.io diagram detection and processing."""

from confluence_export.drawio import (
    detect_drawio_macros,
    find_drawio_attachments,
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
