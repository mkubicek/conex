"""Tests for conex.layout — page+folder tree → collision-free path plan.

Coverage intent per SPEC-V2.md layout.py section:
- Basic tree wiring: single root page, multi-level nesting, position ordering.
- Folder nesting: pages with parent_type=="folder" nest under their folder;
  folders with unknown parents surface as roots.
- Collision allocation: NFC-casefold twins get -2/-3 suffixes; case twins
  also collide.
- Truncation at 100-char cap (truncate_with_suffix enforces the cap).
- Archived pages under synthetic _archived/ root; collision between a live
  page titled "_archived" and the synthetic root.
- PR3: live page whose parent is archived (or absent) surfaces as space root.
- Subtree resolution: case-insensitive raw-title matching, first-match-wins
  among same-titled siblings, no_children, subtree_dir populated.
- Empty space: zero pages, zero folders.
- LayoutPlan field names match the spec exactly.
"""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath

import pytest

from conex.layout import LayoutPlan, plan_layout
from conex.models import Folder, Page, Space


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_space(name: str = "My Space", key: str = "MS") -> Space:
    return Space(id="s1", key=key, name=name)


def make_page(
    page_id: str,
    title: str,
    parent_id: str = "",
    parent_type: str = "",
    position: int = 0,
    status: str = "current",
) -> Page:
    return Page(
        id=page_id,
        title=title,
        space_id="s1",
        parent_id=parent_id,
        parent_type=parent_type,
        position=position,
        status=status,
    )


def make_folder(
    folder_id: str,
    title: str,
    parent_id: str = "",
    position: int = 0,
) -> Folder:
    return Folder(id=folder_id, title=title, parent_id=parent_id, position=position)


# ---------------------------------------------------------------------------
# LayoutPlan interface
# ---------------------------------------------------------------------------


class TestLayoutPlanInterface:
    """The LayoutPlan dataclass must expose exactly the spec'd fields."""

    def test_fields_exist(self):
        plan = plan_layout(make_space(), [], [])
        assert hasattr(plan, "dirs")
        assert hasattr(plan, "files")
        assert hasattr(plan, "order")
        assert hasattr(plan, "folder_dirs")
        assert hasattr(plan, "subtree_dir")

    def test_frozen(self):
        plan = plan_layout(make_space(), [], [])
        with pytest.raises((AttributeError, TypeError)):
            plan.dirs = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Empty space
# ---------------------------------------------------------------------------


class TestEmptySpace:
    def test_empty_returns_empty_plan(self):
        plan = plan_layout(make_space("Empty"), [], [])
        assert plan.dirs == {}
        assert plan.files == {}
        assert plan.order == []
        assert plan.folder_dirs == {}
        assert plan.subtree_dir is None


# ---------------------------------------------------------------------------
# Space root naming
# ---------------------------------------------------------------------------


class TestSpaceRootNaming:
    def test_space_name_sanitized_as_root(self):
        space = make_space("My Space")
        page = make_page("p1", "Home")
        plan = plan_layout(space, [page], [])
        assert plan.dirs["p1"].parts[0] == "My-Space"

    def test_empty_space_name_falls_back_to_untitled(self):
        space = Space(id="s1", key="X", name="")
        page = make_page("p1", "Home")
        plan = plan_layout(space, [page], [])
        assert plan.dirs["p1"].parts[0] == "untitled"


# ---------------------------------------------------------------------------
# Single page
# ---------------------------------------------------------------------------


class TestSinglePage:
    def test_single_root_page(self):
        space = make_space("Docs")
        page = make_page("p1", "Home")
        plan = plan_layout(space, [page], [])

        assert plan.dirs["p1"] == PurePosixPath("Docs/Home")
        assert plan.files["p1"] == PurePosixPath("Docs/Home/Home.md")
        assert plan.order == ["p1"]

    def test_dir_leaf_equals_md_stem(self):
        """I-alloc: the directory leaf and .md stem are the same segment."""
        space = make_space("S")
        page = make_page("p1", "My Page")
        plan = plan_layout(space, [page], [])
        leaf = plan.dirs["p1"].name
        stem = plan.files["p1"].stem
        assert leaf == stem


# ---------------------------------------------------------------------------
# Position ordering
# ---------------------------------------------------------------------------


class TestPositionOrdering:
    def test_depth_first_order_by_position(self):
        space = make_space("S")
        root = make_page("root", "Root", position=0)
        child_a = make_page("ca", "A", parent_id="root", position=10)
        child_b = make_page("cb", "B", parent_id="root", position=20)
        grandchild = make_page("gc", "G", parent_id="ca", position=0)

        plan = plan_layout(space, [root, child_b, child_a, grandchild], [])
        # Depth-first: root, ca, gc, cb
        assert plan.order == ["root", "ca", "gc", "cb"]

    def test_siblings_sorted_by_position_then_id(self):
        space = make_space("S")
        root = make_page("root", "Root", position=0)
        p1 = make_page("p1", "Alpha", parent_id="root", position=5)
        p2 = make_page("p2", "Beta", parent_id="root", position=3)
        plan = plan_layout(space, [root, p1, p2], [])
        idx_p2 = plan.order.index("p2")
        idx_p1 = plan.order.index("p1")
        assert idx_p2 < idx_p1  # position 3 before position 5


# ---------------------------------------------------------------------------
# Collision allocation — NFC-casefold
# ---------------------------------------------------------------------------


class TestCollisionAllocation:
    def test_case_twins_get_suffix(self):
        space = make_space("S")
        p1 = make_page("p1", "Hello", position=1)
        p2 = make_page("p2", "hello", position=2)  # same casefold
        plan = plan_layout(space, [p1, p2], [])
        leaf1 = plan.dirs["p1"].name
        leaf2 = plan.dirs["p2"].name
        assert leaf1 != leaf2
        # First allocated keeps the bare form, second gets -2
        assert leaf2.endswith("-2")

    def test_unicode_nfc_twins_get_suffix(self):
        # U+212B ANGSTROM SIGN and U+00C5 LATIN CAPITAL A WITH RING ABOVE are
        # different codepoints that NFC-normalize to the same character.
        # After sanitize_filename they produce byte-different strings that
        # NFC-casefold to the same collision key.
        angstrom = "Å"   # ANGSTROM SIGN — NFC → U+00C5
        a_ring = "Å"    # LATIN CAPITAL LETTER A WITH RING ABOVE
        assert angstrom != a_ring  # different bytes

        space = make_space("S")
        p1 = make_page("p1", angstrom + "-unit", position=1)
        p2 = make_page("p2", a_ring + "-unit", position=2)
        plan = plan_layout(space, [p1, p2], [])
        leaf1 = plan.dirs["p1"].name
        leaf2 = plan.dirs["p2"].name
        assert leaf1 != leaf2
        assert leaf2.endswith("-2")

    def test_three_siblings_get_sequential_suffixes(self):
        space = make_space("S")
        pages = [make_page(f"p{i}", "same", position=i) for i in range(3)]
        plan = plan_layout(space, pages, [])
        leaves = [plan.dirs[f"p{i}"].name for i in range(3)]
        assert leaves[0] == "same"
        assert leaves[1] == "same-2"
        assert leaves[2] == "same-3"

    def test_collision_only_within_same_parent(self):
        """Pages in different parents may have the same segment without collision."""
        space = make_space("S")
        root1 = make_page("r1", "Root1", position=1)
        root2 = make_page("r2", "Root2", position=2)
        child1 = make_page("c1", "Same", parent_id="r1", position=0)
        child2 = make_page("c2", "Same", parent_id="r2", position=0)
        plan = plan_layout(space, [root1, root2, child1, child2], [])
        assert plan.dirs["c1"].name == "Same"
        assert plan.dirs["c2"].name == "Same"


# ---------------------------------------------------------------------------
# Truncation at 100-char cap
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_long_title_truncated_to_cap(self):
        from conex.paths import MAX_FILENAME_LEN
        space = make_space("S")
        # Title that sanitizes to exactly 110 'a' characters
        long_title = "a" * 110
        page = make_page("p1", long_title, position=1)
        plan = plan_layout(space, [page], [])
        leaf = plan.dirs["p1"].name
        assert len(leaf) <= MAX_FILENAME_LEN

    def test_collision_suffix_fits_within_cap(self):
        from conex.paths import MAX_FILENAME_LEN
        space = make_space("S")
        long_title = "a" * 110
        p1 = make_page("p1", long_title, position=1)
        p2 = make_page("p2", long_title, position=2)
        plan = plan_layout(space, [p1, p2], [])
        for pid in ("p1", "p2"):
            assert len(plan.dirs[pid].name) <= MAX_FILENAME_LEN


# ---------------------------------------------------------------------------
# Archived pages under synthetic _archived/ root
# ---------------------------------------------------------------------------


class TestArchivedPages:
    def test_archived_page_under_archived_root(self):
        space = make_space("S")
        page = make_page("p1", "Old Doc", status="archived")
        plan = plan_layout(space, [page], [])
        # The archived page should live under S/_archived/Old-Doc
        assert plan.dirs["p1"].parts == ("S", "_archived", "Old-Doc")

    def test_archived_child_nests_under_archived_parent(self):
        space = make_space("S")
        parent = make_page("par", "Parent", status="archived")
        child = make_page("ch", "Child", parent_id="par", status="archived")
        plan = plan_layout(space, [parent, child], [])
        assert plan.dirs["ch"].parts == ("S", "_archived", "Parent", "Child")

    def test_live_page_with_archived_parent_surfaces_as_root_pr3(self):
        space = make_space("S")
        archived_parent = make_page("ap", "Archived", status="archived")
        live_child = make_page("lc", "Live", parent_id="ap", status="current")
        plan = plan_layout(space, [archived_parent, live_child], [])
        # live child should be at space root level, not under _archived/
        assert plan.dirs["lc"].parts[0] == "S"
        assert "_archived" not in plan.dirs["lc"].parts

    def test_live_page_with_absent_parent_surfaces_as_root(self):
        space = make_space("S")
        page = make_page("p1", "Orphan", parent_id="missing-parent")
        plan = plan_layout(space, [page], [])
        assert plan.dirs["p1"].parts[0] == "S"

    def test_archived_root_collision_with_live_page_titled_archived(self):
        """A live page titled '_archived' must not collide with the synthetic root."""
        space = make_space("S")
        live_archived_named = make_page("p1", "_archived", position=10)
        archived_page = make_page("p2", "Old Doc", status="archived")
        plan = plan_layout(space, [live_archived_named, archived_page], [])
        # Both must have distinct paths at the space root level
        leaf_live = plan.dirs["p1"].name
        # The synthetic _archived dir is for p2 path prefix
        archived_dir = plan.dirs["p2"].parts[1]  # S/_archived*/Old-Doc
        assert leaf_live != archived_dir

    def test_archived_pages_not_in_files_dot_md_for_archived_root(self):
        """The synthetic __archived__ node has no .md file."""
        space = make_space("S")
        page = make_page("p1", "Old", status="archived")
        plan = plan_layout(space, [page], [])
        # p1 itself should have a .md file
        assert "p1" in plan.files
        # __archived__ synthetic id should not have a .md file
        assert "__archived__" not in plan.files

    def test_archived_synthetic_id_not_in_dirs_or_order(self):
        """The synthetic __archived__ id must not appear in dirs or order."""
        space = make_space("S")
        page = make_page("p1", "Old Doc", status="archived")
        plan = plan_layout(space, [page], [])
        assert "__archived__" not in plan.dirs
        assert "__archived__" not in plan.order


# ---------------------------------------------------------------------------
# Folder nesting (DELIBERATE DIVERGENCE from v1)
# ---------------------------------------------------------------------------


class TestFolderNesting:
    def test_folder_gets_directory_segment_no_md(self):
        space = make_space("S")
        folder = make_folder("f1", "Notes")
        plan = plan_layout(space, [], [folder])
        assert "f1" in plan.folder_dirs
        assert "f1" not in plan.files
        assert "f1" not in plan.dirs

    def test_page_nested_under_folder(self):
        space = make_space("S")
        folder = make_folder("f1", "Notes")
        page = make_page("p1", "My Note", parent_id="f1", parent_type="folder")
        plan = plan_layout(space, [page], [folder])
        assert plan.folder_dirs["f1"] == PurePosixPath("S/Notes")
        assert plan.dirs["p1"] == PurePosixPath("S/Notes/My-Note")
        assert plan.files["p1"] == PurePosixPath("S/Notes/My-Note/My-Note.md")

    def test_nested_folders(self):
        space = make_space("S")
        parent_folder = make_folder("f1", "Top", position=0)
        child_folder = make_folder("f2", "Sub", parent_id="f1", position=0)
        page = make_page("p1", "Deep", parent_id="f2", parent_type="folder")
        plan = plan_layout(space, [page], [parent_folder, child_folder])
        assert plan.folder_dirs["f1"] == PurePosixPath("S/Top")
        assert plan.folder_dirs["f2"] == PurePosixPath("S/Top/Sub")
        assert plan.dirs["p1"] == PurePosixPath("S/Top/Sub/Deep")

    def test_folder_with_unknown_parent_surfaces_as_root(self):
        space = make_space("S")
        folder = make_folder("f1", "Orphan", parent_id="no-such-parent")
        page = make_page("p1", "Doc", parent_id="f1", parent_type="folder")
        plan = plan_layout(space, [page], [folder])
        # Folder should be at root level
        assert plan.folder_dirs["f1"].parts == ("S", "Orphan")
        assert plan.dirs["p1"].parts == ("S", "Orphan", "Doc")

    def test_page_and_folder_collision_in_same_parent(self):
        """A page and folder with the same title in the same parent collide."""
        space = make_space("S")
        folder = make_folder("f1", "Notes", position=1)
        page = make_page("p1", "Notes", position=2)
        plan = plan_layout(space, [page], [folder])
        folder_leaf = plan.folder_dirs["f1"].name
        page_leaf = plan.dirs["p1"].name
        assert folder_leaf != page_leaf
        # The second-allocated gets -2
        assert page_leaf == "Notes-2" or folder_leaf == "Notes-2"

    def test_folder_not_in_order(self):
        """order contains only page ids, not folder ids."""
        space = make_space("S")
        folder = make_folder("f1", "Notes")
        page = make_page("p1", "Doc", parent_id="f1", parent_type="folder")
        plan = plan_layout(space, [page], [folder])
        assert "f1" not in plan.order
        assert "p1" in plan.order

    def test_folder_nested_under_page(self):
        """A folder whose parent_id points to a known PAGE must nest under it."""
        space = make_space("S")
        page = make_page("pg", "PageParent", position=0)
        folder = make_folder("fld", "Sub", parent_id="pg", position=0)
        child = make_page("ch", "Doc", parent_id="fld", parent_type="folder")
        plan = plan_layout(space, [page, child], [folder])
        # folder must be under the page's directory, not at space root
        assert plan.folder_dirs["fld"] == PurePosixPath("S/PageParent/Sub")
        assert plan.dirs["ch"] == PurePosixPath("S/PageParent/Sub/Doc")


# ---------------------------------------------------------------------------
# Subtree resolution
# ---------------------------------------------------------------------------


class TestSubtreeResolution:
    def _make_tree(self) -> tuple[Space, list[Page], list[Folder]]:
        space = make_space("Docs")
        pages = [
            make_page("home", "Home", position=0),
            make_page("a", "Alpha", position=1),
            make_page("a1", "Child One", parent_id="a", position=0),
            make_page("a2", "Child Two", parent_id="a", position=1),
            make_page("b", "Beta", position=2),
        ]
        return space, pages, []

    def test_subtree_restricts_to_node_and_descendants(self):
        space, pages, folders = self._make_tree()
        plan = plan_layout(space, pages, folders, subtree="/Alpha")
        # Should include Alpha and its children, not Home or Beta
        assert "a" in plan.dirs
        assert "a1" in plan.dirs
        assert "a2" in plan.dirs
        assert "home" not in plan.dirs
        assert "b" not in plan.dirs

    def test_subtree_case_insensitive(self):
        space, pages, folders = self._make_tree()
        plan_lower = plan_layout(space, pages, folders, subtree="/alpha")
        plan_upper = plan_layout(space, pages, folders, subtree="/ALPHA")
        assert set(plan_lower.dirs.keys()) == set(plan_upper.dirs.keys())

    def test_subtree_nested_path(self):
        space, pages, folders = self._make_tree()
        plan = plan_layout(space, pages, folders, subtree="/Alpha/Child One")
        assert "a1" in plan.dirs
        assert "a" not in plan.dirs
        assert "a2" not in plan.dirs

    def test_subtree_not_found_returns_empty_plan(self):
        space, pages, folders = self._make_tree()
        plan = plan_layout(space, pages, folders, subtree="/NonExistent")
        assert plan.dirs == {}
        assert plan.files == {}
        assert plan.order == []
        assert plan.subtree_dir is None

    def test_subtree_dir_populated(self):
        space, pages, folders = self._make_tree()
        plan = plan_layout(space, pages, folders, subtree="/Alpha")
        assert plan.subtree_dir is not None
        # subtree_dir should be the planned dir of Alpha
        assert plan.subtree_dir == plan.dirs["a"]

    def test_no_children_includes_only_root(self):
        space, pages, folders = self._make_tree()
        plan = plan_layout(space, pages, folders, subtree="/Alpha", no_children=True)
        assert "a" in plan.dirs
        assert "a1" not in plan.dirs
        assert "a2" not in plan.dirs

    def test_no_children_without_subtree_is_noop(self):
        """no_children without subtree should not restrict anything."""
        space, pages, folders = self._make_tree()
        plan_normal = plan_layout(space, pages, folders)
        plan_no_children = plan_layout(space, pages, folders, no_children=True)
        assert set(plan_normal.dirs.keys()) == set(plan_no_children.dirs.keys())

    def test_subtree_first_match_wins_among_same_title_siblings(self):
        """When two siblings share the same title, the first (position-ordered) wins."""
        space = make_space("S")
        root = make_page("root", "Root", position=0)
        twin1 = make_page("t1", "Twin", parent_id="root", position=1)
        twin2 = make_page("t2", "Twin", parent_id="root", position=2)
        child_of_t1 = make_page("c1", "Child", parent_id="t1", position=0)
        child_of_t2 = make_page("c2", "Child", parent_id="t2", position=0)

        plan = plan_layout(space, [root, twin1, twin2, child_of_t1, child_of_t2], [],
                           subtree="/Root/Twin")
        # Should match t1 (first by position), not t2
        assert "t1" in plan.dirs
        assert "c1" in plan.dirs
        assert "t2" not in plan.dirs

    def test_subtree_order_is_depth_first(self):
        space, pages, folders = self._make_tree()
        plan = plan_layout(space, pages, folders, subtree="/Alpha")
        # a comes before a1 and a2
        assert plan.order.index("a") < plan.order.index("a1")
        assert plan.order.index("a") < plan.order.index("a2")

    def test_subtree_paths_unchanged_vs_full_plan(self):
        """Subtree does not re-allocate; paths must match the full plan."""
        space, pages, folders = self._make_tree()
        full = plan_layout(space, pages, folders)
        sub = plan_layout(space, pages, folders, subtree="/Alpha")
        for pid in ("a", "a1", "a2"):
            assert sub.dirs[pid] == full.dirs[pid]
            assert sub.files[pid] == full.files[pid]


# ---------------------------------------------------------------------------
# Subtree with folder as root
# ---------------------------------------------------------------------------


class TestSubtreeFolderRoot:
    def test_subtree_rooted_at_folder(self):
        space = make_space("S")
        folder = make_folder("f1", "Notes")
        page = make_page("p1", "Doc", parent_id="f1", parent_type="folder")
        plan = plan_layout(space, [page], [folder], subtree="/Notes")
        assert "p1" in plan.dirs
        assert plan.subtree_dir == plan.folder_dirs["f1"]

    def test_subtree_folder_no_children(self):
        space = make_space("S")
        folder = make_folder("f1", "Notes")
        page = make_page("p1", "Doc", parent_id="f1", parent_type="folder")
        plan = plan_layout(space, [page], [folder], subtree="/Notes", no_children=True)
        # no_children with a folder root → no pages included (folder has no .md)
        assert "p1" not in plan.dirs


# ---------------------------------------------------------------------------
# PR3 — live children of archived pages
# ---------------------------------------------------------------------------


class TestPR3:
    def test_live_child_of_archived_parent_is_root(self):
        space = make_space("S")
        archived = make_page("ap", "Archived Parent", status="archived")
        live = make_page("lp", "Live Child", parent_id="ap", status="current")
        plan = plan_layout(space, [archived, live], [])
        # live child must not be nested under _archived
        assert "_archived" not in plan.dirs["lp"].parts
        assert plan.dirs["lp"].parts[0] == "S"

    def test_live_page_absent_parent_is_root(self):
        """A live page whose parent is simply not in the snapshot surfaces as root."""
        space = make_space("S")
        live = make_page("p1", "Live", parent_id="ghost-parent", status="current")
        plan = plan_layout(space, [live], [])
        assert plan.dirs["p1"].parts[0] == "S"
        assert len(plan.dirs["p1"].parts) == 2

    def test_archived_sibling_of_live_page_does_not_affect_live_path(self):
        space = make_space("S")
        parent = make_page("par", "Parent", status="current")
        live_child = make_page("lc", "Live", parent_id="par", status="current")
        archived_child = make_page("ac", "Archived", parent_id="par", status="archived")
        plan = plan_layout(space, [parent, live_child, archived_child], [])
        # live_child should still be under parent
        assert plan.dirs["lc"].parts == ("S", "Parent", "Live")
        # archived_child should be under _archived, not under parent
        assert plan.dirs["ac"].parts[1] == "_archived"


# ---------------------------------------------------------------------------
# Mixed pages and folders, deep tree
# ---------------------------------------------------------------------------


class TestMixedDeepTree:
    def test_deep_mixed_tree(self):
        space = make_space("Wiki")
        f_top = make_folder("f_top", "Engineering", position=0)
        f_sub = make_folder("f_sub", "Backend", parent_id="f_top", position=0)
        page_root = make_page("p_root", "Overview", position=0)
        page_in_folder = make_page("p_f", "API Docs", parent_id="f_top", parent_type="folder", position=0)
        page_nested = make_page("p_n", "Auth", parent_id="f_sub", parent_type="folder", position=0)
        child_of_page = make_page("p_c", "Token", parent_id="p_n", position=0)

        plan = plan_layout(
            space,
            [page_root, page_in_folder, page_nested, child_of_page],
            [f_top, f_sub],
        )

        assert plan.folder_dirs["f_top"] == PurePosixPath("Wiki/Engineering")
        assert plan.folder_dirs["f_sub"] == PurePosixPath("Wiki/Engineering/Backend")
        assert plan.dirs["p_root"] == PurePosixPath("Wiki/Overview")
        assert plan.dirs["p_f"] == PurePosixPath("Wiki/Engineering/API-Docs")
        assert plan.dirs["p_n"] == PurePosixPath("Wiki/Engineering/Backend/Auth")
        assert plan.dirs["p_c"] == PurePosixPath("Wiki/Engineering/Backend/Auth/Token")

    def test_order_depth_first_skips_folder_ids(self):
        space = make_space("S")
        folder = make_folder("f1", "F", position=0)
        p1 = make_page("p1", "A", parent_id="f1", parent_type="folder", position=0)
        p2 = make_page("p2", "B", position=10)
        plan = plan_layout(space, [p2, p1], [folder])
        # p1 is under f1 (position 0 at root), p2 is position 10 at root
        # depth-first: p1 comes before p2
        assert plan.order.index("p1") < plan.order.index("p2")
        assert "f1" not in plan.order
