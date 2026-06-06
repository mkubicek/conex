"""Exporter tests: verify actual files written, not just mock call counts."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

import yaml

from confluence_export.exporter import ExportResult, Exporter, _write_text_atomic
from confluence_export.media import _VERSIONS_FILE
from confluence_export.protection import (
    PageExactProtection,
    ProtectionSet,
    SubtreeProtection,
)
from confluence_export.paths import attachment_identity, plan_attachment_names
from confluence_export.types import (
    Attachment,
    CachedSpace,
    Page,
    PageNode,
    Space,
    Version,
)


def _prot(*, page_exact=(), subtrees=()) -> ProtectionSet:
    """Typed ProtectionSet from plain dir lists, pinning exactly the protections a
    commit_export test intends (page-exact vs recursive scope made explicit)."""
    return ProtectionSet(
        page_exact=tuple(PageExactProtection(p) for p in page_exact),
        subtrees=tuple(SubtreeProtection(p) for p in subtrees),
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

    def test_atomic_text_write_preserves_existing_mode(self, tmp_path):
        target = tmp_path / "Page.md"
        target.write_text("old")
        target.chmod(0o640)

        _write_text_atomic(target, "new")

        assert target.read_text() == "new"
        assert target.stat().st_mode & 0o777 == 0o640

    def test_atomic_text_write_uses_umask_for_new_file(self, tmp_path):
        target = tmp_path / "Page.md"
        old_umask = os.umask(0o027)
        try:
            _write_text_atomic(target, "new")
        finally:
            os.umask(old_umask)

        assert target.read_text() == "new"
        assert target.stat().st_mode & 0o777 == 0o640


class TestConvertFailureIsolated:
    def test_one_bad_page_does_not_abort_export(self, tmp_path):
        # Defense-in-depth: a convert_page exception on one page must not abort
        # the whole space export. The bad page is skipped with a warning; every
        # other page still exports.
        exporter, _, cache = _make_exporter()
        cache.ensure_loaded.return_value = _make_cached_space(pages=[
            _make_page(id="good", title="Good Page"),
            _make_page(id="bad", title="Bad Page"),
        ])

        def flaky(page, **kwargs):
            if page.title == "Bad Page":
                raise ValueError("boom")
            return "---\ntitle: Good Page\n---\n\n# Good Page\n"

        with patch("confluence_export.exporter.convert_page", side_effect=flaky):
            result = exporter.export_space(_make_space(), tmp_path)  # must not raise

        assert (tmp_path / "Good-Page" / "Good-Page.md").exists()
        assert not (tmp_path / "Bad-Page" / "Bad-Page.md").exists()
        # The skipped page's dir is surfaced so the git prune can protect its
        # last-good committed copy instead of deleting it (#34 follow-up).
        assert (tmp_path / "Bad-Page") in result.skipped_paths
        assert (tmp_path / "Good-Page") not in result.skipped_paths

    def test_convert_failure_cleans_up_newly_downloaded_media(self, tmp_path):
        # A convert failure must not leave THIS run's freshly downloaded media
        # orphaned on disk (no untracked junk). download_media=True, one attachment
        # written, then convert raises -> the media file is cleaned up.
        exporter, _, cache = _make_exporter(download_media=True)
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="bad", title="Bad Page")],
            attachments={"bad": [Attachment(id="x", title="img.png", media_type="image/png", file_size=3)]},
        )

        def fake_download(client, attachments, media_dir):
            p = media_dir / "img.png"
            p.write_bytes(b"png")
            return [p]

        with patch("confluence_export.exporter.download_attachments", side_effect=fake_download), \
             patch("confluence_export.exporter.convert_page", side_effect=ValueError("boom")):
            exporter.export_space(_make_space(), tmp_path)

        assert not (tmp_path / "Bad-Page" / ".media" / "img.png").exists()

    def test_convert_failure_restores_existing_media_and_manifest(self, tmp_path):
        exporter, _, cache = _make_exporter(download_media=True)
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="bad", title="Bad Page")],
            attachments={"bad": [Attachment(id="x", title="img.png", media_type="image/png", file_size=3)]},
        )
        media_dir = tmp_path / "Bad-Page" / ".media"
        media_dir.mkdir(parents=True)
        image = media_dir / "img.png"
        manifest = media_dir / _VERSIONS_FILE
        image.write_bytes(b"old-good")
        manifest.write_text('{"img.png": {"version": 1, "id": "x", "title": "img.png"}}')

        def fake_download(client, attachments, media_dir):
            p = media_dir / "img.png"
            p.write_bytes(b"new-partial")
            m = media_dir / _VERSIONS_FILE
            m.write_text('{"img.png": {"version": 2, "id": "x", "title": "img.png"}}')
            return [p, m]

        with patch("confluence_export.exporter.download_attachments", side_effect=fake_download), \
             patch("confluence_export.exporter.convert_page", side_effect=ValueError("boom")):
            exporter.export_space(_make_space(), tmp_path)

        assert image.read_bytes() == b"old-good"
        assert json.loads(manifest.read_text())["img.png"]["version"] == 1

    def test_no_media_convert_failure_restores_materialization_deletions(self, tmp_path):
        exporter, _, cache = _make_exporter(download_media=False)
        moved = Attachment(id="att1", title="a/b.png", page_id="bad", version=Version(number=1))
        moved.created_at = "2025-01-01"
        current_owner = Attachment(
            id="att2", title="a-b.png", page_id="bad", version=Version(number=1)
        )
        current_owner.created_at = "2024-01-01"
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="bad", title="Bad Page")],
            attachments={"bad": [moved, current_owner]},
        )
        media_dir = tmp_path / "Bad-Page" / ".media"
        media_dir.mkdir(parents=True)
        old = media_dir / "a-b.png"
        old.write_bytes(b"att1-last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "a-b.png": {
                "version": 1,
                "id": "att1",
                "title": moved.title,
                "key": attachment_identity(moved),
            }
        }))

        with patch("confluence_export.exporter.convert_page", side_effect=ValueError("boom")):
            exporter.export_space(_make_space(), tmp_path)

        assert old.read_bytes() == b"att1-last-good"
        manifest = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert manifest["a-b.png"]["id"] == "att1"

    def test_no_media_convert_failure_restores_removed_stale_media(self, tmp_path):
        exporter, _, cache = _make_exporter(download_media=False)
        current = Attachment(
            id="current", title="current.png", page_id="bad", version=Version(number=1)
        )
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="bad", title="Bad Page")],
            attachments={"bad": [current]},
        )
        media_dir = tmp_path / "Bad-Page" / ".media"
        media_dir.mkdir(parents=True)
        (media_dir / "current.png").write_bytes(b"current")
        stale = media_dir / "stale.png"
        stale.write_bytes(b"stale-last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "current.png": {"version": 1, "id": "current", "title": "current.png"},
            "stale.png": {"version": 1, "id": "stale", "title": "stale.png"},
        }))

        with patch("confluence_export.exporter.convert_page", side_effect=ValueError("boom")):
            exporter.export_space(_make_space(), tmp_path)

        assert stale.read_bytes() == b"stale-last-good"
        manifest = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert "stale.png" in manifest

    def test_media_value_error_restores_downloaded_media_and_manifest(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True, render_drawio=True)
        att = Attachment(
            id="a1",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/a1",
            version=Version(number=2),
        )
        page = _make_page()
        cs = _make_cached_space(attachments={"p1": [att]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        source = media_dir / "arch.drawio"
        manifest = media_dir / _VERSIONS_FILE
        source.write_text("old-source")
        manifest.write_text(json.dumps({
            "arch.drawio": {"version": 1, "id": "a1", "title": "arch.drawio"}
        }))
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        (media_dir / "arch.drawio.png").symlink_to(outside)

        def fake_download(_client, _attachments, _media_dir):
            source.write_text("new-source")
            manifest.write_text(json.dumps({
                "arch.drawio": {"version": 2, "id": "a1", "title": "arch.drawio"}
            }))
            return [source, manifest]

        with patch("confluence_export.exporter.download_attachments", side_effect=fake_download):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert source.read_text() == "old-source"
        assert json.loads(manifest.read_text())["arch.drawio"]["version"] == 1
        assert outside.read_bytes() == b"outside"

    def test_markdown_write_failure_restores_downloaded_media_and_manifest(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True)
        att = Attachment(
            id="a1",
            title="img.png",
            media_type="image/png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/a1",
            version=Version(number=2),
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="img.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [att]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        image = media_dir / "img.png"
        manifest = media_dir / _VERSIONS_FILE
        image.write_bytes(b"old-image")
        manifest.write_text(json.dumps({
            "img.png": {"version": 1, "id": "a1", "title": "img.png"}
        }))

        def fake_download(_client, _attachments, _media_dir):
            image.write_bytes(b"new-image")
            manifest.write_text(json.dumps({
                "img.png": {"version": 2, "id": "a1", "title": "img.png"}
            }))
            return [image, manifest]

        original_write_text = Path.write_text

        def fail_markdown_write(path, *args, **kwargs):
            if path.name.startswith(".Test-Page.md."):
                raise OSError("disk full")
            return original_write_text(path, *args, **kwargs)

        with patch("confluence_export.exporter.download_attachments", side_effect=fake_download), \
             patch("pathlib.Path.write_text", new=fail_markdown_write):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert image.read_bytes() == b"old-image"
        assert json.loads(manifest.read_text())["img.png"]["version"] == 1

    def test_rollback_unlinks_swapped_symlink_not_target(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True)
        att = Attachment(
            id="a1",
            title="img.png",
            media_type="image/png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/a1",
            version=Version(number=2),
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="img.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [att]})
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")

        def fake_download(_client, _attachments, media_dir):
            image = media_dir / "img.png"
            image.write_bytes(b"new-image")
            image.unlink()
            image.symlink_to(outside)
            return [image]

        with patch("confluence_export.exporter.download_attachments", side_effect=fake_download), \
             patch("confluence_export.exporter.convert_page", side_effect=ValueError("boom")):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert outside.read_bytes() == b"outside"
        assert not (tmp_path / ".media" / "img.png").exists()

    def test_rollback_restores_file_without_following_swapped_symlink(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True)
        att = Attachment(
            id="a1",
            title="img.png",
            media_type="image/png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/a1",
            version=Version(number=2),
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="img.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [att]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        image = media_dir / "img.png"
        image.write_bytes(b"old-image")
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")

        def fake_download(_client, _attachments, _media_dir):
            image.unlink()
            image.symlink_to(outside)
            return [image]

        with patch("confluence_export.exporter.download_attachments", side_effect=fake_download), \
             patch("confluence_export.exporter.convert_page", side_effect=ValueError("boom")):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert outside.read_bytes() == b"outside"
        assert image.read_bytes() == b"old-image"
        assert not image.is_symlink()

    def test_media_phase_exception_restores_mutations_before_written_paths_return(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True)
        att = Attachment(
            id="a1",
            title="img.png",
            media_type="image/png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/a1",
            version=Version(number=2),
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="img.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [att]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        image = media_dir / "img.png"
        manifest = media_dir / _VERSIONS_FILE
        image.write_bytes(b"old-image")
        manifest.write_text(json.dumps({
            "img.png": {"version": 1, "id": "a1", "title": "img.png"}
        }))

        def mutate_then_raise(_client, _attachments, _media_dir):
            image.write_bytes(b"new-image")
            manifest.write_text(json.dumps({
                "img.png": {"version": 2, "id": "a1", "title": "img.png"}
            }))
            raise OSError("manifest write failed")

        with patch("confluence_export.exporter.download_attachments", side_effect=mutate_then_raise):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert image.read_bytes() == b"old-image"
        assert json.loads(manifest.read_text())["img.png"]["version"] == 1


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

    def test_symlinked_page_dir_is_skipped(self, tmp_path, capsys):
        exporter, _, cache = _make_exporter()
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="p1", title="Page", body="<p>safe</p>")]
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "Page").symlink_to(outside, target_is_directory=True)

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.count == 0
        assert not (outside / "Page.md").exists()
        assert (tmp_path / "Page") in result.skipped_paths
        assert "symlinked page directory" in capsys.readouterr().err

    def test_symlinked_page_dir_skips_descendants(self, tmp_path):
        exporter, _, cache = _make_exporter()
        parent = _make_page(id="p", title="Parent", body="<p>parent</p>")
        child = _make_page(
            id="c", title="Child", parent_id="p", parent_type="page",
            body="<p>child</p>",
        )
        cache.ensure_loaded.return_value = _make_cached_space(pages=[parent, child])
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "Parent").symlink_to(outside, target_is_directory=True)

        result = exporter.export_space(_make_space(), tmp_path)

        skipped = {p.resolve() for p in result.skipped_paths}
        assert (tmp_path / "Parent").resolve() in skipped
        assert (tmp_path / "Parent" / "Child").resolve() in skipped

    def test_symlinked_media_dir_skips_only_that_page(self, tmp_path, capsys):
        attachment = Attachment(
            id="att1", title="img.png", page_id="bad", version=Version(number=1),
            download_link="/wiki/download/img.png",
        )
        bad = _make_page(id="bad", title="Bad Page", body="<p>bad</p>")
        good = _make_page(id="good", title="Good Page", body="<p>good</p>")
        exporter, _, cache = _make_exporter(download_media=True)
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[bad, good],
            attachments={"bad": [attachment]},
        )
        bad_dir = tmp_path / "Bad-Page"
        bad_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (bad_dir / ".media").symlink_to(outside, target_is_directory=True)

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.count == 1
        assert (tmp_path / "Good-Page" / "Good-Page.md").exists()
        assert bad_dir in result.skipped_paths
        assert "symlinked media directory" in capsys.readouterr().err

    def test_symlinked_media_dir_is_rejected_before_snapshot_walk(
        self, tmp_path, monkeypatch, capsys
    ):
        attachment = Attachment(
            id="att1", title="img.png", page_id="bad", version=Version(number=1),
            download_link="/wiki/download/img.png",
        )
        bad = _make_page(id="bad", title="Bad Page", body="<p>bad</p>")
        good = _make_page(id="good", title="Good Page", body="<p>good</p>")
        exporter, _, cache = _make_exporter(download_media=True)
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[bad, good],
            attachments={"bad": [attachment]},
        )
        bad_dir = tmp_path / "Bad-Page"
        bad_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (bad_dir / ".media").symlink_to(outside, target_is_directory=True)

        original_rglob = Path.rglob

        def fail_if_symlinked_media_root_is_walked(path, pattern):
            if path.name == ".media" and path.is_symlink():
                raise AssertionError("snapshot walked symlinked media root")
            return original_rglob(path, pattern)

        monkeypatch.setattr(Path, "rglob", fail_if_symlinked_media_root_is_walked)

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.count == 1
        assert (tmp_path / "Good-Page" / "Good-Page.md").exists()
        assert bad_dir in result.skipped_paths
        assert "symlinked media directory" in capsys.readouterr().err

    def test_symlinked_media_file_skips_page_without_touching_target(self, tmp_path, capsys):
        attachment = Attachment(
            id="att1", title="img.png", page_id="bad", version=Version(number=1),
        )
        exporter, _, cache = _make_exporter(download_media=False)
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="bad", title="Bad Page", body="<p>bad</p>")],
            attachments={"bad": [attachment]},
        )
        media_dir = tmp_path / "Bad-Page" / ".media"
        media_dir.mkdir(parents=True)
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        (media_dir / "img.png").symlink_to(outside)
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "img.png": {"version": 1, "id": "att1", "title": "img.png"}
        }))

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.count == 0
        assert outside.read_bytes() == b"outside"
        assert "symlinked path component" in capsys.readouterr().err

    def test_no_media_empty_attachment_list_marks_existing_media_for_prune(self, tmp_path):
        exporter, _, cache = _make_exporter(download_media=False)
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="p1", title="Page", body="<p>safe</p>")],
            attachments={"p1": []},
        )
        media_dir = tmp_path / "Page" / ".media"
        media_dir.mkdir(parents=True)
        (media_dir / "gone.png").write_bytes(b"gone")

        result = exporter.export_space(_make_space(), tmp_path)

        assert (tmp_path / "Page" / "Page.md").exists()
        assert media_dir.resolve() in {p.resolve() for p in result.prune_media_dirs}


class TestCollisionHandling:
    def test_sibling_title_collision_does_not_overwrite(self, tmp_path):
        # Two distinct titles that sanitize to the same name must land in two
        # distinct directories instead of the second silently overwriting the
        # first (issue #11).
        exporter, _, cache = _make_exporter()
        a = _make_page(id="p1", title="page one", body="<p>First page</p>")
        b = _make_page(id="p2", title="page-one", body="<p>Second page</p>")
        cs = _make_cached_space(pages=[a, b])
        cache.ensure_loaded.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.count == 2
        first = tmp_path / "page-one" / "page-one.md"
        second = tmp_path / "page-one-2" / "page-one-2.md"
        assert first.exists() and second.exists()
        first_text, second_text = first.read_text(), second.read_text()
        assert "First page" in first_text and "page_id: p1" in first_text
        assert "Second page" in second_text and "page_id: p2" in second_text


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

    def test_no_media_materializes_planned_name_from_existing_manifest(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=False)
        moved = Attachment(
            id="att1",
            title="a/b.png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/att1",
        )
        collision = Attachment(
            id="att2",
            title="a-b.png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/att2",
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="a/b.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [moved, collision]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        old_name = "a-b.png"
        new_name = plan_attachment_names([moved, collision]).for_attachment(moved)
        assert new_name != old_name
        (media_dir / old_name).write_bytes(b"old-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": moved.title,
                "key": attachment_identity(moved),
            }
        }))

        exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert f".media/{new_name}" in md
        assert (media_dir / new_name).read_bytes() == b"old-good"

    def test_no_media_links_recovered_old_owner_when_planned_name_occupied(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=False)
        moved = Attachment(
            id="att1",
            title="new.png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/att1",
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="new.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [moved]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "old.png").write_bytes(b"last-good")
        (media_dir / "new.png").write_bytes(b"wrong-owner")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "old.png": {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            },
            "new.png": {
                "version": 1,
                "id": "other",
                "title": "new.png",
            },
        }))

        exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert ".media/new.png" in md
        assert (media_dir / "new.png").read_bytes() == b"last-good"

    def test_no_media_links_present_unmanifested_attachment_without_collision(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=False)
        att = Attachment(
            id="att1",
            title="img.png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/att1",
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="img.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [att]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"present")

        exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert ".media/img.png" in md
        assert "Missing attachment" not in md

    def test_failed_download_links_recovered_old_owner_when_planned_name_occupied(self, tmp_path):
        exporter, client, _ = _make_exporter(download_media=True)
        moved = Attachment(
            id="att1",
            title="new.png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/att1",
            version=Version(number=2),
        )
        page = _make_page(body='<ac:image><ri:attachment ri:filename="new.png"/></ac:image>')
        cs = _make_cached_space(attachments={"p1": [moved]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "old.png").write_bytes(b"last-good")
        (media_dir / "new.png").write_bytes(b"wrong-owner")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "old.png": {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            },
            "new.png": {
                "version": 1,
                "id": "other",
                "title": "new.png",
            },
        }))
        client.download_attachment_to_file.side_effect = Exception("network error")

        exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert ".media/new.png" in md
        assert (media_dir / "new.png").read_bytes() == b"last-good"

    def test_no_media_materialization_rolls_back_when_conversion_fails(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=False)
        moved = Attachment(
            id="att1", title="a/b.png", file_size=100, page_id="p1",
            download_link="/wiki/download/att1",
        )
        collision = Attachment(
            id="att2", title="a-b.png", file_size=100, page_id="p1",
            download_link="/wiki/download/att2",
        )
        page = _make_page(body="<p>bad</p>")
        cs = _make_cached_space(attachments={"p1": [moved, collision]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        old_name = "a-b.png"
        new_name = plan_attachment_names([moved, collision]).for_attachment(moved)
        (media_dir / old_name).write_bytes(b"old-good")
        manifest = media_dir / _VERSIONS_FILE
        original_manifest = json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": moved.title,
                "key": attachment_identity(moved),
            }
        })
        manifest.write_text(original_manifest)

        with patch("confluence_export.exporter.convert_page", side_effect=Exception("bad")):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert manifest.read_text() == original_manifest
        assert not (media_dir / new_name).exists()


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

    def test_drawio_render_output_avoids_attachment_name_collision(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True, render_drawio=True)
        drawio = Attachment(
            id="draw",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/draw",
        )
        colliding_png = Attachment(
            id="png",
            title="arch.drawio.png",
            media_type="image/png",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/png",
        )
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [drawio, colliding_png]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "arch.drawio").write_text("<xml/>")
        (media_dir / "arch.drawio.png").write_bytes(b"real attachment")

        def fake_render(_drawio_file, output_path=None, **_kwargs):
            assert output_path is not None
            assert output_path.name != "arch.drawio.png"
            output_path.write_bytes(b"rendered")
            return output_path

        with patch("confluence_export.exporter.download_attachments", return_value=[]), \
             patch("confluence_export.exporter.render_drawio_to_png", side_effect=fake_render):
            exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert ".media/arch-draw-render.drawio.png" in md
        assert "](.media/arch.drawio.png)" not in md

    def test_drawio_render_forces_existing_non_attachment_output_when_source_is_newer(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True, render_drawio=True)
        drawio = Attachment(
            id="draw",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/draw",
        )
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [drawio]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        source = media_dir / "arch.drawio"
        source.write_text("<xml/>")
        stale_png = media_dir / "arch.drawio.png"
        stale_png.write_bytes(b"stale attachment")
        os.utime(stale_png, (source.stat().st_mtime - 10, source.stat().st_mtime - 10))

        with patch("confluence_export.exporter.download_attachments", return_value=[]), \
             patch("confluence_export.exporter.render_drawio_to_png", return_value=stale_png) as render:
            exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert render.call_args.kwargs["force"] is True

    def test_drawio_render_reuses_existing_fresh_output(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True, render_drawio=True)
        drawio = Attachment(
            id="draw",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/draw",
        )
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [drawio]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        source = media_dir / "arch.drawio"
        source.write_text("<xml/>")
        png = media_dir / "arch.drawio.png"
        png.write_bytes(b"fresh render")
        os.utime(png, (source.stat().st_mtime + 10, source.stat().st_mtime + 10))

        with patch("confluence_export.exporter.download_attachments", return_value=[]), \
             patch("confluence_export.exporter.render_drawio_to_png", return_value=png) as render:
            exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert render.call_args.kwargs["force"] is False

    def test_drawio_render_skips_wrong_owner_source_file(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=False, render_drawio=True)
        drawio = Attachment(
            id="draw",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/draw",
        )
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [drawio]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "arch.drawio").write_text("<old xml/>")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "arch.drawio": {
                "version": 1,
                "id": "other",
                "title": "arch.drawio",
            }
        }))

        with patch("confluence_export.exporter.render_drawio_to_png") as render:
            exporter._export_single_page(page, tmp_path, cs, "TEST")

        render.assert_not_called()
        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert "arch.drawio.png" not in md
        assert "Draw.io diagram not rendered: arch.drawio" in md
        assert "Draw.io source:" not in md

    def test_drawio_render_avoids_deleted_attachment_manifest_name(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=False, render_drawio=True)
        drawio = Attachment(
            id="draw",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/draw",
            version=Version(number=1),
        )
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [drawio]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "arch.drawio").write_text("<xml/>")
        stale_deleted_attachment = media_dir / "arch.drawio.png"
        stale_deleted_attachment.write_bytes(b"old attachment")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "arch.drawio": {
                "version": 1,
                "id": "draw",
                "title": "arch.drawio",
                "key": attachment_identity(drawio),
            },
            "arch.drawio.png": {
                "version": 1,
                "id": "deleted",
                "title": "arch.drawio.png",
            },
        }))

        def fake_render(_drawio_file, output_path=None, **_kwargs):
            assert output_path is not None
            assert output_path.name != "arch.drawio.png"
            output_path.write_bytes(b"rendered")
            return output_path

        with patch("confluence_export.exporter.render_drawio_to_png", side_effect=fake_render):
            exporter._export_single_page(page, tmp_path, cs, "TEST")

        md = list(tmp_path.glob("*.md"))[0].read_text()
        assert ".media/arch-draw-render.drawio.png" in md
        assert ".media/arch.drawio.png" not in md
        assert stale_deleted_attachment.read_bytes() == b"old attachment"

    def test_forced_drawio_render_rolls_back_when_conversion_fails(self, tmp_path):
        exporter, _, _ = _make_exporter(download_media=True, render_drawio=True)
        drawio = Attachment(
            id="draw",
            title="arch.drawio",
            media_type="application/x-drawio",
            file_size=100,
            page_id="p1",
            download_link="/wiki/download/draw",
        )
        page = _make_page(body=(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
            "</ac:structured-macro>"
        ))
        cs = _make_cached_space(attachments={"p1": [drawio]})
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        source = media_dir / "arch.drawio"
        source.write_text("<xml/>")
        png = media_dir / "arch.drawio.png"
        png.write_bytes(b"stale")
        os.utime(png, (source.stat().st_mtime - 10, source.stat().st_mtime - 10))

        def fake_render(_drawio_file, output_path=None, **_kwargs):
            output_path.write_bytes(b"fresh")
            return output_path

        with patch("confluence_export.exporter.download_attachments", return_value=[]), \
             patch("confluence_export.exporter.render_drawio_to_png", side_effect=fake_render), \
             patch("confluence_export.exporter.convert_page", side_effect=Exception("bad")):
            result = exporter._export_single_page(page, tmp_path, cs, "TEST")

        assert result == []
        assert png.read_bytes() == b"stale"


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

    def test_cached_include_archived_requests_archive_capable_cache(self, tmp_path):
        exporter, client, cache = _make_exporter()
        cs = _make_cached_space(
            pages=[
                _make_page(),
                _make_page(id="p2", title="Archived Page", body="<p>Old</p>"),
            ]
        )
        cs.pages[1].status = "archived"
        cs.include_archived = True
        cache.ensure_loaded.return_value = cs

        exporter.export_space(_make_space(), tmp_path, include_archived=True)

        cache.ensure_loaded.assert_called_once_with(
            client, _make_space(), include_archived=True
        )
        cache.refresh.assert_not_called()


def _seed_export_page(output_dir, rel, page_id, *, workspace=None):
    """Write a minimal prior-export page directory (frontmatter + optional workspace)."""
    rp = PurePosixPath(rel)
    leaf = rp.name
    page_dir = output_dir.joinpath(*rp.parts)
    page_dir.mkdir(parents=True, exist_ok=True)
    meta = {"title": leaf, "page_id": page_id, "space_key": "TEST",
            "path": "/" + rel, "version": 1}
    (page_dir / f"{leaf}.md").write_text(
        f"---\n{yaml.dump(meta, sort_keys=False)}---\n\n# {leaf}\n\nold body\n"
    )
    if workspace is not None:
        ws = page_dir / ".workspace"
        ws.mkdir(exist_ok=True)
        (ws / "u.txt").write_text(workspace)
    return page_dir


class TestMoveHealingEndToEnd:
    def test_reparented_page_rewritten_at_new_path_workspace_left(self, tmp_path, capsys):
        # A user who exported with an old version has P under A, with workspace
        # content. After P is reparented under B, a normal full export rewrites the
        # page at B/P; the user's .workspace is NOT carried (Option B) — it is left
        # at the old path and the user is told where the page went.
        exporter, _, cache = _make_exporter()
        _seed_export_page(tmp_path, "A", "a")
        _seed_export_page(tmp_path, "A/P", "p", workspace="mine")

        a = _make_page(id="a", title="A")
        b = _make_page(id="b", title="B")
        p = _make_page(id="p", title="P", parent_id="b", parent_type="page")
        cs = _make_cached_space(pages=[a, b, p])
        cache.refresh.return_value = cs

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)

        assert (tmp_path / "B" / "P" / "P.md").exists()             # rewritten at new path
        assert not (tmp_path / "A" / "P" / "P.md").exists()         # stale md dropped
        assert (tmp_path / "A" / "P" / ".workspace" / "u.txt").read_text() == "mine"  # left
        assert "do not move automatically" in capsys.readouterr().err

    def test_cached_full_export_also_reconciles(self, tmp_path):
        # #27: reconcile must run on a full export even WITHOUT --force-refresh
        # (force_refresh=False loads from cache). Otherwise a moved page's old
        # path is left as an on-disk orphan while git prunes only its tracked
        # files. The page should be rewritten at the new path and the old shell
        # cleaned, healing to the cached plan the write walk is also using.
        exporter, _, cache = _make_exporter()
        _seed_export_page(tmp_path, "A", "a")
        _seed_export_page(tmp_path, "A/P", "p")  # old path on disk
        cache.ensure_loaded.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A"),
            _make_page(id="b", title="B"),
            _make_page(id="p", title="P", parent_id="b", parent_type="page"),
        ])

        exporter.export_space(_make_space(), tmp_path)  # no force_refresh (cached)

        assert (tmp_path / "B" / "P" / "P.md").exists()  # written at new path
        assert not (tmp_path / "A" / "P").exists()        # old orphan cleaned (was left pre-#27)


class TestSelfNestEndToEnd:
    def test_reparent_into_own_name_no_crash_leaves_workspace(self, tmp_path):
        # p2 was the top-level "Eps"; a different new page p1 is now titled "Eps"
        # and p2 is reparented under it (target Eps/Bar). The export must not
        # crash or lose data: reconcile drops p2's stale md and leaves its
        # .workspace at the old path; the write walk recreates both pages fresh.
        exporter, _, cache = _make_exporter()
        _seed_export_page(tmp_path, "Eps", "p2", workspace="keep")
        p1 = _make_page(id="p1", title="Eps", body="<p>parent</p>")
        p2 = _make_page(id="p2", title="Bar", parent_id="p1", parent_type="page",
                        body="<p>child</p>")
        cs = _make_cached_space(pages=[p1, p2])
        cache.refresh.return_value = cs

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)

        assert "parent" in (tmp_path / "Eps" / "Eps.md").read_text()        # p1
        assert "child" in (tmp_path / "Eps" / "Bar" / "Bar.md").read_text()  # p2 written at new path
        # p2's workspace stays at its old path (now under p1's dir), not carried.
        assert (tmp_path / "Eps" / ".workspace" / "u.txt").read_text() == "keep"


class TestReconcileWriterHandshake:
    def test_swap_writes_each_page_at_its_new_path_workspaces_stay_put(self, tmp_path):
        # Two siblings swap names. Each page's content is rewritten fresh at its NEW
        # path, but the user workspaces are NOT swapped — each stays at its old
        # physical path (Option B; the user is warned).
        exporter, _, cache = _make_exporter()
        _seed_export_page(tmp_path, "Alpha", "a", workspace="A-ws")
        _seed_export_page(tmp_path, "Beta", "b", workspace="B-ws")
        a = _make_page(id="a", title="Beta", body="<p>content A</p>")
        b = _make_page(id="b", title="Alpha", body="<p>content B</p>")
        cs = _make_cached_space(pages=[a, b])
        cache.refresh.return_value = cs

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)

        # Content swaps (a -> Beta, b -> Alpha); workspaces stay where they were.
        assert "content A" in (tmp_path / "Beta" / "Beta.md").read_text()
        assert "content B" in (tmp_path / "Alpha" / "Alpha.md").read_text()
        assert (tmp_path / "Alpha" / ".workspace" / "u.txt").read_text() == "A-ws"
        assert (tmp_path / "Beta" / ".workspace" / "u.txt").read_text() == "B-ws"

    def test_git_move_preserves_follow_history_workspace_left_at_old_path(self, tmp_path):
        # End-to-end proof of the Option B design in a git dir: a reparented page is
        # rewritten fresh at its new path + the old path git-rm'd, and git's own
        # rename detection makes `git log --follow` cross the move — no `git mv`,
        # no sidecar relocation. The user's .workspace is left at the old path.
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        ensure_repo(tmp_path)
        exporter, _, cache = _make_exporter()
        body = "<p>" + "a substantial body of content for git rename detection. " * 4 + "</p>"

        # Export 1: P under A.
        cache.refresh.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A", body="<p>parent</p>"),
            _make_page(id="p", title="P", parent_id="a", parent_type="page", body=body),
        ])
        r1 = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(tmp_path, r1.written_files, "TEST")
        (tmp_path / "A" / "P" / ".workspace" / "prep.py").write_text("print(1)")

        # Export 2: P reparented under a new page B.
        cache.refresh.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A", body="<p>parent</p>"),
            _make_page(id="b", title="B", body="<p>new parent</p>"),
            _make_page(id="p", title="P", parent_id="b", parent_type="page", body=body),
        ])
        r2 = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(tmp_path, r2.written_files, "TEST")

        assert (tmp_path / "B" / "P" / "P.md").exists()
        # The user's prep files are left at the old path (not carried to B/P).
        assert (tmp_path / "A" / "P" / ".workspace" / "prep.py").read_text() == "print(1)"
        assert not (tmp_path / "A" / "P" / "P.md").exists()  # stale md dropped
        follow = subprocess.run(
            ["git", "log", "--follow", "--oneline", "--", "B/P/P.md"],
            cwd=tmp_path, capture_output=True, text=True,
        ).stdout
        assert len(follow.strip().splitlines()) >= 2  # history crosses the move (rename detected)

    def test_committed_workspace_stays_tracked_at_old_path_on_move(self, tmp_path):
        # Option B is deliberate even for a COMMITTED .workspace: conex does not
        # move it. On a page move the committed workspace stays tracked at its old
        # path (the prune skips .workspace); the user moves it themselves if they
        # want. The page markdown still follows the move as a git rename.
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        def _git(*a):
            subprocess.run(["git", *a], cwd=tmp_path, capture_output=True)

        ensure_repo(tmp_path)
        exporter, _, cache = _make_exporter()
        body = "<p>" + "a substantial body for git rename detection. " * 4 + "</p>"
        cache.refresh.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A", body="<p>p</p>"),
            _make_page(id="p", title="P", parent_id="a", parent_type="page", body=body),
        ])
        r1 = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(tmp_path, r1.written_files, "TEST")
        # The user commits a workspace prep file.
        (tmp_path / "A" / "P" / ".workspace" / "prep.py").write_text("print(1)")
        _git("add", "A/P/.workspace")
        _git("commit", "-m", "track workspace")

        # Reparent P under B.
        cache.refresh.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A", body="<p>p</p>"),
            _make_page(id="b", title="B", body="<p>np</p>"),
            _make_page(id="p", title="P", parent_id="b", parent_type="page", body=body),
        ])
        r2 = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(tmp_path, r2.written_files, "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "A/P/.workspace/prep.py" in ls        # stays tracked at the OLD path
        assert "B/P/.workspace/prep.py" not in ls     # NOT carried to the new path
        assert "B/P/P.md" in ls                       # the page itself moved
        assert "A/P/P.md" not in ls

    def test_moved_page_convert_failure_keeps_last_good_old_path(self, tmp_path):
        # M2/F8: a page that BOTH moved and failed to regenerate this run (empty
        # cached body + offline refetch) must keep its last-good committed export.
        # reconcile drops the old path before the write walk; the page is then
        # skipped, so its files are absent from written_files. The git prune must
        # NOT delete the old committed copy — the old path is protected too, not
        # just the new (failed) page_dir.
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        ensure_repo(tmp_path)
        exporter, client, cache = _make_exporter()
        body = "<p>" + "stable body for export. " * 6 + "</p>"
        cache.refresh.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A", body="<p>p</p>"),
            _make_page(id="p", title="P", parent_id="a", parent_type="page", body=body),
        ])
        r1 = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(tmp_path, r1.written_files, "TEST")
        assert (tmp_path / "A" / "P" / "P.md").exists()

        # Reparent P under B; its cached body is now empty and the refetch fails
        # (offline) -> P is skipped AFTER reconcile already dropped A/P.
        cache.refresh.return_value = _make_cached_space(pages=[
            _make_page(id="a", title="A", body="<p>p</p>"),
            _make_page(id="b", title="B", body="<p>np</p>"),
            _make_page(id="p", title="P", parent_id="b", parent_type="page", body=""),
        ])
        client.get_page_by_id.side_effect = RuntimeError("offline")
        r2 = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(
            tmp_path, r2.written_files, "TEST",
            protection=_prot(
                page_exact=r2.preserved_page_paths,
                subtrees=r2.preserved_paths + r2.skipped_paths,
            ),
        )

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert "A/P/P.md" in ls, "moved+failed page lost its last-good committed export"

    def test_real_page_titled_archived_not_spuriously_moved(self, tmp_path):
        # A real live page titled "_archived" loses the bare name to the synthetic
        # __archived__ container (gets "_archived-2"). The reconcile plan must
        # agree with the write-walk plan on its target, or it churns its workspace.
        exporter, _, cache = _make_exporter()
        _seed_export_page(tmp_path, "_archived-2", "123", workspace="keep")
        live = _make_page(id="123", title="_archived", body="<p>real</p>")
        archived = _make_page(id="z", title="Zarch", body="<p>old</p>")
        archived.status = "archived"
        cache.refresh.return_value = _make_cached_space(pages=[live, archived])

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)  # no include_archived

        assert (tmp_path / "_archived-2" / ".workspace" / "u.txt").read_text() == "keep"
        assert not (tmp_path / "_archived" / ".workspace").exists()  # not relocated into the container

    def test_full_export_without_archived_preserves_archived_subtree(self, tmp_path):
        # M1: a full export WITHOUT --include-archived does not write archived
        # pages, so their committed files are absent from written_files. The
        # exporter surfaces exact archived page dirs in preserved_page_paths so
        # the git prune does not delete a prior --include-archived export while
        # still allowing descendants that moved live to be pruned.
        exporter, _, cache = _make_exporter()
        live = _make_page(id="p1", title="Live")
        archived = _make_page(id="z", title="Zarch", body="<p>old</p>")
        archived.status = "archived"
        cache.ensure_loaded.return_value = _make_cached_space(pages=[live, archived])

        result = exporter.export_space(_make_space(), tmp_path)  # no include_archived

        preserved = [p.resolve() for p in result.preserved_page_paths]
        assert (tmp_path / "_archived" / "Zarch").resolve() in preserved
        assert result.preserved_paths == []

    def test_archived_only_export_preserves_archived_pages(self, tmp_path):
        # ARCH-ONLY: every page is archived and authoritative, so a default export
        # writes nothing but must surface the archived dirs exactly so they are
        # protected if a later run does prune. Per Decision 1 a write-less run does
        # NOT itself prune committed live pages (see the cli/git prune gate).
        exporter, _, cache = _make_exporter()
        archived = _make_page(id="z", title="Zarch", body="<p>old</p>")
        archived.status = "archived"
        cache.ensure_loaded.return_value = _make_cached_space(pages=[archived])

        result = exporter.export_space(_make_space(), tmp_path)  # no include_archived

        assert result.count == 0
        assert result.written_files == []
        assert result.preserved_page_paths
        assert result.preserved_paths == []

    def test_default_export_omits_archived_descendant_under_live_parent(self, tmp_path):
        exporter, _, cache = _make_exporter()
        live = _make_page(id="p1", title="Live")
        archived = _make_page(
            id="z", title="Old Child", parent_id="p1", parent_type="page",
            body="<p>old</p>",
        )
        archived.status = "archived"
        cache.ensure_loaded.return_value = _make_cached_space(pages=[live, archived])

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.count == 1
        assert (tmp_path / "Live" / "Live.md").exists()
        assert not (tmp_path / "Live" / "Old-Child").exists()
        assert (tmp_path / "_archived" / "Old-Child").resolve() in {
            p.resolve() for p in result.preserved_page_paths
        }

    def test_default_export_writes_live_child_of_archived_parent(self, tmp_path):
        # PR3: when the cache DOES see the archived parent (v2 / authoritative), a
        # LIVE child of that archived parent must still be exported (surfaced to a
        # root), not silently dropped with the omitted _archived subtree. The
        # archived parent itself is still preserved, not written.
        exporter, _, cache = _make_exporter()
        archived_parent = _make_page(id="ap", title="Archived Parent", body="<p>old</p>")
        archived_parent.status = "archived"
        live_child = _make_page(
            id="lc", title="Live Child", parent_id="ap", parent_type="page",
            body="<p>current</p>",
        )
        cs = _make_cached_space(pages=[archived_parent, live_child])
        cs.include_archived = True  # cache authoritatively sees archived pages
        cache.ensure_loaded.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path)  # no --include-archived

        # the live child is exported at top level (surfaced to a root), and the
        # archived parent is preserved page-exact (not written under _archived).
        assert (tmp_path / "Live-Child" / "Live-Child.md").exists()
        assert not (tmp_path / "_archived" / "Archived-Parent").exists()
        assert (tmp_path / "_archived" / "Archived-Parent").resolve() in {
            p.resolve() for p in result.preserved_page_paths
        }

    def test_git_export_surfaces_buried_live_child_and_prunes_old_path(self, tmp_path):
        # PR3 end-to-end WITH git: a live child previously committed under the buried
        # path _archived/Archived-Parent/Live-Child surfaces to top-level Live-Child/
        # on a v2 (authoritative) run, the archived parent stays preserved, and the
        # old nested path is PRUNED — no data loss, no stale duplicate. This drives
        # the exact PR3 branch (tree.py: live child whose parent IS present and
        # archived), which the shape tests above and the empty-parent move tests
        # below do not exercise.
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        ensure_repo(tmp_path)
        _seed_export_page(tmp_path, "_archived/Archived-Parent", "ap")
        _seed_export_page(tmp_path, "_archived/Archived-Parent/Live-Child", "lc")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "buried live child"], cwd=tmp_path, capture_output=True)

        exporter, _, cache = _make_exporter()
        archived_parent = _make_page(id="ap", title="Archived Parent", body="<p>old</p>")
        archived_parent.status = "archived"
        live_child = _make_page(
            id="lc", title="Live Child", parent_id="ap", parent_type="page",
            body="<p>current</p>",
        )
        cs = _make_cached_space(pages=[archived_parent, live_child])
        cs.include_archived = True  # v2 / authoritative: the archived parent IS fetched
        cache.refresh.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(
            tmp_path, result.written_files, "TEST",
            is_full=True, protection=result.protection(tmp_path),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Live-Child/Live-Child.md" in ls  # surfaced to top level
        assert "_archived/Archived-Parent/Archived-Parent.md" in ls  # parent preserved
        assert "_archived/Archived-Parent/Live-Child/Live-Child.md" not in ls  # old path pruned

    def test_default_export_preserves_legacy_archived_descendant_path(self, tmp_path):
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        ensure_repo(tmp_path)
        old_dir = _seed_export_page(tmp_path, "Live/Old-Child", "z")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "archived child old layout"], cwd=tmp_path, capture_output=True)

        exporter, _, cache = _make_exporter()
        live = _make_page(id="p1", title="Live")
        archived = _make_page(
            id="z", title="Old Child", parent_id="p1", parent_type="page",
            body="<p>old</p>",
        )
        archived.status = "archived"
        cache.refresh.return_value = _make_cached_space(pages=[live, archived])

        result = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(
            tmp_path,
            result.written_files,
            "TEST",
            is_full=True,
            protection=_prot(
                page_exact=result.preserved_page_paths,
                subtrees=result.preserved_paths,
            ),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Live/Old-Child/Old-Child.md" in ls
        assert old_dir.exists()

    def test_archived_old_path_reused_by_live_page_is_not_preserved(self, tmp_path):
        old_dir = _seed_export_page(tmp_path, "Live/Old-Child", "z")
        (old_dir / ".media").mkdir()
        (old_dir / ".media" / "archived.png").write_bytes(b"archived")
        exporter, _, cache = _make_exporter()
        live = _make_page(id="p1", title="Live")
        live_child = _make_page(
            id="new", title="Old Child", parent_id="p1", parent_type="page",
            body="<p>new</p>",
        )
        archived = _make_page(
            id="z", title="Old Child", parent_id="p1", parent_type="page",
            body="<p>old</p>",
        )
        archived.status = "archived"
        cache.refresh.return_value = _make_cached_space(pages=[live, live_child, archived])

        result = exporter.export_space(_make_space(), tmp_path, force_refresh=True)

        assert old_dir.resolve() not in {
            p.resolve() for p in result.preserved_page_paths
        }
        assert old_dir.resolve() not in {p.resolve() for p in result.preserved_paths}

    def test_no_archived_pages_means_no_preserved_paths(self, tmp_path):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space(pages=[_make_page()])
        cs.include_archived = True  # cache provably covers archived; none exist
        cache.ensure_loaded.return_value = cs
        result = exporter.export_space(_make_space(), tmp_path)
        assert result.preserved_page_paths == []
        assert result.preserved_paths == []

    def test_archived_preserved_when_cache_omits_archived(self, tmp_path):
        # RF-A: a current-only refresh (cookie_v1, or any dialect that doesn't
        # return archived) has no __archived__ node in the plan. A prior
        # --include-archived export's _archived/ subtree on disk must still be
        # preserved from the prune — we cannot see those pages this run.
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space(pages=[_make_page(id="p1", title="Live")])
        cs.include_archived = False  # current-only provenance
        cache.ensure_loaded.return_value = cs
        (tmp_path / "_archived" / "Old").mkdir(parents=True)
        (tmp_path / "_archived" / "Old" / "Old.md").write_text("# Old")

        result = exporter.export_space(_make_space(), tmp_path)  # no include_archived

        preserved = [p.resolve() for p in result.preserved_paths]
        assert (tmp_path / "_archived").resolve() in preserved

    def test_cache_without_archived_exports_to_missing_output_dir(self, tmp_path):
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space(pages=[_make_page(id="p1", title="Live")])
        cs.include_archived = False
        cache.ensure_loaded.return_value = cs
        output_dir = tmp_path / "new-export"

        result = exporter.export_space(_make_space(), output_dir)

        assert result.count == 1
        assert (output_dir / "Live" / "Live.md").exists()

    def test_collision_suffixed_archived_preserved_when_cache_omits_archived(self, tmp_path):
        exporter, _, cache = _make_exporter()
        live = _make_page(id="live", title="_archived")
        cs = _make_cached_space(pages=[live])
        cs.include_archived = False
        cache.ensure_loaded.return_value = cs
        (tmp_path / "_archived-2" / "Old").mkdir(parents=True)
        (tmp_path / "_archived-2" / "Old" / "Old.md").write_text("# Old")

        result = exporter.export_space(_make_space(), tmp_path)

        preserved = [p.resolve() for p in result.preserved_paths]
        assert (tmp_path / "_archived-2").resolve() in preserved
        assert (tmp_path / "_archived").resolve() not in preserved

    def test_live_archived_named_page_not_preserved_as_archived_subtree(self, tmp_path):
        exporter, _, cache = _make_exporter()
        live = _make_page(id="live", title="_archived", body="<p>live</p>")
        cs = _make_cached_space(pages=[live])
        cs.include_archived = False
        cache.ensure_loaded.return_value = cs
        (tmp_path / "_archived" / "Stale").mkdir(parents=True)
        (tmp_path / "_archived" / "Stale" / "Stale.md").write_text("# stale")

        result = exporter.export_space(_make_space(), tmp_path)

        assert result.preserved_paths == []

    def test_real_archived_root_preserved_when_live_page_claims_archived_name(self, tmp_path):
        exporter, _, cache = _make_exporter()
        live = _make_page(id="live", title="_archived", body="<p>live</p>")
        cs = _make_cached_space(pages=[live])
        cs.include_archived = False
        cache.ensure_loaded.return_value = cs
        archived_dir = tmp_path / "_archived" / "Old"
        archived_dir.mkdir(parents=True)
        (archived_dir / "Old.md").write_text(
            "---\n"
            "title: Old\n"
            "page_id: old\n"
            "space_key: TEST\n"
            "path: /_archived/Old\n"
            "status: archived\n"
            "version: 1\n"
            "---\n\n# Old\n"
        )

        result = exporter.export_space(_make_space(), tmp_path)

        assert archived_dir.resolve() in {
            p.resolve() for p in result.preserved_paths
        }
        assert (tmp_path / "_archived").resolve() not in {
            p.resolve() for p in result.preserved_paths
        }

    def test_legacy_archived_root_with_original_path_preserved_on_live_name_conflict(
        self, tmp_path
    ):
        exporter, _, cache = _make_exporter()
        live = _make_page(id="live", title="_archived", body="<p>live</p>")
        cs = _make_cached_space(pages=[live])
        cs.include_archived = False
        cache.ensure_loaded.return_value = cs
        archived_dir = tmp_path / "_archived" / "Old"
        archived_dir.mkdir(parents=True)
        (archived_dir / "Old.md").write_text(
            "---\n"
            "title: Old\n"
            "page_id: old\n"
            "space_key: TEST\n"
            "path: /Original/Old\n"
            "version: 1\n"
            "---\n\n# Old\n"
        )

        result = exporter.export_space(_make_space(), tmp_path)

        assert archived_dir.resolve() in {
            p.resolve() for p in result.preserved_paths
        }

    def test_archived_preservation_does_not_protect_live_archived_root_from_prune(
        self, tmp_path
    ):
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        ensure_repo(tmp_path)
        stale_file = tmp_path / "_archived" / "stale.txt"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_text("stale")
        archived_dir = tmp_path / "_archived" / "Old"
        archived_dir.mkdir(parents=True)
        (archived_dir / "Old.md").write_text(
            "---\n"
            "title: Old\n"
            "page_id: old\n"
            "space_key: TEST\n"
            "path: /_archived/Old\n"
            "status: archived\n"
            "version: 1\n"
            "---\n\n# Old\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, capture_output=True)

        exporter, _, cache = _make_exporter()
        live = _make_page(id="live", title="_archived", body="<p>live</p>")
        cs = _make_cached_space(pages=[live])
        cs.include_archived = False
        cache.refresh.return_value = cs

        result = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(
            tmp_path,
            result.written_files,
            "TEST",
            is_full=True,
            protection=_prot(subtrees=result.preserved_paths),
        )

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert "_archived/Old/Old.md" in ls
        assert "_archived/stale.txt" not in ls

    def test_archived_parent_preservation_does_not_keep_child_moved_live(
        self, tmp_path
    ):
        import subprocess

        from confluence_export.git import commit_export, ensure_repo

        ensure_repo(tmp_path)
        parent_dir = _seed_export_page(tmp_path, "_archived/Parent", "parent")
        child_dir = _seed_export_page(tmp_path, "_archived/Parent/Child", "child")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "seed archived tree"],
            cwd=tmp_path,
            capture_output=True,
        )

        exporter, _, cache = _make_exporter()
        archived_parent = _make_page(id="parent", title="Parent")
        archived_parent.status = "archived"
        live_child = _make_page(id="child", title="Child", body="<p>now live</p>")
        cache.refresh.return_value = _make_cached_space(
            pages=[archived_parent, live_child]
        )

        result = exporter.export_space(_make_space(), tmp_path, force_refresh=True)
        commit_export(
            tmp_path,
            result.written_files,
            "TEST",
            is_full=True,
            protection=_prot(
                page_exact=result.preserved_page_paths,
                subtrees=result.preserved_paths + result.skipped_paths,
            ),
        )

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert "Child/Child.md" in ls
        assert "_archived/Parent/Parent.md" in ls
        assert "_archived/Parent/Child/Child.md" not in ls
        assert parent_dir.exists()
        assert not child_dir.exists()

    def test_include_archived_does_not_preserve(self, tmp_path):
        # When archived pages ARE written, there is nothing to preserve.
        exporter, _, cache = _make_exporter()
        archived = _make_page(id="z", title="Zarch", body="<p>old</p>")
        archived.status = "archived"
        cache.refresh.return_value = _make_cached_space(pages=[_make_page(), archived])
        result = exporter.export_space(
            _make_space(), tmp_path, force_refresh=True, include_archived=True
        )
        assert result.preserved_page_paths == []
        assert result.preserved_paths == []

    def test_include_archived_frontmatter_uses_planned_archive_path(self, tmp_path):
        exporter, _, cache = _make_exporter()
        live = _make_page(id="p1", title="Live")
        archived = _make_page(
            id="z", title="Old Child", parent_id="p1", parent_type="page",
            body="<p>old</p>",
        )
        archived.status = "archived"
        cache.refresh.return_value = _make_cached_space(pages=[live, archived])

        exporter.export_space(_make_space(), tmp_path, force_refresh=True, include_archived=True)

        md = (tmp_path / "_archived" / "Old-Child" / "Old-Child.md").read_text()
        frontmatter = yaml.safe_load(md.split("---", 2)[1])
        assert frontmatter["path"] == "/_archived/Old-Child"

    def test_reconcile_failure_does_not_abort_export(self, tmp_path):
        # A reconcile exception must never abort the export; the write walk still
        # produces correct content at planned paths.
        exporter, _, cache = _make_exporter()
        cs = _make_cached_space()
        cache.refresh.return_value = cs

        with patch("confluence_export.reconcile.reconcile", side_effect=RuntimeError("boom")):
            exporter.export_space(_make_space(), tmp_path, force_refresh=True)

        assert (tmp_path / "Test-Page" / "Test-Page.md").exists()


class TestFolderWorkspace:
    def test_folder_gets_no_workspace_but_pages_do(self, tmp_path):
        exporter, _, cache = _make_exporter()
        folder = _make_page(id="f", title="Section")
        folder.status = "folder"
        child = _make_page(id="c", title="Doc", parent_id="f", parent_type="page")
        cs = _make_cached_space(pages=[folder, child])
        cache.ensure_loaded.return_value = cs

        exporter.export_space(_make_space(), tmp_path)

        assert (tmp_path / "Section").is_dir()
        assert not (tmp_path / "Section" / ".workspace").exists()
        assert (tmp_path / "Section" / "Doc" / ".workspace").is_dir()


class TestExporterDefensiveBranches:
    def test_prefetch_body_failure_warns_and_continues(self, tmp_path, capsys):
        # A page with no cached body triggers a prefetch; if the API call fails the
        # export warns and continues rather than aborting.
        exporter, client, cache = _make_exporter()
        cache.ensure_loaded.return_value = _make_cached_space(
            pages=[_make_page(id="p", title="P", body="")]
        )
        client.get_page_by_id.side_effect = RuntimeError("api down")

        exporter.export_space(_make_space(), tmp_path)

        assert "could not fetch body for P" in capsys.readouterr().err

    def test_reconcile_uses_full_plan_when_include_archived(self, tmp_path):
        # A full, force-refresh export with include_archived=True exercises the
        # reconcile_plan = self._plan branch (no archived-id filtering).
        exporter, _, cache = _make_exporter()
        cache.refresh.return_value = _make_cached_space(pages=[_make_page(id="p", title="P")])

        exporter.export_space(
            _make_space(), tmp_path, force_refresh=True, include_archived=True
        )

        assert (tmp_path / "P" / "P.md").exists()

    def test_single_page_export_with_path_filter_and_no_children(self, tmp_path):
        # path_filter + no_children exports just the matched node (nodes_to_export
        # = [node]) and none of its children.
        exporter, _, cache = _make_exporter()
        cache.ensure_loaded.return_value = _make_cached_space(pages=[
            _make_page(id="r", title="Root"),
            _make_page(id="c", title="Child", parent_id="r", parent_type="page"),
        ])

        result = exporter.export_space(
            _make_space(), tmp_path, path_filter="/Root", no_children=True
        )

        assert result.count == 1  # only Root, children excluded
