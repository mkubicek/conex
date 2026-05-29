"""Tests for the pure layout planner (issue #11: collision-safe naming)."""

from __future__ import annotations

from pathlib import PurePosixPath

from confluence_export.converter import MAX_FILENAME_LEN
from confluence_export.layout import plan_layout
from confluence_export.tree import build_tree
from confluence_export.types import Page


def _page(id, title, parent_id="", position=0, status="page"):
    parent_type = "page" if parent_id else "space"
    return Page(id=id, title=title, parent_id=parent_id,
                parent_type=parent_type, position=position, status=status)


def _plan(pages):
    # page_id -> target_dir (PurePosixPath relative to the output dir)
    return plan_layout(build_tree(pages))


def _segments(plan, *ids):
    return [plan[i].name for i in ids]


class TestCollisionMatrix:
    def test_whitespace_vs_hyphen_collide(self):
        # "page one" and "page-one" both sanitize to "page-one"
        plan = _plan([_page("1", "page one"), _page("2", "page-one")])
        assert _segments(plan, "1", "2") == ["page-one", "page-one-2"]

    def test_punctuation_strip_collides(self):
        # "foo: bar" and "foo bar" both sanitize to "foo-bar"
        plan = _plan([_page("1", "foo: bar"), _page("2", "foo bar")])
        assert _segments(plan, "1", "2") == ["foo-bar", "foo-bar-2"]

    def test_symbol_and_emoji_only_titles(self):
        # Both collapse to "untitled"
        plan = _plan([_page("1", "\U0001F4DD"), _page("2", "???")])
        assert _segments(plan, "1", "2") == ["untitled", "untitled-2"]

    def test_casefold_collision_case_insensitive_fs(self):
        # "Page" and "page" collide on macOS/Windows filesystems
        plan = _plan([_page("1", "Page"), _page("2", "page")])
        assert _segments(plan, "1", "2") == ["Page", "page-2"]

    def test_three_way_collision_increments_suffix(self):
        plan = _plan([_page("1", "Dup"), _page("2", "dup"), _page("3", "DUP")])
        assert _segments(plan, "1", "2", "3") == ["Dup", "dup-2", "DUP-3"]

    def test_long_title_prefix_collision(self):
        # Two distinct titles sharing the first 100 chars collide after truncation
        long_a = "a" * 150
        long_b = "a" * 150 + "b"
        plan = _plan([_page("1", long_a), _page("2", long_b)])
        seg1, seg2 = _segments(plan, "1", "2")
        assert seg1 == "a" * MAX_FILENAME_LEN
        assert seg2 != seg1
        # Suffix reservation: the disambiguated name still fits the cap.
        assert len(seg2) <= MAX_FILENAME_LEN
        assert seg2.endswith("-2")

    def test_no_collision_keeps_bare_names(self):
        plan = _plan([_page("1", "Alpha"), _page("2", "Beta")])
        assert _segments(plan, "1", "2") == ["Alpha", "Beta"]


class TestInvariants:
    def test_suffix_never_exceeds_cap(self):
        # 30 colliding long titles; every allocated segment stays within the cap.
        base = "x" * 150
        pages = [_page(str(i), base, position=i) for i in range(30)]
        plan = _plan(pages)
        for target_dir in plan.values():
            assert len(target_dir.name) <= MAX_FILENAME_LEN


class TestNesting:
    def test_same_title_under_different_parents_does_not_collide(self):
        pages = [
            _page("1", "Parent A"),
            _page("2", "Parent B"),
            _page("3", "Shared", parent_id="1"),
            _page("4", "Shared", parent_id="2"),
        ]
        plan = _plan(pages)
        assert plan["3"] == PurePosixPath("Parent-A/Shared")
        assert plan["4"] == PurePosixPath("Parent-B/Shared")

    def test_siblings_collide_under_same_parent(self):
        pages = [
            _page("1", "Parent"),
            _page("2", "Note", parent_id="1"),
            _page("3", "note", parent_id="1"),
        ]
        plan = _plan(pages)
        assert plan["2"] == PurePosixPath("Parent/Note")
        assert plan["3"] == PurePosixPath("Parent/note-2")


class TestFolders:
    def test_folder_and_child_get_target_dirs(self):
        # A folder is structural-only; it still gets a collision-safe target_dir
        # so its descendants nest correctly.
        pages = [
            _page("1", "Section", status="folder"),
            _page("2", "Doc", parent_id="1"),
        ]
        plan = _plan(pages)
        assert plan["1"] == PurePosixPath("Section")
        assert plan["2"] == PurePosixPath("Section/Doc")


class TestStability:
    def test_assignment_is_stable_across_input_order(self):
        # Same pages, shuffled input order -> identical plan. The allocator orders
        # siblings by (position, id), so the suffix is reproducible regardless of
        # build_tree's (position-only) traversal order.
        pages = [
            _page("10", "Report", position=0),
            _page("2", "report", position=0),
            _page("30", "REPORT", position=0),
        ]
        plan_forward = plan_layout(build_tree(list(pages)))
        plan_reversed = plan_layout(build_tree(list(reversed(pages))))
        assert plan_forward == plan_reversed
        # id "10" < "2" < "30" lexically, so "10" is the bare-name claimant.
        assert plan_forward["10"].name == "Report"
        assert plan_forward["2"].name == "report-2"
        assert plan_forward["30"].name == "REPORT-3"
