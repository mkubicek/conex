"""Tests for the diff module."""

from pathlib import Path

import pytest

from confluence_export.diff import (
    DiffResult,
    ExportedPage,
    _parse_frontmatter,
    compute_diff,
    format_diff,
    scan_export_dir,
)
from confluence_export.types import Page, Version


# -- _parse_frontmatter -------------------------------------------------------


def test_parse_frontmatter(tmp_path: Path):
    md = tmp_path / "page.md"
    md.write_text("---\ntitle: Hello\npage_id: '42'\nversion: 3\n---\n\nBody text\n")
    result = _parse_frontmatter(md)
    assert result == {"title": "Hello", "page_id": "42", "version": 3}


def test_parse_frontmatter_missing_delimiters(tmp_path: Path):
    md = tmp_path / "page.md"
    md.write_text("No frontmatter here\n")
    assert _parse_frontmatter(md) is None


def test_parse_frontmatter_invalid_yaml(tmp_path: Path):
    md = tmp_path / "page.md"
    md.write_text("---\n: [invalid\n---\n")
    assert _parse_frontmatter(md) is None


def test_parse_frontmatter_nonexistent(tmp_path: Path):
    assert _parse_frontmatter(tmp_path / "nope.md") is None


# -- scan_export_dir -----------------------------------------------------------


def _write_page(
    directory: Path,
    filename: str,
    page_id: str,
    version: int,
    title: str = "Test",
    space_key: str = "TST",
    path: str = "/Test",
) -> Path:
    p = directory / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntitle: {title}\npage_id: '{page_id}'\nspace_key: {space_key}\n"
        f"path: {path}\nversion: {version}\n---\n\nBody\n"
    )
    return p


def test_scan_export_dir(tmp_path: Path):
    _write_page(tmp_path, "Root/Root.md", "1", 5, title="Root", path="/Root")
    _write_page(tmp_path, "Root/Child/Child.md", "2", 3, title="Child", path="/Root/Child")

    result = scan_export_dir(tmp_path, "TST")
    assert len(result) == 2
    assert result["1"].version == 5
    assert result["2"].title == "Child"


def test_scan_skips_invalid(tmp_path: Path):
    # Valid file
    _write_page(tmp_path, "good.md", "1", 1)
    # No frontmatter
    (tmp_path / "bad1.md").write_text("Just text\n")
    # Frontmatter but no page_id
    (tmp_path / "bad2.md").write_text("---\ntitle: Oops\n---\n")
    # Different space
    _write_page(tmp_path, "other.md", "99", 1, space_key="OTHER")

    result = scan_export_dir(tmp_path, "TST")
    assert len(result) == 1
    assert "1" in result


# -- compute_diff --------------------------------------------------------------


def _make_page(page_id: str, version: int, title: str = "P") -> Page:
    return Page(id=page_id, title=title, version=Version(number=version))


def _make_exported(page_id: str, version: int, title: str = "P", path: str = "/P") -> ExportedPage:
    return ExportedPage(
        page_id=page_id, version=version, title=title, path=path, file_path=Path("/fake")
    )


def test_compute_diff_new():
    exported: dict[str, ExportedPage] = {}
    api = [_make_page("1", 1)]
    result = compute_diff(exported, api)
    assert len(result.new) == 1
    assert result.new[0].id == "1"
    assert result.unchanged_count == 0


def test_compute_diff_deleted():
    exported = {"1": _make_exported("1", 1)}
    result = compute_diff(exported, [])
    assert len(result.deleted) == 1
    assert result.deleted[0].page_id == "1"


def test_compute_diff_modified():
    exported = {"1": _make_exported("1", 3)}
    api = [_make_page("1", 5)]
    result = compute_diff(exported, api)
    assert len(result.modified) == 1
    exp, page = result.modified[0]
    assert exp.version == 3
    assert page.version.number == 5


def test_compute_diff_unchanged():
    exported = {"1": _make_exported("1", 5)}
    api = [_make_page("1", 5)]
    result = compute_diff(exported, api)
    assert result.unchanged_count == 1
    assert not result.new and not result.deleted and not result.modified


def test_compute_diff_mixed():
    exported = {
        "1": _make_exported("1", 3),  # modified
        "2": _make_exported("2", 1),  # unchanged
        "3": _make_exported("3", 2),  # deleted
    }
    api = [
        _make_page("1", 5),  # modified
        _make_page("2", 1),  # unchanged
        _make_page("4", 1),  # new
    ]
    result = compute_diff(exported, api)
    assert len(result.modified) == 1
    assert result.unchanged_count == 1
    assert len(result.deleted) == 1
    assert len(result.new) == 1


# -- format_diff ---------------------------------------------------------------


def test_format_diff():
    all_pages = [
        Page(id="1", title="Root"),
        Page(id="2", title="Child", parent_id="1"),
        Page(id="3", title="New Page", parent_id="1"),
    ]

    result = DiffResult(
        modified=[(_make_exported("2", 3, title="Child", path="/Root/Child"), all_pages[1])],
        new=[all_pages[2]],
        deleted=[_make_exported("99", 1, title="Gone", path="/Root/Gone")],
        unchanged_count=10,
    )

    output = format_diff(result, all_pages)
    assert "Modified (1):" in output
    assert "v3 -> v0" in output  # version 0 because test Page has default Version
    assert "New (1):" in output
    assert "/Root/New Page" in output
    assert "Deleted (1):" in output
    assert "/Root/Gone" in output
    assert "Unchanged: 10 page(s)" in output


def test_format_diff_empty():
    result = DiffResult(unchanged_count=5)
    output = format_diff(result, [])
    assert output == "Unchanged: 5 page(s)"
