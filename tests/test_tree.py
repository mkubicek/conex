"""Tests for tree building and navigation."""

from confluence_export.tree import (
    build_tree,
    collect_subtree,
    find_node_by_id,
    find_node_by_path,
    find_pages,
    format_tree,
    page_path,
)
from confluence_export.types import Page, Version


def test_build_tree(sample_pages):
    roots = build_tree(sample_pages)
    assert len(roots) == 1
    root = roots[0]
    assert root.page.title == "Root"
    assert len(root.children) == 2
    assert root.children[0].page.title == "Child A"
    assert root.children[1].page.title == "Child B"
    assert len(root.children[0].children) == 1
    assert root.children[0].children[0].page.title == "Grandchild A1"


def test_build_tree_sorts_by_position():
    pages = [
        Page(id="1", title="Root", parent_type="space", position=0),
        Page(id="2", title="Second", parent_id="1", parent_type="page", position=1),
        Page(id="3", title="First", parent_id="1", parent_type="page", position=0),
    ]
    roots = build_tree(pages)
    assert roots[0].children[0].page.title == "First"
    assert roots[0].children[1].page.title == "Second"


def test_build_tree_orphan_becomes_root():
    pages = [
        Page(id="1", title="Orphan", parent_id="999", parent_type="page", position=0),
    ]
    roots = build_tree(pages)
    assert len(roots) == 1
    assert roots[0].page.title == "Orphan"


def test_find_node_by_id(sample_pages):
    roots = build_tree(sample_pages)
    node = find_node_by_id(roots, "4")
    assert node is not None
    assert node.page.title == "Grandchild A1"


def test_find_node_by_id_not_found(sample_pages):
    roots = build_tree(sample_pages)
    assert find_node_by_id(roots, "999") is None


def test_find_node_by_path(sample_pages):
    roots = build_tree(sample_pages)

    node = find_node_by_path(roots, "/Root/Child A/Grandchild A1")
    assert node is not None
    assert node.page.id == "4"


def test_find_node_by_path_case_insensitive(sample_pages):
    roots = build_tree(sample_pages)

    node = find_node_by_path(roots, "/root/child a")
    assert node is not None
    assert node.page.id == "2"


def test_find_node_by_path_no_leading_slash(sample_pages):
    roots = build_tree(sample_pages)

    node = find_node_by_path(roots, "Root/Child B")
    assert node is not None
    assert node.page.id == "3"


def test_find_node_by_path_empty():
    assert find_node_by_path([], "") is None
    assert find_node_by_path([], "/") is None


def test_find_pages(sample_pages):
    results = find_pages(sample_pages, "child")
    assert len(results) == 3  # Child A, Child B, Grandchild A1
    titles = {p.title for p in results}
    assert titles == {"Child A", "Child B", "Grandchild A1"}


def test_find_pages_no_match(sample_pages):
    results = find_pages(sample_pages, "nonexistent")
    assert len(results) == 0


def test_page_path(sample_pages):
    assert page_path(sample_pages, "4") == "/Root/Child A/Grandchild A1"
    assert page_path(sample_pages, "1") == "/Root"
    assert page_path(sample_pages, "3") == "/Root/Child B"


def test_collect_subtree(sample_pages):
    roots = build_tree(sample_pages)
    child_a = find_node_by_id(roots, "2")
    subtree = collect_subtree(child_a)
    assert len(subtree) == 2  # Child A + Grandchild A1
    ids = {n.page.id for n in subtree}
    assert ids == {"2", "4"}


def test_format_tree(sample_pages):
    roots = build_tree(sample_pages)
    output = format_tree(roots)
    assert "Root" in output
    assert "Child A" in output
    assert "Child B" in output
    assert "Grandchild A1" in output
