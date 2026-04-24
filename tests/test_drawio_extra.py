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

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = render_drawio_to_png(drawio)
            assert result == drawio.with_suffix(".drawio.png")
            mock_run.assert_called_once()

    def test_render_failure(self, tmp_path, capsys):
        import subprocess
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "drawio")):
            result = render_drawio_to_png(drawio)
            assert result is None
            assert "render failed" in capsys.readouterr().err

    def test_custom_output_path(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        custom = tmp_path / "custom.png"

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.run"):
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
