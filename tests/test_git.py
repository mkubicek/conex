"""Tests for git versioning module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from confluence_export.git import (
    commit_export,
    commit_local_changes,
    ensure_repo,
    git_available,
)


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
        media = tmp_path / "media" / "img.png"
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
