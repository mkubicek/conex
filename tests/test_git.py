"""Tests for git versioning module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from confluence_export.git import (
    _chunked_paths,
    commit_export,
    commit_local_changes,
    ensure_repo,
    git_available,
)


class TestChunkedPaths:
    def test_empty_input_yields_nothing(self):
        assert list(_chunked_paths([])) == []

    def test_single_small_path_one_batch(self):
        batches = list(_chunked_paths(["a.md"]))
        assert batches == [["a.md"]]

    def test_all_paths_fit_in_one_batch(self):
        paths = [f"page-{i}.md" for i in range(50)]
        batches = list(_chunked_paths(paths, max_bytes=10_000))
        assert batches == [paths]

    def test_splits_when_total_exceeds_budget(self):
        # Each path is ~50 bytes; 100 paths = 5000 bytes; budget 1500 → 4 batches
        paths = [f"some/deeply/nested/path/page-{i:03d}.md" for i in range(100)]
        batches = list(_chunked_paths(paths, max_bytes=1500))
        assert len(batches) >= 3
        # Round-trip: every path appears exactly once, in original order
        assert [p for batch in batches for p in batch] == paths
        # No batch exceeds the budget (each path counted with +1 overhead)
        for batch in batches:
            assert sum(len(p.encode("utf-8")) + 1 for p in batch) <= 1500

    def test_oversized_single_path_still_yielded(self):
        """A path larger than the budget cannot be split — yield it alone."""
        huge = "x" * 5000
        normal = "small.md"
        batches = list(_chunked_paths([huge, normal], max_bytes=1000))
        # Huge path gets its own batch even though it busts the budget;
        # alternative would be silently dropping it. Better to let git fail loudly.
        assert batches[0] == [huge]
        assert normal in batches[-1]

    def test_unicode_byte_length_respected(self):
        """Multi-byte UTF-8 chars count by encoded byte length, not codepoint count."""
        # Each "ä" is 2 bytes in UTF-8. 50 chars × 2 + ".md" = 103 bytes per path.
        paths = [("ä" * 50) + f"-{i}.md" for i in range(20)]
        batches = list(_chunked_paths(paths, max_bytes=300))
        # 300 / ~108 → ~2 paths per batch → ~10 batches
        assert len(batches) >= 5
        assert [p for batch in batches for p in batch] == paths


class TestGitAvailable:
    def test_found(self):
        with patch("confluence_export.git.shutil.which", return_value="/usr/bin/git"):
            assert git_available() is True

    def test_not_found(self):
        with patch("confluence_export.git.shutil.which", return_value=None):
            assert git_available() is False


class TestEnsureRepo:
    def test_already_a_repo(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        assert ensure_repo(tmp_path) is True

    def test_initializes_new_repo(self, tmp_path):
        assert ensure_repo(tmp_path) is True
        assert (tmp_path / ".git").is_dir()

    def test_sets_fallback_identity_on_init(self, tmp_path):
        ensure_repo(tmp_path)
        name = subprocess.run(
            ["git", "config", "user.name"], cwd=tmp_path, capture_output=True, text=True
        )
        assert name.stdout.strip() == "confluence-export"

    def test_does_not_overwrite_existing_identity(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Alice"], cwd=tmp_path, capture_output=True)
        ensure_repo(tmp_path)
        name = subprocess.run(
            ["git", "config", "user.name"], cwd=tmp_path, capture_output=True, text=True
        )
        assert name.stdout.strip() == "Alice"

    def test_inside_parent_repo(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        sub = tmp_path / "sub" / "dir"
        sub.mkdir(parents=True)
        assert ensure_repo(sub) is True
        # Should NOT create a nested .git
        assert not (sub / ".git").exists()


class TestCommitLocalChanges:
    def _init_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        return tmp_path

    def test_no_changes(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "file.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        assert commit_local_changes(tmp_path) is False

    def test_with_modifications(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "file.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        # Modify tracked file
        (tmp_path / "file.md").write_text("modified")

        assert commit_local_changes(tmp_path) is True

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
        assert "Local changes" in log.stdout

    def test_ignores_untracked_files(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "tracked.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        # Create untracked file only
        (tmp_path / "local-notes.txt").write_text("my notes")

        assert commit_local_changes(tmp_path) is False

    def test_skips_on_fresh_repo(self, tmp_path):
        """No crash or noise on a freshly initialized repo with no commits."""
        self._init_repo(tmp_path)
        assert commit_local_changes(tmp_path) is False


class TestCommitExport:
    def _init_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        # Need an initial commit for diff --cached to work
        (tmp_path / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        return tmp_path

    def test_commits_written_files(self, tmp_path):
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        media = tmp_path / ".media" / "img.png"
        media.parent.mkdir()
        media.write_bytes(b"\x89PNG")

        assert commit_export(tmp_path, [md, media], "TEST") is True

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
        assert "Export Confluence space TEST" in log.stdout

    def test_does_not_commit_other_files(self, tmp_path):
        self._init_repo(tmp_path)
        # A locally created file that should NOT be committed
        (tmp_path / "local-notes.txt").write_text("my notes")
        # An exporter-written file
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        commit_export(tmp_path, [md], "TEST")

        # local-notes.txt should still be untracked
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
        assert "local-notes.txt" in status.stdout
        assert "??" in status.stdout  # untracked marker

    def test_nothing_to_commit(self, tmp_path):
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        # First export commits the file
        commit_export(tmp_path, [md], "TEST")

        # Re-export with identical content — nothing to commit
        assert commit_export(tmp_path, [md], "TEST") is False

    def test_removes_deleted_page(self, tmp_path):
        """A previously exported page deleted upstream is removed from git."""
        self._init_repo(tmp_path)
        old = tmp_path / "OldPage.md"
        old.write_text("# Old")
        new = tmp_path / "NewPage.md"
        new.write_text("# New")
        subprocess.run(["git", "add", "OldPage.md", "NewPage.md"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Second export: OldPage no longer in Confluence, NewPage updated
        new.write_text("# New v2")
        commit_export(tmp_path, [new], "TEST")

        # OldPage should be removed from git
        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "OldPage.md" not in ls.stdout
        assert "NewPage.md" in ls.stdout

    def test_removes_renamed_page(self, tmp_path):
        """A page renamed upstream: old name removed, new name added."""
        self._init_repo(tmp_path)
        old = tmp_path / "Old-Name.md"
        old.write_text("# Page")
        subprocess.run(["git", "add", "Old-Name.md"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Renamed in Confluence — exporter writes new filename
        renamed = tmp_path / "New-Name.md"
        renamed.write_text("# Page")
        commit_export(tmp_path, [renamed], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "Old-Name.md" not in ls.stdout
        assert "New-Name.md" in ls.stdout

    def test_commit_message_contains_timestamp(self, tmp_path):
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        commit_export(tmp_path, [md], "NB")

        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=tmp_path, capture_output=True, text=True
        )
        msg = log.stdout.strip()
        assert "NB" in msg
        assert "UTC" in msg

    def test_fresh_repo_no_identity_needed(self, tmp_path):
        """ensure_repo sets fallback identity; commit works without global config."""
        ensure_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        assert commit_export(tmp_path, [md], "TEST") is True

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
        assert "Export Confluence space TEST" in log.stdout

    def test_metadata_files_survive_repeated_exports(self, tmp_path):
        """Files like .versions.json included in written_files are not removed."""
        self._init_repo(tmp_path)
        media = tmp_path / ".media"
        media.mkdir()
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        manifest = media / ".versions.json"
        manifest.write_text('{"img.png": 1}')

        # First export includes both md and manifest
        commit_export(tmp_path, [md, manifest], "TEST")

        # Second export with same files
        md.write_text("# Page v2")
        manifest.write_text('{"img.png": 2}')
        commit_export(tmp_path, [md, manifest], "TEST")

        # Manifest should still be tracked
        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert ".media/.versions.json" in ls.stdout

    def test_workspace_files_survive_reexport(self, tmp_path):
        """Files inside workspace/ directories are never removed as stale."""
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        ws = tmp_path / ".workspace"
        ws.mkdir()
        script = ws / "prep.py"
        script.write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Re-export: only md is in written_files, workspace file should survive
        md.write_text("# Page v2")
        commit_export(tmp_path, [md], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert ".workspace/prep.py" in ls.stdout
        assert "Page.md" in ls.stdout

    def test_commits_many_paths_without_argv_overflow(self, tmp_path):
        """Real-world large-space scenario: thousands of paths in one export.

        Without chunking this raises OSError(7) "Argument list too long" on
        macOS where ARG_MAX is 1 MiB. We synthesize a path list whose joined
        argv comfortably exceeds that limit (~3 MiB) so the chunker is the
        only thing standing between us and the bug.
        """
        self._init_repo(tmp_path)
        # 5000 files with ~100-char names → ~500 KB of paths alone, well past
        # the per-batch budget but still within disk reason for a unit test.
        files = []
        for i in range(5000):
            sub = tmp_path / f"section-{i // 100:03d}"
            sub.mkdir(exist_ok=True)
            f = sub / f"page-with-a-reasonably-long-name-{i:05d}.md"
            f.write_text(f"# Page {i}")
            files.append(f)

        assert commit_export(tmp_path, files, "BIG") is True

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        )
        # All 5000 should be tracked
        assert ls.stdout.count("\n") == 5000

    def test_removes_many_stale_files_without_argv_overflow(self, tmp_path):
        """Same chunking guard for `git rm` when thousands of pages get deleted upstream."""
        self._init_repo(tmp_path)
        # Create + commit 3000 files
        old_files = []
        for i in range(3000):
            f = tmp_path / f"deeply-nested-path-{i:05d}-with-some-extra-bytes.md"
            f.write_text(f"# Page {i}")
            old_files.append(f)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Re-export with only ONE file kept — the other 2999 are stale
        survivor = old_files[0]
        survivor.write_text("# Page 0 v2")
        assert commit_export(tmp_path, [survivor], "TEST") is True

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        )
        # Only the survivor should remain
        assert ls.stdout.strip() == survivor.name

    def test_removes_stale_file_with_non_ascii_path(self, tmp_path):
        """Paths with non-ASCII characters (umlauts etc.) are handled correctly."""
        self._init_repo(tmp_path)
        umlaut_dir = tmp_path / "Pascal-Spörri"
        umlaut_dir.mkdir()
        old = umlaut_dir / "old.md"
        old.write_text("# Old")
        new = tmp_path / "New.md"
        new.write_text("# New")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, capture_output=True)

        # Second export: only New.md remains
        new.write_text("# New v2")
        commit_export(tmp_path, [new], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "old.md" not in ls.stdout
        assert "New.md" in ls.stdout
