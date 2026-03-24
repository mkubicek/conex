"""Tests for the export orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_export.exporter import Exporter
from confluence_export.types import (
    Attachment,
    CachedSpace,
    Page,
    PageNode,
    Space,
    Version,
)


def _make_space():
    return Space(id="1", key="TEST", name="Test Space")


def _make_page(id="p1", title="Test Page", body="<p>Hello</p>", parent_id="", parent_type="space"):
    return Page(
        id=id, title=title, space_id="1", body_storage=body,
        parent_id=parent_id, parent_type=parent_type,
        version=Version(created_at="2025-01-01", number=1),
    )


def _make_cached_space(pages=None, attachments=None):
    return CachedSpace(
        space=_make_space(),
        pages=pages or [_make_page()],
        attachments=attachments or {},
        updated_at="2025-01-01T00:00:00Z",
    )


def _make_exporter(**kwargs):
    client = MagicMock()
    cache = MagicMock()
    defaults = dict(
        client=client,
        cache=cache,
        base_url="https://x.atlassian.net",
        download_media=False,
        render_drawio=False,
    )
    defaults.update(kwargs)
    return Exporter(**defaults), client, cache


class TestExportSinglePage:
    def test_writes_markdown(self, tmp_path):
        exporter, client, cache = _make_exporter()
        page = _make_page()
        cs = _make_cached_space()

        count = exporter._export_single_page(page, tmp_path, cs, "TEST")
        assert count == 1

        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) == 1
        content = md_files[0].read_text()
        assert "Test Page" in content
        assert "Hello" in content

    def test_folder_skipped(self, tmp_path):
        exporter, _, _ = _make_exporter()
        page = _make_page()
        page.status = "folder"
        cs = _make_cached_space()

        count = exporter._export_single_page(page, tmp_path, cs, "TEST")
        assert count == 0
        assert list(tmp_path.glob("*.md")) == []

    def test_fetches_body_if_missing(self, tmp_path):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="")
        full_page = _make_page(body="<p>Fetched</p>")
        client.get_page_by_id.return_value = full_page
        cs = _make_cached_space(pages=[page])

        count = exporter._export_single_page(page, tmp_path, cs, "TEST")
        assert count == 1
        client.get_page_by_id.assert_called_once_with("p1")

    def test_fetch_body_failure(self, tmp_path, capsys):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="")
        client.get_page_by_id.side_effect = Exception("API error")
        cs = _make_cached_space(pages=[page])

        count = exporter._export_single_page(page, tmp_path, cs, "TEST")
        assert count == 0
        assert "Warning" in capsys.readouterr().err

    def test_debug_writes_html(self, tmp_path):
        exporter, _, _ = _make_exporter(debug=True)
        page = _make_page()
        cs = _make_cached_space()

        exporter._export_single_page(page, tmp_path, cs, "TEST")
        html_files = list(tmp_path.glob("*.html"))
        assert len(html_files) == 1

    def test_downloads_media(self, tmp_path):
        exporter, client, _ = _make_exporter(download_media=True)
        att = Attachment(id="a1", title="img.png", file_size=100,
                         download_link="/wiki/download/a1", page_id="p1")
        page = _make_page()
        cs = _make_cached_space(attachments={"p1": [att]})

        with patch("confluence_export.exporter.download_attachments", return_value=[]) as mock_dl:
            exporter._export_single_page(page, tmp_path, cs, "TEST")
            mock_dl.assert_called_once()


class TestExportNode:
    def test_recursive_export(self, tmp_path):
        exporter, _, _ = _make_exporter()
        parent = _make_page(id="p1", title="Parent", body="<p>Parent</p>")
        child = _make_page(id="p2", title="Child", body="<p>Child</p>",
                           parent_id="p1", parent_type="page")
        cs = _make_cached_space(pages=[parent, child])

        root = PageNode(page=parent, children=[PageNode(page=child)])
        count = exporter._export_node(root, tmp_path, cs, "TEST", depth=0)
        assert count == 2
        assert (tmp_path / "Parent" / "Parent.md").exists()
        assert (tmp_path / "Parent" / "Child" / "Child.md").exists()


class TestExportSpace:
    def test_full_export(self, tmp_path):
        exporter, client, cache = _make_exporter()
        page = _make_page()
        cs = _make_cached_space(pages=[page])
        cache.ensure_loaded.return_value = cs

        count = exporter.export_space(_make_space(), tmp_path)
        assert count == 1

    def test_path_filter_not_found(self, tmp_path, capsys):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.ensure_loaded.return_value = cs

        count = exporter.export_space(_make_space(), tmp_path, path_filter="/nonexistent")
        assert count == 0
        assert "not found" in capsys.readouterr().err

    def test_force_refresh(self, tmp_path):
        exporter, client, cache = _make_exporter()
        cs = _make_cached_space()
        cache.refresh.return_value = cs

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        cache.refresh.assert_called_once()

    def test_no_children(self, tmp_path):
        exporter, _, cache = _make_exporter()
        page = _make_page()
        cs = _make_cached_space(pages=[page])
        cache.ensure_loaded.return_value = cs

        count = exporter.export_space(_make_space(), tmp_path, no_children=True)
        assert count == 1


class TestResolveUser:
    def test_caches_user_lookup(self):
        exporter, client, _ = _make_exporter()
        client.get_user_info.return_value = {"displayName": "Alice"}

        result1 = exporter._resolve_user("acc1")
        result2 = exporter._resolve_user("acc1")
        assert result1 == {"displayName": "Alice"}
        assert result2 == {"displayName": "Alice"}
        client.get_user_info.assert_called_once_with("acc1")


class TestExportSpacePathFilter:
    def test_path_filter_with_subtree(self, tmp_path):
        exporter, _, cache = _make_exporter()
        parent = _make_page(id="p1", title="Parent", body="<p>P</p>")
        child = _make_page(id="p2", title="Child", body="<p>C</p>",
                           parent_id="p1", parent_type="page")
        cs = _make_cached_space(pages=[parent, child])
        cache.ensure_loaded.return_value = cs

        count = exporter.export_space(_make_space(), tmp_path, path_filter="/Parent")
        assert count == 2

    def test_no_children_with_path(self, tmp_path):
        exporter, _, cache = _make_exporter()
        parent = _make_page(id="p1", title="Parent", body="<p>P</p>")
        child = _make_page(id="p2", title="Child", body="<p>C</p>",
                           parent_id="p1", parent_type="page")
        cs = _make_cached_space(pages=[parent, child])
        cache.ensure_loaded.return_value = cs

        count = exporter.export_space(_make_space(), tmp_path,
                                       path_filter="/Parent", no_children=True)
        assert count == 1


class TestExportDrawio:
    def test_drawio_rendering(self, tmp_path):
        exporter, client, _ = _make_exporter(download_media=True, render_drawio=True)
        att = Attachment(id="a1", title="arch.drawio", media_type="application/x-drawio",
                         file_size=100, page_id="p1",
                         download_link="/wiki/download/a1")
        page = _make_page()
        cs = _make_cached_space(attachments={"p1": [att]})

        # Create the drawio file so render_drawio_to_png finds it
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "arch.drawio").write_text("<xml/>")

        with patch("confluence_export.exporter.download_attachments", return_value=[]), \
             patch("confluence_export.exporter.render_drawio_to_png",
                   return_value=media_dir / "arch.drawio.png"):
            count = exporter._export_single_page(page, tmp_path, cs, "TEST")
            assert count == 1


class TestPrefetchBodies:
    def test_skips_pages_with_body(self):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="<p>Already loaded</p>")
        cs = _make_cached_space(pages=[page])

        exporter._prefetch_bodies(cs)
        client.get_page_by_id.assert_not_called()

    def test_fetches_missing_bodies(self):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="")
        full = _make_page(body="<p>Fetched</p>")
        client.get_page_by_id.return_value = full
        cs = _make_cached_space(pages=[page])

        exporter._prefetch_bodies(cs)
        client.get_page_by_id.assert_called_once_with("p1")
        assert page.body_storage == "<p>Fetched</p>"
