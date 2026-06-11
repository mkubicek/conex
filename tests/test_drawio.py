"""Tests for draw.io diagram detection and processing."""

import os
from pathlib import Path
from unittest.mock import patch

from confluence_export.drawio import (
    detect_drawio_macros,
    find_drawio_attachments,
    render_drawio_to_png,
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


class _DoneProcess:
    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self):
        return 0


def _popen_writes_png(args, **_kwargs):
    output = Path(args[args.index("--output") + 1])
    output.write_bytes(b"PNG")
    return _DoneProcess()


def test_forced_render_preserves_existing_png_mode(tmp_path):
    source = tmp_path / "diagram.drawio"
    source.write_text("<xml/>")
    output = tmp_path / "diagram.drawio.png"
    output.write_bytes(b"old")
    output.chmod(0o640)

    with patch("confluence_export.drawio.find_drawio_cli", return_value="drawio"), \
         patch("confluence_export.drawio.subprocess.Popen", side_effect=_popen_writes_png):
        result = render_drawio_to_png(source, output, force=True)

    assert result == output
    assert output.read_bytes() == b"PNG"
    assert output.stat().st_mode & 0o777 == 0o640


def test_forced_render_uses_umask_mode_for_new_png(tmp_path):
    source = tmp_path / "diagram.drawio"
    source.write_text("<xml/>")
    output = tmp_path / "diagram.drawio.png"
    old_umask = os.umask(0o027)
    try:
        with patch("confluence_export.drawio.find_drawio_cli", return_value="drawio"), \
             patch("confluence_export.drawio.subprocess.Popen", side_effect=_popen_writes_png):
            result = render_drawio_to_png(source, output, force=True)
    finally:
        os.umask(old_umask)

    assert result == output
    assert output.read_bytes() == b"PNG"
    assert output.stat().st_mode & 0o777 == 0o640


def test_find_drawio_attachments_tolerates_null_media_type():
    # #47: an attachment record with "mediaType": null reaches consumers as a
    # real None unless from_api coalesces it; the matcher must not crash and
    # must still match by title.
    atts = [
        Attachment.from_api({"id": "a", "title": None, "mediaType": None}),
        Attachment.from_api({"id": "b", "title": "d.drawio", "mediaType": None}),
    ]
    assert [a.id for a in find_drawio_attachments(atts)] == ["b"]
