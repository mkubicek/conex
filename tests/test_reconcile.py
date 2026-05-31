"""Tests for the move reconciler (issue #17, Option B — detect-and-warn).

The reconciler never moves page directories or sidecars. A full export rewrites
every page's markdown fresh at its new path and git detects the rename, and
``.media`` re-downloads at the new path, so on a move the reconciler only: drops
the stale old markdown + ``.media`` (disposable), leaves a non-empty user
``.workspace`` in place with a note (conex deliberately does NOT auto-carry it),
removes an empty auto-created ``.workspace``, heals legacy duplicates, and prunes
emptied shells.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

from confluence_export.layout import plan_layout
from confluence_export.reconcile import reconcile
from confluence_export.tree import build_tree
from confluence_export.types import Page


def _page(id, title, parent_id="", position=0, status="page"):
    return Page(id=id, title=title, parent_id=parent_id,
                parent_type="page" if parent_id else "space",
                position=position, status=status)


def _plan(pages):
    return plan_layout(build_tree(pages))


def _frontmatter(page_id, title, version=1, space_key="TEST"):
    meta = {"title": title, "page_id": page_id, "space_key": space_key,
            "path": f"/{title}", "version": version}
    return f"---\n{yaml.dump(meta, sort_keys=False, allow_unicode=True)}---\n\n# {title}\n\nbody\n"


def _seed(output_dir, rel, page_id, *, title=None, version=1, workspace=None, media=None):
    """Create a page directory on disk; its markdown stem is rel's final segment."""
    rel_path = PurePosixPath(rel)
    leaf = rel_path.name
    title = leaf if title is None else title
    page_dir = output_dir.joinpath(*rel_path.parts)
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / f"{leaf}.md").write_text(_frontmatter(page_id, title, version))
    if workspace is not None:
        ws = page_dir / ".workspace"
        ws.mkdir(exist_ok=True)
        (ws / "note.txt").write_text(workspace)
    if media is not None:
        m = page_dir / ".media"
        m.mkdir(exist_ok=True)
        (m / "img.png").write_text(media)
    return page_dir


class TestMovedPage:
    def test_reparent_drops_old_md_and_media_and_leaves_workspace_with_note(self, tmp_path, capsys):
        _seed(tmp_path, "A", "a")
        _seed(tmp_path, "A/P", "p", workspace="user script", media="png-bytes")
        plan = _plan([_page("a", "A"), _page("b", "B"), _page("p", "P", parent_id="b")])

        reconcile(plan, tmp_path, "TEST")

        # The user's .workspace is NOT carried — left in place at the old path...
        assert (tmp_path / "A" / "P" / ".workspace" / "note.txt").read_text() == "user script"
        # ...the disposable markdown + media are dropped (regenerated at B/P)...
        assert not (tmp_path / "A" / "P" / "P.md").exists()
        assert not (tmp_path / "A" / "P" / ".media").exists()
        # ...reconcile does NOT write the page at the new path (the writer does)...
        assert not (tmp_path / "B" / "P" / "P.md").exists()
        assert (tmp_path / "A" / "A.md").exists()  # untouched sibling parent
        # ...and the user is told where the page went.
        err = capsys.readouterr().err
        assert "moved to" in err and "B/P" in err and "do not move automatically" in err

    def test_note_points_at_new_path_and_old_workspace(self, tmp_path, capsys):
        _seed(tmp_path, "Old", "p", title="Old", workspace="keep")
        reconcile(_plan([_page("q", "New-Parent"), _page("p", "Old", parent_id="q")]),
                  tmp_path, "TEST")
        err = capsys.readouterr().err
        assert "New-Parent/Old" in err            # new location
        assert "Old/.workspace" in err            # where the prep files are

    def test_no_media_move_announces_dropped_attachments(self, tmp_path, capsys):
        # Under --no-media (media_will_redownload=False) the old .media is still
        # dropped, but the loss is announced, not silent.
        _seed(tmp_path, "A/P", "p", media="png-bytes")
        reconcile(_plan([_page("q", "B"), _page("p", "P", parent_id="q")]),
                  tmp_path, "TEST", media_will_redownload=False)
        assert not (tmp_path / "A" / "P" / ".media").exists()  # still dropped
        err = capsys.readouterr().err
        assert "cached attachments" in err and "re-run a full export with media" in err

    def test_with_media_move_does_not_announce_attachments(self, tmp_path, capsys):
        # The default (media_will_redownload=True) re-downloads at the new path,
        # so dropping the old .media is a non-event — no attachment note.
        _seed(tmp_path, "A/P", "p", media="png-bytes")
        reconcile(_plan([_page("q", "B"), _page("p", "P", parent_id="q")]), tmp_path, "TEST")
        assert "cached attachments" not in capsys.readouterr().err

    def test_sidecarless_move_just_drops_old_markdown(self, tmp_path):
        # A page with no .workspace/.media: nothing to leave behind; reconcile only
        # removes the stale old .md and prunes the emptied shell.
        _seed(tmp_path, "A", "a")
        _seed(tmp_path, "A/P", "p")
        reconcile(_plan([_page("a", "A"), _page("b", "B"), _page("p", "P", parent_id="b")]),
                  tmp_path, "TEST")
        assert not (tmp_path / "A" / "P").exists()

    def test_move_with_only_empty_workspace_leaves_no_orphan_dir(self, tmp_path):
        # Every page gets an auto-created EMPTY .workspace on export. Moving a page
        # with no user content must still fully remove the old dir — the empty
        # .workspace must not keep the old shell alive (rmdir-only prune).
        _seed(tmp_path, "A", "a")
        page_dir = _seed(tmp_path, "A/P", "p")
        (page_dir / ".workspace").mkdir()  # auto-created, empty
        reconcile(_plan([_page("a", "A"), _page("b", "B"), _page("p", "P", parent_id="b")]),
                  tmp_path, "TEST")
        assert not (tmp_path / "A" / "P").exists()  # no old/path/.workspace/ orphan

    def test_no_return_value(self, tmp_path):
        # reconcile is a side-effecting relayout; it returns nothing (no git
        # relocation list to stage anymore).
        _seed(tmp_path, "A/P", "p")
        assert reconcile(_plan([_page("p", "P")]), tmp_path, "TEST") is None


class TestCaseOnlyRename:
    def test_case_only_target_is_noop(self, tmp_path):
        # A case-only title change ("Page" -> "page") must not be seen as a move
        # on a case-insensitive FS (same dir) — otherwise it churns every export.
        (tmp_path / "CaseProbe").mkdir()
        case_insensitive = (tmp_path / "caseprobe").exists()
        (tmp_path / "CaseProbe").rmdir()
        if not case_insensitive:
            import pytest

            pytest.skip("requires a case-insensitive filesystem")

        _seed(tmp_path, "Page", "p1", workspace="keep")
        reconcile(_plan([_page("p1", "page")]), tmp_path, "TEST")

        assert (tmp_path / "Page" / ".workspace" / "note.txt").read_text() == "keep"
        assert (tmp_path / "Page" / "Page.md").exists()  # not dropped — not a move


class TestDuplicateHealAtTarget:
    def test_at_target_copy_wins_over_higher_version_off_target(self, tmp_path):
        # A stale lower-version copy sits AT the target; a higher-version copy is
        # off-target. The at-target copy must be canonical (no move), so the live
        # target content is not destroyed; the off-target duplicate is pruned.
        _seed(tmp_path, "Page", "p1", version=1)
        _seed(tmp_path, "Old", "p1", title="Old", version=2, workspace="w")
        reconcile(_plan([_page("p1", "Page")]), tmp_path, "TEST")

        assert (tmp_path / "Page" / "Page.md").exists()        # at-target copy kept
        assert not (tmp_path / "Old" / "Old.md").exists()      # off-target duplicate's md deleted
        # The off-target duplicate's user .workspace is preserved (never destroyed).
        assert (tmp_path / "Old" / ".workspace" / "note.txt").read_text() == "w"


class TestSwap:
    def test_workspace_swap_leaves_each_at_its_old_path(self, tmp_path):
        # Two siblings swap names AND both have user workspace. conex does NOT swap
        # the workspaces — each stays at its old physical path with a note; only
        # the disposable markdown is dropped (the writer rewrites it swapped).
        _seed(tmp_path, "Alpha", "a", title="Alpha", workspace="A-ws")
        _seed(tmp_path, "Beta", "b", title="Beta", workspace="B-ws")
        plan = _plan([_page("a", "Beta", position=0), _page("b", "Alpha", position=1)])

        reconcile(plan, tmp_path, "TEST")

        assert (tmp_path / "Alpha" / ".workspace" / "note.txt").read_text() == "A-ws"
        assert (tmp_path / "Beta" / ".workspace" / "note.txt").read_text() == "B-ws"
        assert not (tmp_path / "Alpha" / "Alpha.md").exists()
        assert not (tmp_path / "Beta" / "Beta.md").exists()


class TestSelfNest:
    def test_reparent_into_own_subtree_leaves_workspace_with_note(self, tmp_path, capsys):
        # p2 ("Eps") is reparented under a NEW page p1 that takes its old name, so
        # p2's target Eps/Bar is INSIDE p2's own old dir. The old .workspace is left
        # at Eps/ (where p1 will be written) with a note; the stale md is dropped.
        _seed(tmp_path, "Eps", "p2", workspace="keep")
        plan = _plan([_page("p1", "Eps"), _page("p2", "Bar", parent_id="p1")])

        reconcile(plan, tmp_path, "TEST")

        assert (tmp_path / "Eps" / ".workspace" / "note.txt").read_text() == "keep"
        assert not (tmp_path / "Eps" / "Eps.md").exists()  # stale md dropped
        assert "do not move automatically" in capsys.readouterr().err


class TestFolderRename:
    def test_legacy_empty_folder_workspace_tidied(self, tmp_path):
        (tmp_path / "Docs").mkdir()
        (tmp_path / "Docs" / ".workspace").mkdir()
        _seed(tmp_path, "Docs/Guide", "guide", workspace="g")
        plan = _plan([
            _page("f", "Manuals", status="folder"),
            _page("guide", "Guide", parent_id="f"),
        ])

        reconcile(plan, tmp_path, "TEST")

        # Guide's own .workspace is left at the old path (warn-and-leave)...
        assert (tmp_path / "Docs" / "Guide" / ".workspace" / "note.txt").read_text() == "g"
        # ...but the empty legacy FOLDER .workspace is tidied away.
        assert not (tmp_path / "Docs" / ".workspace").exists()

    def test_legacy_empty_folder_workspace_tidied_despite_stray_sidecar_md(self, tmp_path):
        # A stray .md under a SIDECAR dir (.media/.conex/.git) must not count as a
        # real page and so must not spare an empty folder-level .workspace from
        # cleanup. The has-page scan mirrors the grouped scan's sidecar pruning.
        (tmp_path / "Docs").mkdir()
        (tmp_path / "Docs" / ".workspace").mkdir()
        (tmp_path / "Docs" / ".media").mkdir()
        (tmp_path / "Docs" / ".media" / "stray.md").write_text("# not a page")
        _seed(tmp_path, "Docs/Guide", "guide", workspace="g")
        plan = _plan([
            _page("f", "Manuals", status="folder"),
            _page("guide", "Guide", parent_id="f"),
        ])

        reconcile(plan, tmp_path, "TEST")

        # The stray .md under .media does not make the folder look occupied, so the
        # empty legacy FOLDER .workspace is still tidied.
        assert not (tmp_path / "Docs" / ".workspace").exists()

    def test_legacy_nonempty_folder_workspace_preserved_and_warned(self, tmp_path, capsys):
        (tmp_path / "Docs").mkdir()
        ws = tmp_path / "Docs" / ".workspace"
        ws.mkdir()
        (ws / "prep.txt").write_text("mine")
        _seed(tmp_path, "Docs/Guide", "guide")
        plan = _plan([
            _page("f", "Manuals", status="folder"),
            _page("guide", "Guide", parent_id="f"),
        ])

        reconcile(plan, tmp_path, "TEST")

        assert (tmp_path / "Docs" / ".workspace" / "prep.txt").read_text() == "mine"
        assert "user .workspace left" in capsys.readouterr().err


class TestArchiveToggle:
    def test_archive_leaves_workspace_at_old_path_with_note(self, tmp_path, capsys):
        _seed(tmp_path, "Page", "p1", workspace="keep")
        plan = _plan([_page("p1", "Page", status="archived")])

        reconcile(plan, tmp_path, "TEST")

        # The page's target moves into _archived/, so its old md is dropped, but the
        # user .workspace is left at the old path with a note (not carried).
        assert (tmp_path / "Page" / ".workspace" / "note.txt").read_text() == "keep"
        assert not (tmp_path / "Page" / "Page.md").exists()
        assert "_archived" in capsys.readouterr().err


class TestLegacyDuplicateHeal:
    def test_canonical_kept_duplicate_pruned_workspace_preserved(self, tmp_path, capsys):
        _seed(tmp_path, "Page", "p1", version=2)
        _seed(tmp_path, "OldPage", "p1", title="OldPage", version=1,
              workspace="keep me", media="x")
        plan = _plan([_page("p1", "Page")])

        reconcile(plan, tmp_path, "TEST")

        assert (tmp_path / "Page" / "Page.md").exists()  # canonical (at target) untouched
        assert not (tmp_path / "OldPage" / "OldPage.md").exists()
        assert not (tmp_path / "OldPage" / ".media").exists()
        assert (tmp_path / "OldPage" / ".workspace" / "note.txt").read_text() == "keep me"
        assert "orphaned workspace" in capsys.readouterr().err

    def test_duplicate_without_workspace_pruned(self, tmp_path):
        _seed(tmp_path, "Page", "p1", version=2)
        _seed(tmp_path, "OldPage", "p1", title="OldPage", version=1)
        reconcile(_plan([_page("p1", "Page")]), tmp_path, "TEST")
        assert (tmp_path / "Page" / "Page.md").exists()
        assert not (tmp_path / "OldPage").exists()


class TestNoOp:
    def test_absent_page_left_in_place(self, tmp_path):
        _seed(tmp_path, "Gone", "gone", workspace="mine")
        reconcile(_plan([_page("other", "Other")]), tmp_path, "TEST")
        assert (tmp_path / "Gone" / "Gone.md").exists()
        assert (tmp_path / "Gone" / ".workspace" / "note.txt").read_text() == "mine"

    def test_idempotent_second_run_is_noop(self, tmp_path):
        _seed(tmp_path, "A", "a")
        _seed(tmp_path, "A/P", "p", workspace="w")
        plan = _plan([_page("a", "A"), _page("b", "B"), _page("p", "P", parent_id="b")])

        reconcile(plan, tmp_path, "TEST")
        before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
        reconcile(plan, tmp_path, "TEST")
        after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
        assert before == after


def _raise_oserror(*args, **kwargs):
    raise OSError("forced")


class TestHelperBranches:
    def test_rel_str_falls_back_when_not_under_output_dir(self, tmp_path):
        from confluence_export.reconcile import _rel_str

        outside = tmp_path.parent / "elsewhere"
        assert _rel_str(outside, tmp_path) == str(outside)

    def test_same_dir_returns_false_on_oserror(self, tmp_path, monkeypatch):
        from confluence_export.reconcile import _same_dir

        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setattr(Path, "samefile", _raise_oserror)
        assert _same_dir(a, b) is False

    def test_rmdir_if_empty_swallows_oserror(self, tmp_path, monkeypatch):
        from confluence_export.reconcile import _rmdir_if_empty

        d = tmp_path / "d"
        d.mkdir()
        monkeypatch.setattr(Path, "rmdir", _raise_oserror)
        _rmdir_if_empty(d)  # must not raise
        assert d.exists()

    def test_remove_artifacts_swallows_unlink_oserror(self, tmp_path, monkeypatch):
        from confluence_export.reconcile import _remove_artifacts

        md = tmp_path / "P.md"
        md.write_text("x")
        monkeypatch.setattr(Path, "unlink", _raise_oserror)
        _remove_artifacts(md)  # must not raise
        assert md.exists()  # the unlink was blocked but swallowed

    def test_execute_deletes_swallows_unlink_oserror(self, tmp_path, monkeypatch):
        # A read-only/locked stale duplicate must not abort the reconcile: the
        # unlink in _execute_deletes swallows OSError like its sibling deletes.
        _seed(tmp_path, "Page", "p1", version=2)
        _seed(tmp_path, "OldPage", "p1", title="OldPage", version=1)
        monkeypatch.setattr(Path, "unlink", _raise_oserror)
        reconcile(_plan([_page("p1", "Page")]), tmp_path, "TEST")  # must not raise

    def test_duplicate_sharing_canonical_dir_keeps_its_sidecars(self, tmp_path):
        # Two markdown files with the same page_id in the SAME directory: the
        # non-canonical copy shares the canonical's dir, so its .media/.workspace
        # must never be touched (the canonical lives there too).
        page_dir = _seed(tmp_path, "Page", "p1", version=2, media="m", workspace="w")
        (page_dir / "legacy.md").write_text(_frontmatter("p1", "Page", version=1))

        reconcile(_plan([_page("p1", "Page")]), tmp_path, "TEST")

        assert (page_dir / "Page.md").exists()              # canonical kept
        assert not (page_dir / "legacy.md").exists()        # duplicate md removed
        assert (page_dir / ".media" / "img.png").exists()   # shared dir untouched
        assert (page_dir / ".workspace" / "note.txt").exists()
