"""Tests for the relocation primitives in git.py (issue #17, part 1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from confluence_export.git import _prune_empty_dirs, relocate_subtree


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)


class TestRelocateSubtreePrimitive:
    def test_moves_directory_tree(self, tmp_path):
        old = tmp_path / "Old"
        old.mkdir()
        (old / "file.md").write_text("hi")
        (old / ".workspace").mkdir()
        (old / ".workspace" / "user.py").write_text("print(1)")

        moved = relocate_subtree(
            old, tmp_path / "New", output_dir=tmp_path, use_git=False
        )
        assert moved is True
        assert not old.exists()
        assert (tmp_path / "New" / "file.md").read_text() == "hi"
        assert (tmp_path / "New" / ".workspace" / "user.py").read_text() == "print(1)"

    def test_noop_when_source_missing(self, tmp_path):
        moved = relocate_subtree(
            tmp_path / "missing",
            tmp_path / "dest",
            output_dir=tmp_path,
            use_git=False,
        )
        assert moved is False
        assert not (tmp_path / "dest").exists()

    def test_refuses_to_clobber_existing_dest(self, tmp_path, capsys):
        old = tmp_path / "Old"
        old.mkdir()
        (old / "a.md").write_text("a")
        new = tmp_path / "New"
        new.mkdir()
        (new / "b.md").write_text("b")

        moved = relocate_subtree(old, new, output_dir=tmp_path, use_git=False)
        assert moved is False
        # Both still exist
        assert (old / "a.md").exists()
        assert (new / "b.md").exists()
        assert "destination exists" in capsys.readouterr().err

    def test_git_rename_detected_via_log_follow(self, tmp_path):
        _init_repo(tmp_path)
        old = tmp_path / "Old"
        old.mkdir()
        # Substantial content so git's rename heuristic catches it.
        (old / "page.md").write_text("# Old\n\n" + "lorem ipsum " * 50)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        moved = relocate_subtree(
            old, tmp_path / "New", output_dir=tmp_path, use_git=True
        )
        assert moved is True
        subprocess.run(["git", "commit", "-m", "rename"], cwd=tmp_path, capture_output=True)

        # log --follow on the new path picks up the old commit
        log = subprocess.run(
            ["git", "log", "--follow", "--name-status", "--format=%s", "New/page.md"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "init" in log.stdout
        # The rename should show up as a rename (R) status
        diff = subprocess.run(
            ["git", "log", "--diff-filter=R", "--name-status", "-1"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "R" in diff.stdout


class TestPruneEmptyDirs:
    def test_removes_empty_chain_up_to_stop(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        _prune_empty_dirs(deep, tmp_path)
        assert not (tmp_path / "a").exists()
        assert tmp_path.exists()

    def test_stops_when_workspace_present(self, tmp_path):
        deep = tmp_path / "Page"
        deep.mkdir()
        (deep / ".workspace").mkdir()
        (deep / ".workspace" / "user.py").write_text("data")
        _prune_empty_dirs(deep, tmp_path)
        # Page dir survives because .workspace has content
        assert (deep / ".workspace" / "user.py").exists()
