"""Tests for conex.drawio — pair detection, render_batch, CLI caching.

All tests mock subprocess.run and shutil.which so no real binary is needed.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from conex.models import Attachment, PageVersion
from conex.store.blobs import BlobStore


# ---------------------------------------------------------------------------
# Helpers to build test fixtures
# ---------------------------------------------------------------------------


def _att(
    title: str,
    *,
    media_type: str = "application/octet-stream",
    version_created_at: str = "2024-01-01T00:00:00Z",
    version_number: int = 1,
) -> Attachment:
    """Build an Attachment with given title and version timestamp."""
    return Attachment(
        id=f"att-{title}",
        title=title,
        media_type=media_type,
        version=PageVersion(
            number=version_number,
            created_at=version_created_at,
        ),
    )


# ---------------------------------------------------------------------------
# find_drawio_pairs — pair detection
# ---------------------------------------------------------------------------


class TestFindDrawioPairs:
    def _import(self):
        from conex.drawio import find_drawio_pairs
        return find_drawio_pairs

    def test_xml_with_fresh_png_sibling(self):
        """PNG sibling with newer timestamp is paired and marked fresh."""
        find_drawio_pairs = self._import()
        xml = _att(
            "diagram.drawio",
            media_type="application/x-drawio",
            version_created_at="2024-01-01T10:00:00Z",
            version_number=5,
        )
        png = _att(
            "diagram.drawio.png",
            media_type="image/png",
            version_created_at="2024-01-02T10:00:00Z",
            version_number=2,
        )
        pairs = find_drawio_pairs([xml, png])
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.xml is xml
        assert pair.png is png
        assert pair.preview_fresh is True

    def test_xml_with_stale_png_sibling(self):
        """PNG sibling with OLDER timestamp than xml is paired but not fresh."""
        find_drawio_pairs = self._import()
        # xml version 5 but created_at is NEWER than png version 2
        xml = _att(
            "diagram.drawio",
            media_type="application/x-drawio",
            version_created_at="2024-06-01T12:00:00Z",
            version_number=5,
        )
        png = _att(
            "diagram.drawio.png",
            media_type="image/png",
            version_created_at="2024-01-01T08:00:00Z",
            version_number=2,
        )
        pairs = find_drawio_pairs([xml, png])
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.xml is xml
        assert pair.png is png
        assert pair.preview_fresh is False

    def test_version_number_red_herring(self):
        """Version numbers do NOT determine freshness — only created_at timestamps.

        png version=2, xml version=5 but png timestamp is OLDER → stale.
        """
        find_drawio_pairs = self._import()
        xml = _att(
            "flow.drawio",
            media_type="application/x-drawio",
            version_created_at="2024-05-15T09:00:00Z",
            version_number=5,
        )
        png = _att(
            "flow.drawio.png",
            media_type="image/png",
            version_created_at="2024-01-01T00:00:00Z",  # older timestamp
            version_number=2,
        )
        pairs = find_drawio_pairs([xml, png])
        assert len(pairs) == 1
        assert pairs[0].preview_fresh is False  # timestamp wins, not version number

    def test_xml_without_png_sibling(self):
        """A drawio source with no matching PNG has preview_fresh=False."""
        find_drawio_pairs = self._import()
        xml = _att("arch.drawio", media_type="application/x-drawio")
        pairs = find_drawio_pairs([xml])
        assert len(pairs) == 1
        assert pairs[0].xml is xml
        assert pairs[0].png is None
        assert pairs[0].preview_fresh is False

    def test_png_equal_timestamp_is_fresh(self):
        """PNG with same created_at as xml is considered fresh (>=)."""
        find_drawio_pairs = self._import()
        ts = "2024-03-10T15:00:00Z"
        xml = _att("same.drawio", media_type="application/x-drawio", version_created_at=ts)
        png = _att("same.drawio.png", media_type="image/png", version_created_at=ts)
        pairs = find_drawio_pairs([xml, png])
        assert pairs[0].preview_fresh is True

    def test_png_not_returned_as_separate_pair(self):
        """PNG preview attachments are NOT returned as independent pairs."""
        find_drawio_pairs = self._import()
        xml = _att("d.drawio", media_type="application/x-drawio")
        png = _att("d.drawio.png", media_type="image/png")
        pairs = find_drawio_pairs([xml, png])
        # Only one pair; the png attachment doesn't become its own pair
        assert len(pairs) == 1

    def test_multiple_diagrams(self):
        """Multiple drawio sources each get their own pair."""
        find_drawio_pairs = self._import()
        xml1 = _att("a.drawio", media_type="application/x-drawio", version_created_at="2024-01-01T00:00:00Z")
        xml2 = _att("b.drawio", media_type="application/x-drawio", version_created_at="2024-01-01T00:00:00Z")
        png1 = _att("a.drawio.png", media_type="image/png", version_created_at="2024-06-01T00:00:00Z")
        # b has no matching png
        pairs = find_drawio_pairs([xml1, xml2, png1])
        names = {p.xml.title for p in pairs}
        assert names == {"a.drawio", "b.drawio"}
        pair_a = next(p for p in pairs if p.xml.title == "a.drawio")
        pair_b = next(p for p in pairs if p.xml.title == "b.drawio")
        assert pair_a.png is png1
        assert pair_a.preview_fresh is True
        assert pair_b.png is None

    def test_empty_attachments(self):
        """No attachments -> empty result."""
        find_drawio_pairs = self._import()
        assert find_drawio_pairs([]) == []


# ---------------------------------------------------------------------------
# render_batch — absent CLI
# ---------------------------------------------------------------------------


class TestRenderBatchAbsentCli:
    def _reset_cache(self):
        """Reset the module-level CLI cache before each test."""
        import conex.drawio as m
        m._DRAWIO_CLI = None

    def test_absent_cli_returns_empty_and_warns_once(self, tmp_path):
        """When CLI is absent, return {} with exactly one warning."""
        self._reset_cache()
        blobs = BlobStore(tmp_path)
        xml_data = b"<mxGraphModel/>"
        digest = blobs.add_bytes(xml_data)

        with patch("conex.drawio.shutil.which", return_value=None):
            import conex.drawio as m
            m._DRAWIO_CLI = None  # force re-probe

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = m.render_batch({"diagram.drawio": digest}, blobs)

        assert result == {}
        assert len(caught) == 1
        assert "draw.io" in str(caught[0].message).lower() or "drawio" in str(caught[0].message).lower()

    def test_absent_cli_cached_no_re_probe(self, tmp_path):
        """which() is NOT called a second time once the absence is cached."""
        import conex.drawio as m
        m._DRAWIO_CLI = None  # start fresh

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")

        call_count = 0

        def counting_which(name):
            nonlocal call_count
            call_count += 1
            return None

        with patch("conex.drawio.shutil.which", side_effect=counting_which):
            # first call
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                m.render_batch({"a.drawio": digest}, blobs)
            # second call — should NOT re-probe
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                m.render_batch({"b.drawio": digest}, blobs)

        # which() called at most once (for "drawio"); after the cache is
        # set to False no further calls happen.
        assert call_count <= 2  # one probe for "drawio" and one for "draw.io"

    def test_absent_cli_empty_xml_blobs_returns_empty(self, tmp_path):
        """Empty xml_blobs input always returns {} without touching which()."""
        import conex.drawio as m
        blobs = BlobStore(tmp_path)
        with patch("conex.drawio.shutil.which") as mock_which:
            result = m.render_batch({}, blobs)
        assert result == {}
        mock_which.assert_not_called()


# ---------------------------------------------------------------------------
# render_batch — folder-mode success
# ---------------------------------------------------------------------------


class TestRenderBatchFolderMode:
    def _reset_cache(self, cli_path: str = "/usr/bin/drawio"):
        import conex.drawio as m
        m._DRAWIO_CLI = cli_path

    def test_folder_mode_success_returns_digests(self, tmp_path):
        """Folder-mode success: correct argv, PNGs stored, name->digest returned."""
        self._reset_cache()
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        xml_data = b"<mxGraphModel><root/></mxGraphModel>"
        digest = blobs.add_bytes(xml_data)

        # We need to make the folder-mode run produce a PNG in the output dir.
        # We'll capture the argv and create the file ourselves.
        captured_argvs: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured_argvs.append(argv)
            # First call is folder-mode: create a fake png in output dir
            # argv[-2] is --output path, argv[-1] is input folder
            out_dir = Path(argv[argv.index("--output") + 1])
            # Find staged xml files and write matching pngs
            in_dir = Path(argv[-1])
            for xml_file in in_dir.glob("drawio-src-*"):
                png_file = out_dir / (xml_file.name + ".png")
                png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = m.render_batch({"diagram.drawio": digest}, blobs)

        assert len(result) == 1
        assert "diagram.drawio" in result
        # Verify the PNG was stored in the blob store
        stored_digest = result["diagram.drawio"]
        assert blobs.has(stored_digest)
        # No warnings
        assert len(caught) == 0

    def test_folder_mode_argv_has_long_flags(self, tmp_path):
        """Folder-mode invocation uses --export --format png --no-sandbox (long flags)."""
        self._reset_cache("/usr/local/bin/drawio")
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")

        argvs: list[list[str]] = []

        def capturing_run(argv, **kwargs):
            argvs.append(list(argv))
            # Create a PNG to simulate success
            out_dir = Path(argv[argv.index("--output") + 1])
            in_dir = Path(argv[-1])
            for xml_file in in_dir.glob("drawio-src-*"):
                (out_dir / (xml_file.name + ".png")).write_bytes(b"\x89PNG" + b"\x00" * 20)
            return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=capturing_run):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                m.render_batch({"x.drawio": digest}, blobs)

        assert len(argvs) >= 1
        folder_argv = argvs[0]
        assert "--export" in folder_argv
        assert "--format" in folder_argv
        assert "png" in folder_argv
        assert "--no-sandbox" in folder_argv
        assert "--output" in folder_argv
        # No short flags like -f, -o etc.
        assert "-f" not in folder_argv
        assert "-o" not in folder_argv
        assert "-e" not in folder_argv


# ---------------------------------------------------------------------------
# render_batch — folder-mode failure → per-file fallback
# ---------------------------------------------------------------------------


class TestRenderBatchPerFileFallback:
    def _reset_cache(self, cli_path: str = "/usr/bin/drawio"):
        import conex.drawio as m
        m._DRAWIO_CLI = cli_path

    def test_folder_failure_triggers_per_file_fallback(self, tmp_path):
        """Non-zero folder exit triggers per-file fallback with same flags."""
        self._reset_cache()
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")

        argvs: list[list[str]] = []
        call_index = 0

        def fake_run(argv, **kwargs):
            nonlocal call_index
            argvs.append(list(argv))
            if call_index == 0:
                # Folder-mode fails
                call_index += 1
                return MagicMock(returncode=1)
            else:
                # Per-file succeeds
                out_path = Path(argv[argv.index("--output") + 1])
                out_path.write_bytes(b"\x89PNG" + b"\x00" * 20)
                call_index += 1
                return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = m.render_batch({"d.drawio": digest}, blobs)

        assert "d.drawio" in result
        # There must be at least 2 calls (folder + per-file)
        assert len(argvs) >= 2
        # Verify per-file argv has the correct long flags
        per_file_argv = argvs[1]
        assert "--export" in per_file_argv
        assert "--format" in per_file_argv
        assert "png" in per_file_argv
        assert "--no-sandbox" in per_file_argv
        assert "--output" in per_file_argv

    def test_folder_no_output_triggers_per_file_fallback(self, tmp_path):
        """Folder-mode with return code 0 but no PNGs triggers per-file fallback."""
        self._reset_cache()
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")

        call_index = 0

        def fake_run(argv, **kwargs):
            nonlocal call_index
            if call_index == 0:
                call_index += 1
                # returncode=0 but no files created in output dir
                return MagicMock(returncode=0)
            else:
                out_path = Path(argv[argv.index("--output") + 1])
                out_path.write_bytes(b"\x89PNG" + b"\x00" * 20)
                call_index += 1
                return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = m.render_batch({"e.drawio": digest}, blobs)

        # Per-file fallback should have produced a result
        assert "e.drawio" in result

    def test_per_file_argv_structure(self, tmp_path):
        """Per-file fallback uses same long flags, file as last arg."""
        self._reset_cache("/opt/drawio")
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")

        argvs: list[list[str]] = []
        call_index = 0

        def fake_run(argv, **kwargs):
            nonlocal call_index
            argvs.append(list(argv))
            if call_index == 0:
                call_index += 1
                return MagicMock(returncode=1)
            else:
                out_path = Path(argv[argv.index("--output") + 1])
                out_path.write_bytes(b"\x89PNG" + b"\x00" * 20)
                call_index += 1
                return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                m.render_batch({"test.drawio": digest}, blobs)

        assert len(argvs) >= 2
        per_file = argvs[1]
        # argv[0] is the CLI; last element is the input file
        assert per_file[0] == "/opt/drawio"
        assert per_file[1] == "--export"
        assert "--format" in per_file
        idx = per_file.index("--format")
        assert per_file[idx + 1] == "png"
        assert "--no-sandbox" in per_file

    def test_partial_per_file_failure_partial_result_and_warnings(self, tmp_path):
        """Per-file mode: partial failure produces partial result map + warnings."""
        self._reset_cache()
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest_a = blobs.add_bytes(b"<mxGraphModel id='a'/>")
        digest_b = blobs.add_bytes(b"<mxGraphModel id='b'/>")

        call_index = 0

        def fake_run(argv, **kwargs):
            nonlocal call_index
            if call_index == 0:
                # Folder fails
                call_index += 1
                return MagicMock(returncode=1)
            elif call_index == 1:
                # First per-file succeeds
                out_path = Path(argv[argv.index("--output") + 1])
                out_path.write_bytes(b"\x89PNG" + b"\x00" * 20)
                call_index += 1
                return MagicMock(returncode=0)
            else:
                # Second per-file fails
                call_index += 1
                return MagicMock(returncode=1)

        xml_blobs = {"a.drawio": digest_a, "b.drawio": digest_b}
        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = m.render_batch(xml_blobs, blobs)

        # Exactly one success out of two
        assert len(result) == 1
        # At least one warning for the failure
        assert len(caught) >= 1
        warning_text = " ".join(str(w.message) for w in caught)
        assert any(
            name in warning_text for name in ["a.drawio", "b.drawio"]
        )


# ---------------------------------------------------------------------------
# render_batch — tmp dir invariant (I4)
# ---------------------------------------------------------------------------


class TestRenderBatchTmpDir:
    def _reset_cache(self, cli_path: str = "/usr/bin/drawio"):
        import conex.drawio as m
        m._DRAWIO_CLI = cli_path

    def test_tmp_files_under_conex_tmp(self, tmp_path):
        """All intermediate files are staged under .conex/tmp/ (I4)."""
        self._reset_cache()
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")
        conex_tmp = tmp_path / ".conex" / "tmp"

        accessed_paths: list[str] = []

        def fake_run(argv, **kwargs):
            # Record all paths mentioned in argv
            for arg in argv:
                accessed_paths.append(arg)
            # Success with a PNG
            out_path = Path(argv[argv.index("--output") + 1])
            if out_path.is_dir():
                # folder mode
                return MagicMock(returncode=1)  # force per-file fallback for simplicity
            else:
                out_path.write_bytes(b"\x89PNG" + b"\x00" * 20)
                return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                m.render_batch({"diag.drawio": digest}, blobs)

        conex_tmp_str = str(conex_tmp)
        # Every path that is inside the tmp tree should be under .conex/tmp
        for p in accessed_paths:
            if p.startswith(str(tmp_path)) and p != str(tmp_path):
                # Must be under .conex/tmp
                assert p.startswith(conex_tmp_str), (
                    f"File {p!r} is not under .conex/tmp/"
                )


# ---------------------------------------------------------------------------
# find_drawio_pairs — media-type-based detection (MAJOR 2 regression test)
# ---------------------------------------------------------------------------


class TestFindDrawioPairsMediaType:
    """Verify v1 PORT semantics: media_type-based source detection is independent."""

    def _import(self):
        from conex.drawio import find_drawio_pairs
        return find_drawio_pairs

    def test_media_type_only_source_detected(self):
        """Attachment with media_type='application/x-drawio' but no .drawio title is a source."""
        find_drawio_pairs = self._import()
        att = _att("MyDiagram", media_type="application/x-drawio")
        pairs = find_drawio_pairs([att])
        assert len(pairs) == 1
        assert pairs[0].xml is att

    def test_media_type_containing_drawio_detected(self):
        """Attachment whose media_type contains 'drawio' (but is not the exact value) is a source."""
        find_drawio_pairs = self._import()
        att = _att("Untitled", media_type="application/vnd.jgraph.drawio")
        pairs = find_drawio_pairs([att])
        assert len(pairs) == 1
        assert pairs[0].xml is att

    def test_media_type_only_source_with_png_sibling(self):
        """Media-type-only source pairs with its <title>.png sibling."""
        find_drawio_pairs = self._import()
        src = _att(
            "MyDiagram",
            media_type="application/x-drawio",
            version_created_at="2024-01-01T00:00:00Z",
        )
        png = _att(
            "MyDiagram.png",
            media_type="image/png",
            version_created_at="2024-06-01T00:00:00Z",
        )
        pairs = find_drawio_pairs([src, png])
        assert len(pairs) == 1
        assert pairs[0].xml is src
        assert pairs[0].png is png
        assert pairs[0].preview_fresh is True

    def test_title_endswith_drawio_without_media_type(self):
        """Title ending .drawio is still a source even without a drawio media_type."""
        find_drawio_pairs = self._import()
        att = _att("diagram.drawio", media_type="application/octet-stream")
        pairs = find_drawio_pairs([att])
        assert len(pairs) == 1

    def test_xml_source_png_pairing(self):
        """.xml-named source paired with '<title>.png' sibling."""
        find_drawio_pairs = self._import()
        src = _att(
            "diagram.xml",
            media_type="application/x-drawio",
            version_created_at="2024-03-01T00:00:00Z",
        )
        png = _att(
            "diagram.xml.png",
            media_type="image/png",
            version_created_at="2024-04-01T00:00:00Z",
        )
        pairs = find_drawio_pairs([src, png])
        assert len(pairs) == 1
        assert pairs[0].xml is src
        assert pairs[0].png is png
        assert pairs[0].preview_fresh is True


# ---------------------------------------------------------------------------
# render_batch — folder-mode produces unmappable PNGs → per-file fallback
# ---------------------------------------------------------------------------


class TestRenderBatchFolderUnmappableFallback:
    """Verify MAJOR 1 fix: folder-mode exit 0 but non-matching PNG names -> fallback."""

    def _reset_cache(self, cli_path: str = "/usr/bin/drawio"):
        import conex.drawio as m
        m._DRAWIO_CLI = cli_path

    def test_folder_unmappable_png_triggers_per_file_fallback(self, tmp_path):
        """Folder returns 0 and writes a PNG under a non-matching name; per-file runs."""
        self._reset_cache()
        import conex.drawio as m

        blobs = BlobStore(tmp_path)
        digest = blobs.add_bytes(b"<mxGraphModel/>")

        argvs: list[list[str]] = []
        call_index = 0

        def fake_run(argv, **kwargs):
            nonlocal call_index
            argvs.append(list(argv))
            if call_index == 0:
                # Folder mode: exit 0, writes a PNG but under the WRONG name
                # (drawio stem-replacement instead of appending .png)
                call_index += 1
                out_dir = Path(argv[argv.index("--output") + 1])
                # Write a PNG with a non-matching name to simulate unknown naming scheme
                (out_dir / "totally-different-name.png").write_bytes(b"\x89PNG" + b"\x00" * 20)
                return MagicMock(returncode=0)
            else:
                # Per-file mode: succeeds
                out_path = Path(argv[argv.index("--output") + 1])
                out_path.write_bytes(b"\x89PNG" + b"\x00" * 20)
                call_index += 1
                return MagicMock(returncode=0)

        with patch("conex.drawio.subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = m.render_batch({"diagram.drawio": digest}, blobs)

        # Per-file fallback must have run (at least 2 subprocess calls)
        assert len(argvs) >= 2, "per-file fallback should have been invoked"
        # Result must be populated (per-file succeeded)
        assert "diagram.drawio" in result, "per-file fallback must produce the result"
