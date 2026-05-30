"""Exporter tests: verify actual files written, not just mock call counts."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

import yaml

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

    def test_real_page_titled_archived_not_spuriously_moved(self, tmp_path):
        # A real live page titled "_archived" loses the bare name to the synthetic
        # __archived__ container (gets "_archived-2"). The reconcile plan must
        # agree with the write-walk plan on its target, or it churns its workspace.
        exporter, _, cache = _make_exporter()
        _seed_export_page(tmp_path, "_archived-2", "r", workspace="keep")
        live = _make_page(id="r", title="_archived", body="<p>real</p>")
        archived = _make_page(id="z", title="Zarch", body="<p>old</p>")
        archived.status = "archived"
        cache.refresh.return_value = _make_cached_space(pages=[live, archived])

        exporter.export_space(_make_space(), tmp_path, force_refresh=True)  # no include_archived

        assert (tmp_path / "_archived-2" / ".workspace" / "u.txt").read_text() == "keep"
        assert not (tmp_path / "_archived" / ".workspace").exists()  # not relocated into the container

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
