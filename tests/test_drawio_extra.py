"""Tests for draw.io detection, storage inspection, and rendering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_export.drawio import (
    detect_drawio_macros,
    find_drawio_attachment,
    find_drawio_attachments,
    find_drawio_cli,
    find_drawio_macro_refs,
    render_drawio_to_png,
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

    def test_finds_inc_drawio_names(self):
        html = (
            '<ac:structured-macro ac:name="inc-drawio">'
            '<ac:parameter ac:name="diagramName">included_arch</ac:parameter>'
            '</ac:structured-macro>'
        )
        assert detect_drawio_macros(html) == ["included_arch"]

    def test_macro_refs_preserve_macro_name(self):
        html = (
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
            '</ac:structured-macro>'
            '<ac:structured-macro ac:name="drawio-sketch">'
            '<ac:parameter ac:name="diagramName">sketch</ac:parameter>'
            '</ac:structured-macro>'
        )
        refs = find_drawio_macro_refs(html)
        assert [(r.macro_name, r.diagram_name) for r in refs] == [
            ("drawio", "arch"),
            ("drawio-sketch", "sketch"),
        ]

    def test_find_attachment_from_macro_name_without_extension(self):
        attachments = [Attachment(id="1", title="arch.drawio", media_type="")]
        assert find_drawio_attachment(attachments, "arch") == attachments[0]


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

    def test_popen_raises_file_not_found(self, tmp_path, capsys):
        """find_drawio_cli returned a path but exec failed (vanished or perm error)."""
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=FileNotFoundError("no such file")):
            result = render_drawio_to_png(drawio)
            assert result is None
            assert "render failed" in capsys.readouterr().err

    def test_polls_multiple_times_until_file_appears(self, tmp_path):
        """File doesn't appear immediately — exercises the sleep + retry path."""
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        expected_png = drawio.with_suffix(".drawio.png")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # never exits on its own

        # File is created on the second sleep call, simulating drawio writing
        # the PNG mid-poll. By the next loop iteration the existence check fires.
        sleep_count = [0]
        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] == 2:
                expected_png.write_bytes(b"PNG data")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("confluence_export.drawio.time.sleep", side_effect=fake_sleep):
            result = render_drawio_to_png(drawio)
            assert result == expected_png
            assert sleep_count[0] >= 2
            mock_proc.kill.assert_called()  # finally block reaped the hung proc

    def test_render_timeout_kills_process(self, tmp_path, capsys):
        """drawio truly hangs without producing output — deadline triggers cleanup."""
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # never exits

        # Force the deadline check to flip from "in time" to "past deadline"
        # without waiting 120s of wall clock.
        times = iter([0.0, 0.0, 1000.0])  # third call is past deadline
        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("confluence_export.drawio.time.monotonic", side_effect=lambda: next(times)), \
             patch("confluence_export.drawio.time.sleep"):
            result = render_drawio_to_png(drawio)
            assert result is None
            assert "no output" in capsys.readouterr().err
            mock_proc.kill.assert_called()

    def test_file_appears_after_process_exit(self, tmp_path):
        """Process exits without file in the loop, but file lands during finally cleanup."""
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        expected_png = drawio.with_suffix(".drawio.png")

        mock_proc = MagicMock()

        # First poll inside loop returns None (still running, no file yet);
        # second poll returns 0 (exited) AND simulates the late file write.
        poll_seq = iter([None, 0, 0, 0])
        def fake_poll():
            ret = next(poll_seq)
            if ret == 0 and not expected_png.exists():
                expected_png.write_bytes(b"PNG data")
            return ret

        mock_proc.poll.side_effect = fake_poll

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("confluence_export.drawio.time.sleep"):
            result = render_drawio_to_png(drawio)
            assert result == expected_png
