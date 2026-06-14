"""Tests for conex.gitio — thin git layer.

Coverage targets (per SPEC-V2.md §gitio.py):
- ensure_repo: fresh init sets fallback identity + .gitignore; existing
  identity untouched; .gitignore entry added when absent / not duplicated
- commit_user_changes: stages tracked edits only; untracked file NOT committed;
  force-added .conex path unstaged; no commit on fresh repo (no commits yet)
- commit_export: stages exact written+deleted delta; dirty unrelated tracked
  file must NOT enter the export commit; empty delta -> False + no commit;
  chunking with many paths (>1000); .conex never committed
- missing git binary -> GitError raised
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# Bring conex.gitio into scope via PYTHONPATH (set by the test runner).
from conex.errors import GitError
from conex import gitio


# ---------------------------------------------------------------------------
# Duck-typed BuildResult stand-in (build.py is built in parallel)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _BuildResult:
    """Minimal duck-type of conex.build.BuildResult for tests."""
    written: list[Path] = dataclasses.field(default_factory=list)
    deleted: list[Path] = dataclasses.field(default_factory=list)
    skipped: int = 0
    moved: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, name: str = "test", email: str = "test@example.com") -> None:
    """Init a git repo in path with an explicit identity."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", name], cwd=path, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", email], cwd=path, capture_output=True, check=True
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )


def _get_config(path: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "config", "--local", key],
        cwd=path, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _make_commit(repo: Path, filename: str = "a.txt", content: str = "init") -> None:
    """Create a file, stage it, and commit to give the repo at least one commit."""
    f = repo / filename
    f.write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", "initial")


def _log_messages(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _staged_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


# ---------------------------------------------------------------------------
# ensure_repo
# ---------------------------------------------------------------------------


class TestEnsureRepo:
    def test_fresh_init_creates_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        # Not a git repo yet — need to check (rev-parse fails outside any repo).
        result = gitio.ensure_repo(root)
        assert result is True
        assert (root / ".git").exists()

    def test_fresh_init_sets_fallback_identity(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        gitio.ensure_repo(root)
        name = _get_config(root, "user.name")
        email = _get_config(root, "user.email")
        assert name == "conex"
        assert email == "conex@localhost"

    def test_fresh_init_adds_gitignore_entry(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        gitio.ensure_repo(root)
        gitignore = root / ".gitignore"
        assert gitignore.exists()
        assert ".conex/" in gitignore.read_text(encoding="utf-8").splitlines()

    def test_existing_repo_identity_untouched(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root, name="original-user", email="original@example.com")
        gitio.ensure_repo(root)
        assert _get_config(root, "user.name") == "original-user"
        assert _get_config(root, "user.email") == "original@example.com"

    def test_existing_repo_returns_true(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        assert gitio.ensure_repo(root) is True

    def test_gitignore_entry_not_duplicated(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        gitio.ensure_repo(root)
        gitio.ensure_repo(root)  # second call
        content = (root / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".conex/") == 1

    def test_gitignore_appended_to_existing_file(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        gitio.ensure_repo(root)
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "*.pyc" in lines
        assert ".conex/" in lines

    def test_gitignore_already_has_entry(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        (root / ".gitignore").write_text(".conex/\n", encoding="utf-8")
        gitio.ensure_repo(root)
        count = (root / ".gitignore").read_text(encoding="utf-8").count(".conex/")
        assert count == 1

    def test_missing_git_binary_raises_git_error(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        with mock.patch("conex.gitio.shutil.which", return_value=None):
            with pytest.raises(GitError):
                gitio.ensure_repo(root)

    def test_creates_root_if_absent(self, tmp_path: Path) -> None:
        root = tmp_path / "brand" / "new"
        assert not root.exists()
        gitio.ensure_repo(root)
        assert root.exists()
        assert (root / ".git").exists()


# ---------------------------------------------------------------------------
# commit_user_changes
# ---------------------------------------------------------------------------


class TestCommitUserChanges:
    def test_no_commit_on_fresh_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        # Fresh repo has no commits.
        result = gitio.commit_user_changes(root)
        assert result is False

    def test_commits_tracked_modifications(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        (root / "a.txt").write_text("modified", encoding="utf-8")
        result = gitio.commit_user_changes(root)
        assert result is True
        msgs = _log_messages(root)
        assert msgs[0] == "Local changes before export"

    def test_untracked_file_not_committed(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        (root / "untracked.txt").write_text("hello", encoding="utf-8")
        result = gitio.commit_user_changes(root)
        assert result is False
        # The untracked file must not appear in any commit.
        log = subprocess.run(
            ["git", "log", "--all", "--name-only", "--format="],
            cwd=root, capture_output=True, text=True, check=True,
        )
        assert "untracked.txt" not in log.stdout

    def test_no_changes_returns_false(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        result = gitio.commit_user_changes(root)
        assert result is False

    def test_force_added_conex_path_unstaged(self, tmp_path: Path) -> None:
        """A .conex path that was force-added must be unstaged before commit."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        # Also modify a real tracked file so there IS something to commit.
        (root / "a.txt").write_text("changed", encoding="utf-8")
        # Force-add a .conex secret.
        conex_dir = root / ".conex"
        conex_dir.mkdir()
        secret = conex_dir / "config.json"
        secret.write_text('{"token":"secret"}', encoding="utf-8")
        _git(root, "add", "-f", ".conex/config.json")
        # The .conex file is now staged; commit_user_changes must unstage it.
        result = gitio.commit_user_changes(root)
        assert result is True
        # Verify .conex/config.json is not in the commit.
        show = subprocess.run(
            ["git", "show", "--name-only", "--format="],
            cwd=root, capture_output=True, text=True, check=True,
        )
        assert ".conex/config.json" not in show.stdout

    def test_missing_git_binary_raises_git_error(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        with mock.patch("conex.gitio._git_available", return_value=False):
            with pytest.raises(GitError):
                gitio.commit_user_changes(root)


# ---------------------------------------------------------------------------
# commit_export
# ---------------------------------------------------------------------------


class TestCommitExport:
    def test_commits_written_files(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        page = root / "Page.md"
        page.write_text("# Page", encoding="utf-8")
        result = _BuildResult(written=[page])
        committed = gitio.commit_export(root, result, "Export space TEST")
        assert committed is True
        msgs = _log_messages(root)
        assert msgs[0] == "Export space TEST"

    def test_force_adds_past_user_gitignore(self, tmp_path: Path) -> None:
        """A user .gitignore pattern that matches an exported file must not abort
        the export commit — conex output is authoritative and force-added."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        (root / ".gitignore").write_text("*.md\n", encoding="utf-8")
        page = root / "Page.md"
        page.write_text("# Page", encoding="utf-8")
        result = _BuildResult(written=[page])
        committed = gitio.commit_export(root, result, "Export with gitignore")
        assert committed is True
        assert "Page.md" in _git(root, "ls-files").stdout

    def test_export_commits_the_gitignore(self, tmp_path: Path) -> None:
        """The conex-managed .gitignore is committed by the export, so the
        working tree is clean afterwards (not left perpetually untracked)."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        gitio.ensure_repo(root)  # creates the .gitignore with .conex/
        assert ".gitignore" not in _git(root, "ls-files").stdout  # untracked pre-export
        page = root / "Page.md"
        page.write_text("# Page", encoding="utf-8")
        committed = gitio.commit_export(root, _BuildResult(written=[page]), "Export")
        assert committed is True
        assert ".gitignore" in _git(root, "ls-files").stdout, ".gitignore must be committed"
        # Working tree is clean (nothing left untracked/modified).
        assert _git(root, "status", "--porcelain").stdout.strip() == ""

    def test_export_root_under_dotconex_ancestor_still_stages(self, tmp_path: Path) -> None:
        """Regression: an export rooted under an ancestor dir named .conex (e.g.
        ~/.conex/myspace) must still stage its files — the .conex exclusion is
        relative to the export root, not the absolute path."""
        root = tmp_path / ".conex" / "myspace"
        root.mkdir(parents=True)
        _init_repo(root)
        page = root / "Page.md"
        page.write_text("# Page", encoding="utf-8")
        result = _BuildResult(written=[page])
        committed = gitio.commit_export(root, result, "Export under .conex ancestor")
        assert committed is True  # was False (nothing staged) before the fix
        assert "Page.md" in _git(root, "ls-files").stdout

    def test_commits_deleted_files(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        # Commit an initial file then delete it.
        page = root / "Old.md"
        page.write_text("old", encoding="utf-8")
        _git(root, "add", "Old.md")
        _git(root, "commit", "-m", "initial")
        page.unlink()
        result = _BuildResult(deleted=[page])
        committed = gitio.commit_export(root, result, "Export: prune Old")
        assert committed is True

    def test_empty_delta_returns_false(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        result = _BuildResult()
        committed = gitio.commit_export(root, result, "Export space TEST")
        assert committed is False

    def test_empty_delta_no_commit_created(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)
        initial_log = _log_messages(root)
        result = _BuildResult()
        gitio.commit_export(root, result, "should not appear")
        after_log = _log_messages(root)
        assert after_log == initial_log

    def test_dirty_unrelated_file_not_in_export_commit(self, tmp_path: Path) -> None:
        """A tracked file modified outside the build must NOT enter the export commit."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        # Commit two files.
        dirty = root / "user.txt"
        dirty.write_text("user content", encoding="utf-8")
        _git(root, "add", "user.txt")
        _git(root, "commit", "-m", "initial")
        # Modify the tracked file (simulate user edit) without staging it.
        dirty.write_text("modified by user", encoding="utf-8")
        # Export writes a different file.
        export_page = root / "Space" / "Page.md"
        export_page.parent.mkdir()
        export_page.write_text("# Page", encoding="utf-8")
        result = _BuildResult(written=[export_page])
        gitio.commit_export(root, result, "Export space")
        # The export commit must contain only Space/Page.md, not user.txt.
        show = subprocess.run(
            ["git", "show", "--name-only", "--format="],
            cwd=root, capture_output=True, text=True, check=True,
        )
        committed_files = [f for f in show.stdout.splitlines() if f]
        assert "user.txt" not in committed_files
        assert any("Page.md" in f for f in committed_files)

    def test_conex_paths_not_committed(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        conex_dir = root / ".conex"
        conex_dir.mkdir()
        secret = conex_dir / "state.json"
        secret.write_text("{}", encoding="utf-8")
        # .conex path listed in written — must be excluded from the commit.
        result = _BuildResult(written=[secret])
        committed = gitio.commit_export(root, result, "Export")
        assert committed is False  # nothing actually staged

    def test_chunking_many_paths(self, tmp_path: Path) -> None:
        """Verify chunking logic fires when there are more than 1000 paths."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        # Create 1500 small files.
        pages_dir = root / "pages"
        pages_dir.mkdir()
        written: list[Path] = []
        for i in range(1500):
            p = pages_dir / f"page_{i:04d}.md"
            p.write_text(f"# Page {i}", encoding="utf-8")
            written.append(p)
        # Monkeypatch chunk constant to a small value to force many batches.
        orig = gitio._CHUNK_SIZE_BYTES
        calls: list[list[str]] = []
        orig_run_git = gitio._run_git

        def tracking_run_git(repo_dir: Path, *args: str, **kwargs: Any) -> Any:
            if args and args[0] == "add":
                calls.append(list(args))
            return orig_run_git(repo_dir, *args, **kwargs)

        try:
            gitio._CHUNK_SIZE_BYTES = 200  # tiny: forces many batches
            with mock.patch("conex.gitio._run_git", side_effect=tracking_run_git):
                result = _BuildResult(written=written)
                gitio.commit_export(root, result, "Export large space")
        finally:
            gitio._CHUNK_SIZE_BYTES = orig

        # With 1500 files at ~20 bytes each and chunk=200, we expect >1 add call.
        add_calls = [c for c in calls if c[0] == "add" and "--" in c]
        assert len(add_calls) > 1, (
            f"Expected multiple 'git add' batches; got {len(add_calls)} add calls"
        )

    def test_chunking_large_path_list_commits_all(self, tmp_path: Path) -> None:
        """All 1500 files must end up in the commit even with chunked staging."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        pages_dir = root / "pages"
        pages_dir.mkdir()
        written: list[Path] = []
        for i in range(1500):
            p = pages_dir / f"page_{i:04d}.md"
            p.write_text(f"# Page {i}", encoding="utf-8")
            written.append(p)
        result = _BuildResult(written=written)
        committed = gitio.commit_export(root, result, "Export large")
        assert committed is True
        # Count files in the commit.
        show = subprocess.run(
            ["git", "show", "--name-only", "--format="],
            cwd=root, capture_output=True, text=True, check=True,
        )
        committed_count = len([f for f in show.stdout.splitlines() if f.strip()])
        assert committed_count == 1500

    def test_missing_git_binary_raises_git_error(self, tmp_path: Path) -> None:
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        page = root / "Page.md"
        page.write_text("# Page", encoding="utf-8")
        result = _BuildResult(written=[page])
        with mock.patch("conex.gitio._git_available", return_value=False):
            with pytest.raises(GitError):
                gitio.commit_export(root, result, "Export")

    def test_written_path_that_vanished_does_not_abort_commit(
        self, tmp_path: Path
    ) -> None:
        """Regression (MAJOR): a path in result.written that no longer exists
        on disk must not cause 'git add --' to exit 128 and abort the commit.

        The vanished path should be treated as a deletion so the remaining
        written paths are still committed successfully.
        """
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        # A page that actually exists and should be committed.
        present = root / "Present.md"
        present.write_text("# Present", encoding="utf-8")
        # A path listed in written that has already vanished from disk
        # (simulates the title-swap scenario where a freshly-written file was
        # subsequently deleted by the deferred cleanup of a swap partner before
        # the commit step — should NOT happen with the deferred-cleanup fix,
        # but commit_export must be robust regardless).
        vanished = root / "Vanished.md"
        # Do NOT create vanished — it is absent from disk.
        result = _BuildResult(written=[present, vanished])
        # Must not raise GitError; must commit the present file.
        committed = gitio.commit_export(root, result, "Export resilience test")
        assert committed is True
        show = subprocess.run(
            ["git", "show", "--name-only", "--format="],
            cwd=root, capture_output=True, text=True, check=True,
        )
        committed_files = [f for f in show.stdout.splitlines() if f.strip()]
        assert any("Present.md" in f for f in committed_files), (
            "Present.md must be in the commit even when a sibling written path vanished"
        )

    def test_all_written_vanished_still_does_not_abort_commit(
        self, tmp_path: Path
    ) -> None:
        """When every written path has vanished, commit_export must not raise;
        it returns False (empty delta) or commits the deletions if tracked."""
        root = tmp_path / "export"
        root.mkdir()
        _init_repo(root)
        _make_commit(root)  # give repo a HEAD
        vanished = root / "Vanished.md"
        # Do NOT create vanished.
        result = _BuildResult(written=[vanished])
        # Must not raise; returns True/False but never GitError.
        try:
            gitio.commit_export(root, result, "All vanished")
        except GitError as exc:
            pytest.fail(f"commit_export raised GitError for all-vanished written list: {exc}")


# ---------------------------------------------------------------------------
# _chunked_paths unit tests
# ---------------------------------------------------------------------------


class TestChunkedPaths:
    def test_empty_input(self) -> None:
        assert list(gitio._chunked_paths([])) == []

    def test_single_path_below_limit(self) -> None:
        batches = list(gitio._chunked_paths(["a/b/c.md"], max_bytes=1000))
        assert batches == [["a/b/c.md"]]

    def test_split_at_limit(self) -> None:
        # Two paths of 5 bytes each; limit = 8 -> must split.
        paths = ["abcd", "efgh"]  # 5 bytes each (4 chars + 1 overhead)
        batches = list(gitio._chunked_paths(paths, max_bytes=8))
        assert len(batches) == 2
        assert batches[0] == ["abcd"]
        assert batches[1] == ["efgh"]

    def test_all_paths_in_one_batch_when_small(self) -> None:
        paths = ["a", "b", "c"]
        batches = list(gitio._chunked_paths(paths, max_bytes=1000))
        assert len(batches) == 1
        assert batches[0] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _is_conex_relpath unit tests
# ---------------------------------------------------------------------------


class TestIsConexRelpath:
    def test_conex_dir_itself(self) -> None:
        assert gitio._is_conex_relpath(".conex") is True

    def test_conex_subpath(self) -> None:
        assert gitio._is_conex_relpath(".conex/state.json") is True

    def test_conex_nested(self) -> None:
        assert gitio._is_conex_relpath("Space/.conex/lock") is True

    def test_non_conex(self) -> None:
        assert gitio._is_conex_relpath("Space/Page.md") is False

    def test_conex_upper_case(self) -> None:
        assert gitio._is_conex_relpath(".CONEX/config.json") is True


# ---------------------------------------------------------------------------
# Integration: ensure_repo + commit_export full round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_full_workflow(self, tmp_path: Path) -> None:
        root = tmp_path / "wiki"
        root.mkdir()
        gitio.ensure_repo(root)
        # Write a page and commit via export.
        page = root / "Space" / "PageA.md"
        page.parent.mkdir()
        page.write_text("# PageA", encoding="utf-8")
        result = _BuildResult(written=[page])
        assert gitio.commit_export(root, result, "Export: initial") is True
        # Modify the page to simulate user edit then re-export.
        page.write_text("# PageA (edited by user)", encoding="utf-8")
        assert gitio.commit_user_changes(root) is True
        # Now update via export again.
        page.write_text("# PageA v2", encoding="utf-8")
        result2 = _BuildResult(written=[page])
        assert gitio.commit_export(root, result2, "Export: update") is True
        msgs = _log_messages(root)
        assert msgs[0] == "Export: update"
        assert msgs[1] == "Local changes before export"
        assert msgs[2] == "Export: initial"
