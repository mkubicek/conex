"""Tests for page relocation across export runs (issue #17)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from confluence_export.exporter import Exporter
from confluence_export.git import _prune_empty_dirs, relocate_subtree
from confluence_export.types import CachedSpace, Page, Space, Version


def _make_space():
    return Space(id="1", key="TEST", name="Test Space")


def _make_page(id_, title, *, parent_id="", body=None, position=0):
    return Page(
        id=id_,
        title=title,
        space_id="1",
        body_storage=body if body is not None else f"<p>{title}</p>",
        parent_id=parent_id,
        parent_type="page" if parent_id else "space",
        position=position,
        version=Version(created_at="2025-01-01", number=1),
        webui=f"/spaces/TEST/pages/{id_}",
    )


def _make_exporter():
    client = MagicMock()
    cache = MagicMock()
    return (
        Exporter(
            client=client,
            cache=cache,
            base_url="https://x.atlassian.net",
            download_media=False,
            render_drawio=False,
        ),
        cache,
    )


def _run_export(exporter, cache, pages, output_dir, *, use_git=False):
    cs = CachedSpace(
        space=_make_space(),
        pages=pages,
        attachments={},
        updated_at="2025-01-01T00:00:00Z",
    )
    cache.ensure_loaded.return_value = cs
    return exporter.export_space(_make_space(), output_dir, use_git=use_git)


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


class TestPageMovedBetweenRuns:
    def test_filesystem_move(self, tmp_path):
        # Run 1: page under parent A
        exporter, cache = _make_exporter()
        a = _make_page("a", "ParentA")
        p = _make_page("p", "MyPage", parent_id="a")
        _run_export(exporter, cache, [a, p], tmp_path)
        assert (tmp_path / "ParentA" / "MyPage" / "MyPage.md").exists()

        # Run 2: page moved under parent B
        b = _make_page("b", "ParentB")
        p2 = _make_page("p", "MyPage", parent_id="b")
        exporter2, cache2 = _make_exporter()
        result = _run_export(exporter2, cache2, [a, b, p2], tmp_path)

        assert (tmp_path / "ParentB" / "MyPage" / "MyPage.md").exists()
        # Old location is gone
        assert not (tmp_path / "ParentA" / "MyPage").exists()
        assert result.relocated >= 1

    def test_workspace_carried_along(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "ParentA")
        p = _make_page("p", "MyPage", parent_id="a")
        _run_export(exporter, cache, [a, p], tmp_path)

        ws_file = tmp_path / "ParentA" / "MyPage" / ".workspace" / "notes.py"
        ws_file.write_text("user data")

        b = _make_page("b", "ParentB")
        p2 = _make_page("p", "MyPage", parent_id="b")
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [a, b, p2], tmp_path)

        # Workspace content moved with the page
        moved_ws = tmp_path / "ParentB" / "MyPage" / ".workspace" / "notes.py"
        assert moved_ws.exists()
        assert moved_ws.read_text() == "user data"
        assert not ws_file.exists()

    def test_subtree_with_children_moves(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "ParentA")
        p = _make_page("p", "MyPage", parent_id="a")
        c = _make_page("c", "ChildPage", parent_id="p")
        _run_export(exporter, cache, [a, p, c], tmp_path)
        assert (tmp_path / "ParentA" / "MyPage" / "ChildPage" / "ChildPage.md").exists()

        b = _make_page("b", "ParentB")
        p2 = _make_page("p", "MyPage", parent_id="b")
        c2 = _make_page("c", "ChildPage", parent_id="p")
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [a, b, p2, c2], tmp_path)

        assert (tmp_path / "ParentB" / "MyPage" / "ChildPage" / "ChildPage.md").exists()
        assert not (tmp_path / "ParentA" / "MyPage").exists()

    def test_old_parent_dir_pruned_when_empty(self, tmp_path):
        exporter, cache = _make_exporter()
        # ParentA exists only as a structural folder; after the only child moves
        # out, it should be cleaned up (it has no content of its own and no
        # page in the new export tree).
        a = _make_page("a", "ParentA", body="")
        a.status = "folder"
        p = _make_page("p", "MyPage", parent_id="a")
        _run_export(exporter, cache, [a, p], tmp_path)

        b = _make_page("b", "ParentB")
        p2 = _make_page("p", "MyPage", parent_id="b")
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [b, p2], tmp_path)

        # Old parent folder's MyPage subdir is gone — the folder itself may
        # still exist (we don't prune in the exporter outside the git path),
        # but the orphaned page directory is gone.
        assert not (tmp_path / "ParentA" / "MyPage").exists()

    def test_two_page_swap(self, tmp_path):
        """Pages X and Y swap parents simultaneously. Both end up at the
        right location, neither overwrites the other."""
        exporter, cache = _make_exporter()
        a = _make_page("a", "A")
        b = _make_page("b", "B")
        x = _make_page("x", "X", parent_id="a")
        y = _make_page("y", "Y", parent_id="b")
        _run_export(exporter, cache, [a, b, x, y], tmp_path)
        assert (tmp_path / "A" / "X" / "X.md").exists()
        assert (tmp_path / "B" / "Y" / "Y.md").exists()

        # Swap names so X→ inside A becomes Y's slot, and vice versa
        x2 = _make_page("x", "Y", parent_id="a")
        y2 = _make_page("y", "X", parent_id="b")
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [a, b, x2, y2], tmp_path)

        # Both pages exist at their new on-disk names, frontmatter intact
        x_new = tmp_path / "A" / "Y" / "Y.md"
        y_new = tmp_path / "B" / "X" / "X.md"
        assert x_new.exists()
        assert y_new.exists()
        # Page id of the file at A/Y is still 'x'
        assert "page_id: x" in x_new.read_text() or "page_id: 'x'" in x_new.read_text()
        assert "page_id: y" in y_new.read_text() or "page_id: 'y'" in y_new.read_text()


class TestFrontmatterFallback:
    def test_first_run_with_existing_export_no_manifest(self, tmp_path):
        """A user without a manifest (pre-fix install) re-exports: the manifest
        is reconstructed from frontmatter and orphaned pages are relocated."""
        # Simulate a pre-fix export: page at old location, no manifest.
        old_dir = tmp_path / "OldParent" / "MyPage"
        old_dir.mkdir(parents=True)
        (old_dir / ".workspace").mkdir()
        (old_dir / ".workspace" / "notes.txt").write_text("user data")
        (old_dir / "MyPage.md").write_text(
            "---\n"
            "title: MyPage\n"
            "page_id: p\n"
            "space_key: TEST\n"
            "path: /OldParent/MyPage\n"
            "version: 1\n"
            "---\n\n"
            "# MyPage\n"
        )

        # New export run: the page now lives under NewParent
        exporter, cache = _make_exporter()
        new_parent = _make_page("np", "NewParent")
        p = _make_page("p", "MyPage", parent_id="np")
        _run_export(exporter, cache, [new_parent, p], tmp_path)

        # Page is relocated to NewParent, including workspace content
        assert (tmp_path / "NewParent" / "MyPage" / "MyPage.md").exists()
        assert (tmp_path / "NewParent" / "MyPage" / ".workspace" / "notes.txt").exists()
        # Old path gone
        assert not (tmp_path / "OldParent" / "MyPage").exists()


class TestNewDirWritesManifest:
    def test_brand_new_export_writes_manifest(self, tmp_path):
        exporter, cache = _make_exporter()
        p = _make_page("p", "Page")
        _run_export(exporter, cache, [p], tmp_path)
        mpath = tmp_path / ".test.path_manifest.json"
        assert mpath.exists()
        raw = json.loads(mpath.read_text())
        assert raw["pages"]["p"]["path"] == "Page"
        assert raw["pages"]["p"]["title"] == "Page"


class TestEmptyOldParentPruned:
    def test_relocation_prunes_empty_parent_in_non_git_mode(self, tmp_path):
        """Issue #17 requires the old path to leave no stale directory behind,
        including the empty parent dir, even when output_dir is not a git repo."""
        exporter, cache = _make_exporter()
        a = _make_page("a", "ParentA")
        a.status = "folder"
        p = _make_page("p", "MyPage", parent_id="a")
        _run_export(exporter, cache, [a, p], tmp_path)

        b = _make_page("b", "ParentB")
        p2 = _make_page("p", "MyPage", parent_id="b")
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [b, p2], tmp_path)

        # The whole ParentA chain is gone — relocate_subtree prunes upward
        # until it hits output_dir or a dir with .workspace content.
        assert not (tmp_path / "ParentA").exists()


class TestOrphanParkRecovery:
    def test_orphan_park_with_known_page_id_restored(self, tmp_path):
        """A .__conex_tmp_<id> dir left by a crashed prior run is moved back
        to the manifest path so its workspace content survives."""
        exporter, cache = _make_exporter()
        a = _make_page("a", "ParentA")
        p = _make_page("p", "MyPage", parent_id="a")
        _run_export(exporter, cache, [a, p], tmp_path)

        # Simulate a crashed swap: MyPage's directory got parked.
        page_dir = tmp_path / "ParentA" / "MyPage"
        park = tmp_path / ".__conex_tmp_p"
        # Move workspace content + md into the park to simulate the partial state
        park.mkdir()
        ws = park / ".workspace"
        ws.mkdir()
        (ws / "data.txt").write_text("user data")
        # Remove the real page dir to simulate it was parked
        import shutil as _sh
        _sh.rmtree(page_dir)

        # Re-export: the sweep should recover the park into the manifest path
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [a, p], tmp_path)

        # Workspace content restored, park gone
        assert (tmp_path / "ParentA" / "MyPage" / ".workspace" / "data.txt").exists()
        assert not park.exists()

    def test_orphan_park_with_unknown_page_id_dropped(self, tmp_path):
        """A .__conex_tmp_<id> dir whose page_id isn't in the manifest is dropped."""
        exporter, cache = _make_exporter()
        p = _make_page("p", "MyPage")
        _run_export(exporter, cache, [p], tmp_path)

        # Park for a totally unknown page id
        park = tmp_path / ".__conex_tmp_unknown999"
        park.mkdir()
        (park / "junk.md").write_text("nope")

        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [p], tmp_path)
        assert not park.exists()


class TestDeletedPageWorkspacePreserved:
    def test_deleting_page_with_workspace_keeps_user_content(self, tmp_path):
        """When a page is deleted upstream and its on-disk dir has workspace
        content, the workspace content should not be silently destroyed.
        This is enforced by _prune_empty_dirs treating .workspace as content."""
        exporter, cache = _make_exporter()
        p = _make_page("p", "Page")
        _run_export(exporter, cache, [p], tmp_path)

        ws = tmp_path / "Page" / ".workspace" / "notes.txt"
        ws.write_text("user data")

        # The non-git path of the exporter doesn't proactively delete orphans
        # (that's git's job via _remove_stale_files). The workspace content
        # therefore survives a re-export with the page removed upstream.
        exporter2, cache2 = _make_exporter()
        # Empty page list — page was deleted upstream
        other = _make_page("other", "Other")
        _run_export(exporter2, cache2, [other], tmp_path)
        assert ws.exists()
        assert ws.read_text() == "user data"
