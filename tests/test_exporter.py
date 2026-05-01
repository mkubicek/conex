"""Exporter tests: verify actual files written, not just mock call counts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_export.exporter import ExportResult, Exporter
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


def _make_page(id="p1", title="Test Page", body="<p>Hello <strong>world</strong></p>",
               parent_id="", parent_type="space"):
    return Page(
        id=id, title=title, space_id="1", body_storage=body,
        parent_id=parent_id, parent_type=parent_type,
        version=Version(created_at="2025-01-01", number=3),
        webui=f"/spaces/TEST/pages/{id}",
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


class TestExportWritesCorrectMarkdown:
    def test_markdown_contains_content_and_frontmatter(self, tmp_path):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.ensure_loaded.return_value = cs

        exporter.export_space(_make_space(), tmp_path)

        md_path = tmp_path / "Test-Page" / "Test-Page.md"
        assert md_path.exists()
        md = md_path.read_text()

        # Frontmatter
        assert "title: Test Page" in md
        assert "page_id:" in md
        assert "version: 3" in md

        # Converted content
        assert "**world**" in md

        # No raw HTML leaked
        assert "<strong>" not in md
        assert "<p>" not in md

    def test_debug_mode_writes_html_alongside(self, tmp_path):
        exporter, _, cache = _make_exporter(debug=True)
        cs = _make_cached_space()
        cache.ensure_loaded.return_value = cs

        exporter.export_space(_make_space(), tmp_path)

        html_path = tmp_path / "Test-Page" / "Test-Page.html"
        assert html_path.exists()
        assert "<strong>world</strong>" in html_path.read_text()


class TestTreeExport:
    def test_nested_pages_create_nested_directories(self, tmp_path):
        exporter, _, cache = _make_exporter()
        parent = _make_page(id="p1", title="Parent", body="<p>I am parent</p>")
        child = _make_page(id="p2", title="Child", body="<p>I am child</p>",
                           parent_id="p1", parent_type="page")
        cs = _make_cached_space(pages=[parent, child])
        cache.ensure_loaded.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path)
        assert result.count == 2
        assert len(result.written_files) == 2
        written_names = {f.name for f in result.written_files}
        assert written_names == {"Parent.md", "Child.md"}

        parent_md = (tmp_path / "Parent" / "Parent.md").read_text()
        child_md = (tmp_path / "Parent" / "Child" / "Child.md").read_text()
        assert "I am parent" in parent_md
        assert "I am child" in child_md

    def test_path_filter_exports_subtree(self, tmp_path):
        exporter, _, cache = _make_exporter()
        root = _make_page(id="p1", title="Root", body="<p>Root</p>")
        sub = _make_page(id="p2", title="Sub", body="<p>Sub page</p>",
                         parent_id="p1", parent_type="page")
        leaf = _make_page(id="p3", title="Leaf", body="<p>Leaf page</p>",
                          parent_id="p2", parent_type="page")
        cs = _make_cached_space(pages=[root, sub, leaf])
        cache.ensure_loaded.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path, path_filter="/Root/Sub")
        assert result.count == 2  # Sub + Leaf

        assert (tmp_path / "Sub" / "Sub.md").exists()
        assert (tmp_path / "Sub" / "Leaf" / "Leaf.md").exists()

    def test_path_filter_not_found_returns_zero(self, tmp_path, capsys):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.ensure_loaded.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path, path_filter="/Nonexistent")
        assert result.count == 0
        assert "not found" in capsys.readouterr().err

    def test_no_children_exports_only_roots(self, tmp_path):
        exporter, _, cache = _make_exporter()
        parent = _make_page(id="p1", title="Parent", body="<p>P</p>")
        child = _make_page(id="p2", title="Child", body="<p>C</p>",
                           parent_id="p1", parent_type="page")
        cs = _make_cached_space(pages=[parent, child])
        cache.ensure_loaded.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path, no_children=True)
        assert result.count == 1  # only the root


class TestFolderHandling:
    def test_folders_produce_no_markdown(self, tmp_path):
        exporter, _, _ = _make_exporter()
        page = _make_page()
        page.status = "folder"
        cs = _make_cached_space()

        files = exporter._export_single_page(page, tmp_path, cs, "TEST")
        assert files == []
        assert list(tmp_path.glob("*.md")) == []


class TestBodyFetching:
    def test_fetches_body_when_missing(self, tmp_path):
        """When cache has no body, exporter fetches it from the API."""
        exporter, client, _ = _make_exporter()
        page = _make_page(body="")
        full_page = _make_page(body="<p>Fetched from API</p>")
        client.get_page_by_id.return_value = full_page
        cs = _make_cached_space(pages=[page])

        exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert "Fetched from API" in md

    def test_body_fetch_failure_skips_page(self, tmp_path, capsys):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="")
        client.get_page_by_id.side_effect = Exception("timeout")
        cs = _make_cached_space(pages=[page])

        files = exporter._export_single_page(page, tmp_path, cs, "TEST")
        assert files == []
        assert "Warning" in capsys.readouterr().err

    def test_prefetch_fills_bodies_before_export(self):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="")
        full = _make_page(body="<p>Prefetched</p>")
        client.get_page_by_id.return_value = full
        cs = _make_cached_space(pages=[page])

        exporter._prefetch_bodies(cs)
        assert page.body_storage == "<p>Prefetched</p>"

    def test_prefetch_skips_pages_with_body(self):
        exporter, client, _ = _make_exporter()
        page = _make_page(body="<p>Already loaded</p>")
        cs = _make_cached_space(pages=[page])

        exporter._prefetch_bodies(cs)
        client.get_page_by_id.assert_not_called()


class TestMediaDownload:
    def test_downloads_attachments_to_media_dir(self, tmp_path):
        exporter, client, _ = _make_exporter(download_media=True)
        att = Attachment(id="a1", title="img.png", file_size=100,
                         download_link="/wiki/download/a1", page_id="p1")
        page = _make_page()
        cs = _make_cached_space(attachments={"p1": [att]})

        with patch("confluence_export.exporter.download_attachments") as mock_dl:
            mock_dl.return_value = [tmp_path / ".media" / "img.png"]
            exporter._export_single_page(page, tmp_path, cs, "TEST")
            mock_dl.assert_called_once()
            # Verify media dir was created and passed
            assert mock_dl.call_args[0][2].name == ".media"


class TestDrawioRendering:
    def test_drawio_placeholder_replaced_in_markdown(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True, render_drawio=True)
        att = Attachment(id="a1", title="arch.drawio", media_type="application/x-drawio",
                         file_size=100, page_id="p1",
                         download_link="/wiki/download/a1")
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [att]})

        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "arch.drawio").write_text("<xml/>")
        png_path = media_dir / "arch.drawio.png"

        with patch("confluence_export.exporter.download_attachments", return_value=[]), \
             patch("confluence_export.exporter.render_drawio_to_png", return_value=png_path):
            exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert "arch.drawio.png" in md
        assert "arch.drawio" in md


class TestUserResolution:
    def test_caches_repeated_lookups(self):
        exporter, client, _ = _make_exporter()
        client.get_user_info.return_value = {"displayName": "Alice"}

        assert exporter._resolve_user("u1") == {"displayName": "Alice"}
        assert exporter._resolve_user("u1") == {"displayName": "Alice"}
        client.get_user_info.assert_called_once_with("u1")  # only 1 API call

    def test_skip_author_lookup_returns_none_without_api_call(self):
        exporter, client, _ = _make_exporter(skip_author_lookup=True)

        assert exporter._resolve_user("u1") is None
        assert exporter._resolve_user("u2") is None
        client.get_user_info.assert_not_called()


class TestWorkspaceDirectory:
    def test_workspace_created_for_each_page(self, tmp_path):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.ensure_loaded.return_value = cs

        exporter.export_space(_make_space(), tmp_path)

        workspace = tmp_path / "Test-Page" / ".workspace"
        assert workspace.is_dir()

    def test_workspace_created_for_nested_pages(self, tmp_path):
        exporter, _, cache = _make_exporter()
        parent = _make_page(id="p1", title="Parent", body="<p>P</p>")
        child = _make_page(id="p2", title="Child", body="<p>C</p>",
                           parent_id="p1", parent_type="page")
        cs = _make_cached_space(pages=[parent, child])
        cache.ensure_loaded.return_value = cs

        exporter.export_space(_make_space(), tmp_path)

        assert (tmp_path / "Parent" / ".workspace").is_dir()
        assert (tmp_path / "Parent" / "Child" / ".workspace").is_dir()

    def test_workspace_preserves_existing_files(self, tmp_path):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.ensure_loaded.return_value = cs

        # First export
        exporter.export_space(_make_space(), tmp_path)

        # User adds a file to the workspace
        workspace = tmp_path / "Test-Page" / ".workspace"
        script = workspace / "aggregate.py"
        script.write_text("print('hello')")

        # Re-export
        exporter.export_space(_make_space(), tmp_path)

        # User's file is still there
        assert script.exists()
        assert script.read_text() == "print('hello')"


class TestForceRefresh:
    def test_force_refresh_calls_cache_refresh(self, tmp_path):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.refresh.return_value = cs

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        cache.refresh.assert_called_once()
        cache.ensure_loaded.assert_not_called()
