"""Tests for collision-safe name allocation and manifest stability (issue #11)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from confluence_export.converter import (
    sanitize_base,
    sanitize_filename,
    truncate_with_suffix,
)
from confluence_export.exporter import Exporter
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


def _run_export(exporter, cache, pages, output_dir):
    cs = CachedSpace(
        space=_make_space(),
        pages=pages,
        attachments={},
        updated_at="2025-01-01T00:00:00Z",
    )
    cache.ensure_loaded.return_value = cs
    return exporter.export_space(_make_space(), output_dir)


class TestTruncateWithSuffix:
    def test_no_suffix_caps_at_max(self):
        assert truncate_with_suffix("a" * 200) == "a" * 100

    def test_suffix_steals_room(self):
        # Base of 100 chars + "-2" suffix → base truncated to 98 chars
        assert truncate_with_suffix("a" * 200, "-2") == "a" * 98 + "-2"

    def test_trailing_hyphen_stripped_before_suffix(self):
        # If truncation leaves a trailing hyphen, strip it so we don't get "foo--2"
        result = truncate_with_suffix("a" * 50 + "-" + "b" * 50, "-2")
        # 100 chars total length cap, with "-2" suffix reserving 2 → 98 chars of base
        assert result.endswith("-2")
        assert "--2" not in result

    def test_short_base_unchanged(self):
        assert truncate_with_suffix("hello") == "hello"
        assert truncate_with_suffix("hello", "-3") == "hello-3"


class TestSanitizeBase:
    def test_strips_punctuation(self):
        assert sanitize_base("foo: bar") == "foo-bar"
        assert sanitize_base("foo bar") == "foo-bar"

    def test_empty_returns_untitled(self):
        assert sanitize_base("") == "untitled"
        assert sanitize_base("???") == "untitled"
        assert sanitize_base("📝") == "untitled"

    def test_no_length_cap(self):
        """sanitize_base itself does not cap — that's truncate_with_suffix's job."""
        assert len(sanitize_base("a" * 200)) == 200


class TestCollisionsWhitespaceVsHyphen:
    def test_two_pages_distinct_directories(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "page one", position=0)
        b = _make_page("b", "page-one", position=1)
        result = _run_export(exporter, cache, [a, b], tmp_path)

        assert result.count == 2
        # First sibling wins the base name, second gets "-2"
        assert (tmp_path / "page-one" / "page-one.md").exists()
        assert (tmp_path / "page-one-2" / "page-one-2.md").exists()
        assert result.disambiguated == 1


class TestCollisionsPunctuation:
    def test_colon_vs_space_disambiguated(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "foo: bar", position=0)
        b = _make_page("b", "foo bar", position=1)
        _run_export(exporter, cache, [a, b], tmp_path)
        assert (tmp_path / "foo-bar").is_dir()
        assert (tmp_path / "foo-bar-2").is_dir()


class TestCollisionsCaseInsensitive:
    def test_uppercase_vs_lowercase_disambiguated(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "Foo", position=0)
        b = _make_page("b", "foo", position=1)
        _run_export(exporter, cache, [a, b], tmp_path)
        # Even on case-sensitive FS we disambiguate so the export works
        # uniformly across platforms.
        names = {p.name for p in tmp_path.iterdir() if p.is_dir()}
        assert "Foo" in names
        # The second sibling collides under casefold and gets "-2"
        assert "foo-2" in names


class TestCollisionsEmptyTitles:
    def test_three_emoji_titles_get_distinct_names(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "📝", position=0)
        b = _make_page("b", "???", position=1)
        c = _make_page("c", "💡", position=2)
        result = _run_export(exporter, cache, [a, b, c], tmp_path)
        assert result.count == 3
        names = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
        assert names == ["untitled", "untitled-2", "untitled-3"]


class TestCollisionsTruncation:
    def test_long_titles_sharing_prefix_disambiguated(self, tmp_path):
        exporter, cache = _make_exporter()
        long_a = "a" * 150
        long_b = "a" * 100 + "different_tail"
        a = _make_page("a", long_a, position=0)
        b = _make_page("b", long_b, position=1)
        _run_export(exporter, cache, [a, b], tmp_path)
        names = {p.name for p in tmp_path.iterdir() if p.is_dir()}
        # First gets 100-char "aaa..." truncation
        assert ("a" * 100) in names
        # Second collides and gets numeric suffix; total length still ≤ 100
        suffixed = [n for n in names if n.endswith("-2")]
        assert suffixed
        assert all(len(n) <= 100 for n in names)


class TestDirAndFilenameStaySynced:
    def test_md_filename_matches_dir(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "page one", position=0)
        b = _make_page("b", "page-one", position=1)
        _run_export(exporter, cache, [a, b], tmp_path)
        # The .md file inside page-one-2 is also named page-one-2.md
        assert (tmp_path / "page-one-2" / "page-one-2.md").exists()


class TestCollisionTreeShape:
    def test_colliding_sibling_children_do_not_merge(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "page one", position=0)
        b = _make_page("b", "page-one", position=1)
        child_a = _make_page("ca", "Child A", parent_id="a")
        child_b = _make_page("cb", "Child B", parent_id="b")

        _run_export(exporter, cache, [a, b, child_a, child_b], tmp_path)

        a_child = tmp_path / "page-one" / "Child-A" / "Child-A.md"
        b_child = tmp_path / "page-one-2" / "Child-B" / "Child-B.md"
        assert a_child.exists()
        assert b_child.exists()
        assert "page_id: ca" in a_child.read_text()
        assert "page_id: cb" in b_child.read_text()
        assert not (tmp_path / "page-one" / "Child-B").exists()
        assert not (tmp_path / "page-one-2" / "Child-A").exists()

    def test_same_child_name_can_repeat_under_different_parents(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "Parent A", position=0)
        b = _make_page("b", "Parent B", position=1)
        child_a = _make_page("ca", "Shared", parent_id="a")
        child_b = _make_page("cb", "Shared", parent_id="b")

        _run_export(exporter, cache, [a, b, child_a, child_b], tmp_path)

        assert (tmp_path / "Parent-A" / "Shared" / "Shared.md").exists()
        assert (tmp_path / "Parent-B" / "Shared" / "Shared.md").exists()
        assert not (tmp_path / "Parent-B" / "Shared-2").exists()


class TestManifestStability:
    def test_manifest_written_with_deterministic_keys(self, tmp_path):
        exporter, cache = _make_exporter()
        pages = [
            _make_page("p3", "Charlie", position=2),
            _make_page("p1", "Alpha", position=0),
            _make_page("p2", "Bravo", position=1),
        ]
        _run_export(exporter, cache, pages, tmp_path)
        mpath = tmp_path / ".test.path_manifest.json"
        assert mpath.exists()
        raw = json.loads(mpath.read_text())
        # Page ids sorted lexicographically
        assert list(raw["pages"].keys()) == ["p1", "p2", "p3"]

    def test_rerun_produces_byte_identical_manifest(self, tmp_path):
        exporter, cache = _make_exporter()
        a = _make_page("a", "page one", position=0)
        b = _make_page("b", "page-one", position=1)
        _run_export(exporter, cache, [a, b], tmp_path)

        mpath = tmp_path / ".test.path_manifest.json"
        first = mpath.read_text()

        # Second run with same inputs
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [a, b], tmp_path)
        second = mpath.read_text()
        assert first == second

    def test_manifest_preferred_name_survives_when_position_changes(self, tmp_path):
        """Page A gets 'foo', page B gets 'foo-2'. If positions swap on the next
        run, A still keeps 'foo' (because the manifest records it) rather than
        swapping with B."""
        exporter, cache = _make_exporter()
        a = _make_page("a", "Foo", position=0)
        b = _make_page("b", "foo", position=1)
        _run_export(exporter, cache, [a, b], tmp_path)

        # After first run: a='Foo', b='foo-2' (assuming case-insensitive collision)
        first_manifest = json.loads((tmp_path / ".test.path_manifest.json").read_text())
        a_path = first_manifest["pages"]["a"]["path"]
        b_path = first_manifest["pages"]["b"]["path"]

        # Swap their positions and re-export
        a2 = _make_page("a", "Foo", position=1)
        b2 = _make_page("b", "foo", position=0)
        exporter2, cache2 = _make_exporter()
        _run_export(exporter2, cache2, [a2, b2], tmp_path)

        second_manifest = json.loads((tmp_path / ".test.path_manifest.json").read_text())
        assert second_manifest["pages"]["a"]["path"] == a_path
        assert second_manifest["pages"]["b"]["path"] == b_path
