"""Tests for draw.io detection, rendering, and placeholder replacement."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_export.drawio import (
    detect_drawio_macros,
    find_drawio_attachments,
    find_drawio_cli,
    render_drawio_to_png,
    replace_drawio_placeholders,
)
from confluence_export.types import Attachment


class TestFindDrawioAttachments:
    def test_by_extension(self):
        atts = [
            Attachment(id="1", title="arch.drawio", media_type=""),
            Attachment(id="2", title="photo.png", media_type="image/png"),
        ]
        result = find_drawio_attachments(atts)
        assert len(result) == 1
        assert result[0].title == "arch.drawio"

    def test_by_media_type(self):
        atts = [
            Attachment(id="1", title="diagram", media_type="application/x-drawio"),
        ]
        assert len(find_drawio_attachments(atts)) == 1

    def test_empty(self):
        assert find_drawio_attachments([]) == []


class TestDetectDrawioMacros:
    def test_finds_diagram_names(self):
        html = (
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">architecture</ac:parameter>'
            '</ac:structured-macro>'
        )
        assert detect_drawio_macros(html) == ["architecture"]

    def test_no_macros(self):
        assert detect_drawio_macros("<p>no diagrams</p>") == []


class TestFindDrawioCli:
    def test_found_on_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/drawio"):
            assert find_drawio_cli() == "/usr/local/bin/drawio"

    def test_mac_app_bundle(self):
        with patch("shutil.which", return_value=None), \
             patch("pathlib.Path.exists", return_value=True):
            result = find_drawio_cli()
            assert result is not None
            assert "draw.io" in result

    def test_not_found(self):
        with patch("shutil.which", return_value=None), \
             patch("pathlib.Path.exists", return_value=False):
            assert find_drawio_cli() is None


class TestRenderDrawioToPng:
    def test_cli_not_found(self, capsys):
        with patch("confluence_export.drawio.find_drawio_cli", return_value=None):
            result = render_drawio_to_png(Path("test.drawio"))
            assert result is None
            assert "not found" in capsys.readouterr().err

    def test_existing_png_skipped(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"PNG data")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"):
            result = render_drawio_to_png(drawio)
            assert result == png

    def test_successful_render(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        expected_png = drawio.with_suffix(".drawio.png")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def fake_popen(*args, **kwargs):
            expected_png.write_bytes(b"PNG data")
            return mock_proc

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen) as mock_popen:
            result = render_drawio_to_png(drawio)
            assert result == expected_png
            mock_popen.assert_called_once()

    def test_render_failure(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # process exited with error, no file

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", return_value=mock_proc):
            result = render_drawio_to_png(drawio)
            assert result is None
            assert "no output" in capsys.readouterr().err

    def test_custom_output_path(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        custom = tmp_path / "custom.png"

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def fake_popen(*args, **kwargs):
            custom.write_bytes(b"PNG data")
            return mock_proc

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = render_drawio_to_png(drawio, output_path=custom)
            assert result == custom


class TestReplaceDrawioPlaceholders:
    def test_replace_with_extension(self):
        md = "# Diagram\n\n[drawio:arch.drawio]\n\nMore text"
        rendered = {"arch.drawio": Path(".media/arch.drawio.png")}
        result = replace_drawio_placeholders(md, rendered)
        assert "![arch](.media/arch.drawio.png)" in result
        assert "arch.drawio" in result  # source link

    def test_replace_without_extension(self):
        md = "# Diagram\n\n[drawio:arch]\n\nMore text"
        rendered = {"arch.drawio": Path(".media/arch.drawio.png")}
        result = replace_drawio_placeholders(md, rendered)
        assert "![arch](.media/arch.drawio.png)" in result

    def test_no_matching_placeholder(self):
        md = "# No diagrams here"
        rendered = {"other.drawio": Path(".media/other.drawio.png")}
        result = replace_drawio_placeholders(md, rendered)
        assert result == md
