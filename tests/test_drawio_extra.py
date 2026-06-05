"""Tests for draw.io detection, rendering, and placeholder replacement."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_export.drawio import (
    detect_drawio_macros,
    find_drawio_attachments,
    find_drawio_cli,
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

    def test_existing_png_reused_when_cli_missing(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"PNG data")

        with patch("confluence_export.drawio.find_drawio_cli", return_value=None):
            result = render_drawio_to_png(drawio)

        assert result == png

    def test_existing_png_directory_not_reused_when_cli_missing(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.mkdir()

        with patch("confluence_export.drawio.find_drawio_cli", return_value=None):
            result = render_drawio_to_png(drawio)

        assert result is None
        assert "CLI not found" in capsys.readouterr().err

    def test_existing_png_symlink_not_reused_when_cli_missing(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"PNG data")
        png = tmp_path / "test.drawio.png"
        png.symlink_to(outside)

        with patch("confluence_export.drawio.find_drawio_cli", return_value=None):
            result = render_drawio_to_png(drawio)

        assert result is None
        assert "CLI not found" in capsys.readouterr().err

    def test_render_replaces_symlink_output_without_touching_target(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        png = tmp_path / "test.drawio.png"
        png.symlink_to(outside)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0

        def fake_popen(*args, **kwargs):
            output = Path(args[0][args[0].index("--output") + 1])
            assert output != png
            output.write_bytes(b"fresh")
            return mock_proc

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = render_drawio_to_png(drawio)

        assert result == png
        assert outside.read_bytes() == b"outside"
        assert png.read_bytes() == b"fresh"
        assert not png.is_symlink()

    def test_force_render_cli_missing_keeps_existing_png(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"stale")

        with patch("confluence_export.drawio.find_drawio_cli", return_value=None):
            result = render_drawio_to_png(drawio, force=True)

        assert result == png
        assert png.read_bytes() == b"stale"

    def test_force_render_does_not_return_stale_existing_png(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"stale")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def fake_popen(*args, **kwargs):
            output = Path(args[0][args[0].index("--output") + 1])
            assert output != png
            assert png.read_bytes() == b"stale"
            output.write_bytes(b"fresh")
            return mock_proc

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = render_drawio_to_png(drawio, force=True)

        assert result == png
        assert png.read_bytes() == b"fresh"

    def test_force_render_waits_for_stable_output_before_replacing_existing_png(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"stale")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        render_path = None
        sleeps = 0

        def fake_popen(*args, **kwargs):
            nonlocal render_path
            render_path = Path(args[0][args[0].index("--output") + 1])
            render_path.write_bytes(b"partial")
            return mock_proc

        def fake_sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                assert png.read_bytes() == b"stale"
                render_path.write_bytes(b"complete")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch("confluence_export.drawio.time.sleep", side_effect=fake_sleep):
            result = render_drawio_to_png(drawio, force=True)

        assert result == png
        assert sleeps >= 1
        assert png.read_bytes() == b"complete"

    def test_force_render_failure_keeps_existing_png(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"stale")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", return_value=mock_proc):
            result = render_drawio_to_png(drawio, force=True)

        assert result == png
        assert png.read_bytes() == b"stale"
        assert "keeping previous PNG" in capsys.readouterr().err

    def test_failed_initial_render_removes_partial_png(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1

        def fake_popen(*args, **kwargs):
            output = Path(args[0][args[0].index("--output") + 1])
            output.write_bytes(b"partial")
            return mock_proc

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = render_drawio_to_png(drawio)

        assert result is None
        assert not png.exists()
        assert "no output" in capsys.readouterr().err

    def test_force_render_exec_failure_keeps_existing_png(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"stale")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=FileNotFoundError("vanished")):
            result = render_drawio_to_png(drawio, force=True)

        assert result == png
        assert png.read_bytes() == b"stale"
        err = capsys.readouterr().err
        assert "render failed" in err
        assert "keeping previous PNG" in err

    def test_force_render_oserror_keeps_existing_png_and_removes_temp(self, tmp_path, capsys):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        png = tmp_path / "test.drawio.png"
        png.write_bytes(b"stale")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=PermissionError("not executable")):
            result = render_drawio_to_png(drawio, force=True)

        assert result == png
        assert png.read_bytes() == b"stale"
        assert not list(tmp_path.glob(".drawio-*.png"))
        err = capsys.readouterr().err
        assert "render failed" in err
        assert "keeping previous PNG" in err

    def test_successful_render(self, tmp_path):
        drawio = tmp_path / "test.drawio"
        drawio.write_text("<xml/>")
        expected_png = drawio.with_suffix(".drawio.png")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def fake_popen(*args, **kwargs):
            output = Path(args[0][args[0].index("--output") + 1])
            output.write_bytes(b"PNG data")
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
            output = Path(args[0][args[0].index("--output") + 1])
            output.write_bytes(b"PNG data")
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
        render_path = None

        def fake_popen(*args, **kwargs):
            nonlocal render_path
            render_path = Path(args[0][args[0].index("--output") + 1])
            return mock_proc

        # File is created on the second sleep call, simulating drawio writing
        # the PNG mid-poll. By the next loop iteration the existence check fires.
        sleep_count = [0]
        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] == 2:
                render_path.write_bytes(b"PNG data")

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen), \
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
        render_path = None

        def fake_popen(*args, **kwargs):
            nonlocal render_path
            render_path = Path(args[0][args[0].index("--output") + 1])
            return mock_proc

        # First poll inside loop returns None (still running, no file yet);
        # second poll returns 0 (exited) AND simulates the late file write.
        poll_seq = iter([None, 0, 0, 0])
        def fake_poll():
            ret = next(poll_seq)
            if ret == 0 and render_path is not None:
                try:
                    if render_path.stat().st_size == 0:
                        render_path.write_bytes(b"PNG data")
                except FileNotFoundError:
                    pass
            return ret

        mock_proc.poll.side_effect = fake_poll

        with patch("confluence_export.drawio.find_drawio_cli", return_value="/usr/bin/drawio"), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch("confluence_export.drawio.time.sleep"):
            result = render_drawio_to_png(drawio)
            assert result == expected_png
