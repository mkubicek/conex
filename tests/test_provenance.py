"""Unit tests for the archived-preservation provenance predicates.

Each hard-won invariant is one named predicate in ``provenance.py``; these tests
hit the predicates directly with hand-built ``RunProvenance`` snapshots, so a
regression in any single edge case surfaces as one failing predicate test rather
than a buried end-to-end failure. This is the whole point of the module.
"""

from pathlib import Path, PurePosixPath

from confluence_export.diff import ExportedPage
from confluence_export.provenance import (
    RunProvenance,
    archived_set_is_knowable,
    exact_archived_dirs,
    preservation_in_scope,
    recursive_archived_dirs,
)


def _prov(**kw) -> RunProvenance:
    base = dict(
        is_full=True,
        cache_sees_archived=False,
        archived_ids=frozenset(),
        in_scope_ids=frozenset(),
        plan={},
        pre_reconcile_dirs={},
        on_disk_entries=(),
        output_dir=Path("."),
    )
    base.update(kw)
    return RunProvenance(**base)


class TestPreservationInScope:
    def test_full_export_not_writing_archived_preserves(self):
        p = _prov(archived_ids=frozenset({"z"}), in_scope_ids=frozenset({"live"}))
        assert preservation_in_scope(p) is True

    def test_partial_export_never_preserves(self):
        p = _prov(is_full=False, archived_ids=frozenset({"z"}), in_scope_ids=frozenset({"live"}))
        assert preservation_in_scope(p) is False

    def test_include_archived_writes_them_so_nothing_to_preserve(self):
        # archived id is in scope (written this run) -> nothing to protect.
        p = _prov(archived_ids=frozenset({"z"}), in_scope_ids=frozenset({"z", "live"}))
        assert preservation_in_scope(p) is False


class TestArchivedSetIsKnowable:
    def test_authoritative_cache_is_knowable(self):
        assert archived_set_is_knowable(_prov(cache_sees_archived=True)) is True

    def test_holding_archived_pages_is_knowable(self):
        assert archived_set_is_knowable(_prov(archived_ids=frozenset({"z"}))) is True

    def test_blind_cache_with_no_archived_is_not_knowable(self):
        assert (
            archived_set_is_knowable(
                _prov(cache_sees_archived=False, archived_ids=frozenset())
            )
            is False
        )


class TestExactArchivedDirs:
    def test_preserves_archived_page_dir_from_plan(self, tmp_path):
        p = _prov(
            archived_ids=frozenset({"z"}),
            in_scope_ids=frozenset({"live"}),
            plan={"z": PurePosixPath("_archived/Zarch"), "live": PurePosixPath("Live")},
            output_dir=tmp_path,
        )
        dirs = {d.resolve() for d in exact_archived_dirs(p)}
        assert (tmp_path / "_archived" / "Zarch").resolve() in dirs

    def test_excludes_page_moved_back_into_scope(self, tmp_path):
        # M1-exact: a page that unarchived (now live, in scope) is NOT preserved,
        # so its old archived copy is still prunable.
        p = _prov(
            archived_ids=frozenset({"z"}),
            in_scope_ids=frozenset({"z"}),
            plan={"z": PurePosixPath("Live/Child")},
            output_dir=tmp_path,
        )
        assert exact_archived_dirs(p) == []

    def test_includes_pre_reconcile_old_path(self, tmp_path):
        # M2 overlap: an archived page's pre-reconcile old dir is preserved.
        old = tmp_path / "_archived" / "Old"
        p = _prov(
            archived_ids=frozenset({"z"}),
            pre_reconcile_dirs={"z": [old]},
            output_dir=tmp_path,
        )
        dirs = {d.resolve() for d in exact_archived_dirs(p)}
        assert old.resolve() in dirs


def _entry(tmp_path: Path, rel: str, *, status: str = "", path: str | None = None) -> ExportedPage:
    name = Path(rel).name
    return ExportedPage(
        page_id="x",
        version=1,
        title=name,
        path=path if path is not None else "/" + rel,
        file_path=tmp_path / rel / (name + ".md"),
        status=status,
    )


class TestRecursiveArchivedDirs:
    def test_whole_archived_root_preserved_without_frontmatter(self, tmp_path):
        # RF-A: a prior export with NO frontmatter is still preserved, because the
        # directory-existence check (not a frontmatter read) catches it.
        (tmp_path / "_archived" / "Old").mkdir(parents=True)
        (tmp_path / "_archived" / "Old" / "Old.md").write_text("# Old")
        p = _prov(plan={"live": PurePosixPath("Live")}, output_dir=tmp_path)
        dirs = {d.resolve() for d in recursive_archived_dirs(p)}
        assert (tmp_path / "_archived").resolve() in dirs

    def test_live_page_in_archived_named_dir_is_not_preserved(self, tmp_path):
        # The anti-dirname regression: a LIVE page whose dir is literally
        # "_archived" (status current, its own path) is NOT swept into archived
        # preservation just because of the dir name.
        (tmp_path / "_archived" / "Stale").mkdir(parents=True)
        entry = _entry(tmp_path, "_archived/Stale", status="", path="/_archived/Stale")
        p = _prov(
            plan={"live": PurePosixPath("_archived")},  # a live page owns "_archived"
            on_disk_entries=(entry,),
            output_dir=tmp_path,
        )
        assert recursive_archived_dirs(p) == []

    def test_real_archived_page_under_live_claimed_root_is_rescued(self, tmp_path):
        # A live page claims "_archived", but a genuinely archived page underneath
        # (status archived) is rescued per-page, not the whole live root.
        (tmp_path / "_archived" / "Old").mkdir(parents=True)
        entry = _entry(tmp_path, "_archived/Old", status="archived", path="/_archived/Old")
        p = _prov(
            plan={"live": PurePosixPath("_archived")},
            on_disk_entries=(entry,),
            output_dir=tmp_path,
        )
        dirs = {d.resolve() for d in recursive_archived_dirs(p)}
        assert (tmp_path / "_archived" / "Old").resolve() in dirs
        assert (tmp_path / "_archived").resolve() not in dirs

    def test_numeric_collision_suffix_root_is_preserved(self, tmp_path):
        # _archived-2 is a real numeric collision suffix plan_layout emits.
        (tmp_path / "_archived-2" / "Old").mkdir(parents=True)
        (tmp_path / "_archived-2" / "Old" / "Old.md").write_text("# Old")
        p = _prov(plan={"live": PurePosixPath("Live")}, output_dir=tmp_path)
        dirs = {d.resolve() for d in recursive_archived_dirs(p)}
        assert (tmp_path / "_archived-2").resolve() in dirs

    def test_non_numeric_archived_named_dir_is_not_preserved(self, tmp_path):
        # A stray / deleted live page named "_archived-legacy" is NOT a numeric
        # collision suffix, so it must not be mistaken for an archived root and
        # kept stale forever (the dirname-over-preservation Codex flagged).
        (tmp_path / "_archived-legacy" / "Old").mkdir(parents=True)
        (tmp_path / "_archived-legacy" / "Old" / "Old.md").write_text("# Old")
        p = _prov(plan={"live": PurePosixPath("Live")}, output_dir=tmp_path)
        assert recursive_archived_dirs(p) == []

    def test_live_claimed_root_with_empty_path_entry_stays_prunable(self, tmp_path):
        # A live page claims "_archived" and its own md carries no path frontmatter.
        # Without provenance we treat it as live content, so the whole live-claimed
        # root is NOT re-preserved (RF-A-coll guarantee for legacy/hand-edited md).
        (tmp_path / "_archived").mkdir(parents=True)
        entry = _entry(tmp_path, "_archived", status="", path="")
        p = _prov(
            plan={"live": PurePosixPath("_archived")},
            on_disk_entries=(entry,),
            output_dir=tmp_path,
        )
        assert recursive_archived_dirs(p) == []
