"""Export orchestrator: walk tree, convert, download, write."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from confluence_export.cache import CacheStore
from confluence_export.client import ConfluenceClient
from confluence_export.converter import convert_page, inspect_macros, sanitize_filename
from confluence_export.drawio import (
    find_drawio_attachment,
    render_drawio_to_png,
)
from confluence_export.media import download_attachments, ensure_media_dir
from confluence_export.tree import (
    build_tree,
    find_node_by_path,
    format_tree,
    page_path,
)
from confluence_export.types import (
    Attachment,
    CachedSpace,
    ExportDiagnostic,
    ExportReport,
    Page,
    PageNode,
    Space,
)
from confluence_export.validation import validate_markdown


class ExportResult(ExportReport):
    """Backward-compatible alias for the export report shape."""

    def __init__(
        self,
        count: int = 0,
        written_files: list[Path] | None = None,
        pages_written: int | None = None,
        files_written: int = 0,
        diagnostics: list[ExportDiagnostic] | None = None,
    ):
        super().__init__(
            pages_written=count if pages_written is None else pages_written,
            files_written=files_written,
            diagnostics=diagnostics or [],
            written_files=written_files or [],
        )


class Exporter:
    """Orchestrates the full export pipeline."""

    def __init__(
        self,
        client: ConfluenceClient,
        cache: CacheStore,
        base_url: str,
        *,
        download_media: bool = True,
        render_drawio: bool = True,
        debug: bool = False,
        skip_author_lookup: bool = False,
    ):
        self.client = client
        self.cache = cache
        self.base_url = base_url
        self.download_media = download_media
        self.render_drawio = render_drawio
        self.debug = debug
        self.skip_author_lookup = skip_author_lookup
        self._user_cache: dict[str, dict | None] = {}

    def _resolve_user(self, account_id: str) -> dict | None:
        """Resolve user account ID to user info dict, with caching."""
        if self.skip_author_lookup:
            return None
        if account_id not in self._user_cache:
            self._user_cache[account_id] = self.client.get_user_info(account_id)
        return self._user_cache[account_id]

    def _render_drawio_artifacts(
        self,
        page: Page,
        page_dir: Path,
        attachments: list[Attachment],
        written: list[Path],
    ) -> tuple[dict[str, Path], dict[str, str], set[Path]]:
        """Render draw.io source attachments before markdown conversion."""
        rendered: dict[str, Path] = {}
        failures: dict[str, str] = {}
        generated_media: set[Path] = set()
        needs = inspect_macros(page.body_storage, attachments)
        if not needs.drawio_names or not self.render_drawio or not self.download_media:
            return rendered, failures, generated_media

        media_dir = ensure_media_dir(page_dir)
        for diagram_name in sorted(needs.drawio_names):
            source = find_drawio_attachment(attachments, diagram_name)
            if source is None:
                failures[diagram_name] = "source attachment not found"
                continue

            drawio_file = media_dir / source.title
            if not drawio_file.exists():
                failures[diagram_name] = "source attachment was not downloaded"
                failures[source.title] = "source attachment was not downloaded"
                continue

            png_path = render_drawio_to_png(drawio_file)
            if png_path:
                rendered[diagram_name] = png_path
                rendered[source.title] = png_path
                generated_media.add(png_path)
                if png_path not in written:
                    written.append(png_path)
            else:
                failures[diagram_name] = "render failed"
                failures[source.title] = "render failed"
        return rendered, failures, generated_media

    def _prefetch_bodies(self, cs: CachedSpace) -> None:
        """Pre-fetch page bodies in parallel so the tree walk doesn't block."""
        pages_to_fetch = [
            p for p in cs.pages
            if not p.body_storage and p.status != "folder"
        ]
        if not pages_to_fetch:
            return

        total = len(pages_to_fetch)
        counter = [0]
        lock = threading.Lock()
        print(f"Pre-fetching {total} page bodies...", file=sys.stderr)

        def fetch_body(page: Page) -> None:
            try:
                full_page = self.client.get_page_by_id(page.id)
                page.body_storage = full_page.body_storage
                if full_page.version.number:
                    page.version = full_page.version
                if full_page.webui:
                    page.webui = full_page.webui
            except Exception as exc:
                print(f"  Warning: could not fetch body for {page.title}: {exc}", file=sys.stderr)
            with lock:
                counter[0] += 1
                print(
                    f"\rPre-fetching bodies ({counter[0]}/{total})...",
                    end="",
                    file=sys.stderr,
                )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch_body, pages_to_fetch))
        print(file=sys.stderr)

    def export_space(
        self,
        space: Space,
        output_dir: Path,
        path_filter: str | None = None,
        no_children: bool = False,
        force_refresh: bool = False,
        include_archived: bool = False,
    ) -> ExportResult:
        """Export a space (or subtree) to output_dir."""
        # Ensure cache
        if force_refresh:
            cs = self.cache.refresh(self.client, space, include_archived=include_archived)
        else:
            cs = self.cache.ensure_loaded(self.client, space, include_archived=include_archived)

        self._prefetch_bodies(cs)

        roots = build_tree(cs.pages)

        if not include_archived:
            roots = [r for r in roots if r.page.id != "__archived__"]

        # Resolve subtree if path given
        if path_filter:
            node = find_node_by_path(roots, path_filter)
            if not node:
                print(f"Error: path '{path_filter}' not found in space {space.key}", file=sys.stderr)
                return ExportResult(
                    diagnostics=[
                        ExportDiagnostic(
                            severity="error",
                            page_id=None,
                            page_title=None,
                            code="path_not_found",
                            message=f"path '{path_filter}' not found in space {space.key}",
                        )
                    ]
                )
            if no_children:
                nodes_to_export = [node]
            else:
                return self._export_node(node, output_dir, cs, space.key, depth=0)
        else:
            if no_children:
                nodes_to_export = roots
            else:
                result = ExportResult()
                for root in roots:
                    r = self._export_node(root, output_dir, cs, space.key, depth=0)
                    result.extend(r)
                return result

        # Export flat list (no_children case)
        result = ExportResult()
        for node in nodes_to_export:
            files = self._export_single_page(
                node.page, output_dir, cs, space.key, diagnostics=result.diagnostics
            )
            result.add_files(files, page_written=bool(files))
        return result

    def _export_node(
        self,
        node: PageNode,
        parent_dir: Path,
        cs: CachedSpace,
        space_key: str,
        depth: int,
    ) -> ExportResult:
        """Recursively export a node and its children."""
        page = node.page
        dir_name = sanitize_filename(page.title)
        page_dir = parent_dir / dir_name
        page_dir.mkdir(parents=True, exist_ok=True)

        # Create workspace directory for user's preparation files
        # Dot-prefix avoids collision with a Confluence page titled "workspace"
        # (sanitize_filename strips dots, so no page can produce ".workspace")
        (page_dir / ".workspace").mkdir(exist_ok=True)

        indent = "  " * depth
        print(f"{indent}Exporting: {page.title}")

        result = ExportResult()
        files = self._export_single_page(
            page, page_dir, cs, space_key, diagnostics=result.diagnostics
        )
        result.add_files(files, page_written=bool(files))

        for child in node.children:
            child_result = self._export_node(child, page_dir, cs, space_key, depth + 1)
            result.extend(child_result)

        return result

    def _export_single_page(
        self,
        page: Page,
        page_dir: Path,
        cs: CachedSpace,
        space_key: str,
        diagnostics: list[ExportDiagnostic] | None = None,
    ) -> list[Path]:
        """Export a single page. Returns list of files written (empty if skipped)."""
        diagnostics = diagnostics if diagnostics is not None else []
        # Folders are structural only — no content to export
        if page.status == "folder":
            return []

        # Fetch full page body if not already loaded
        if not page.body_storage:
            try:
                full_page = self.client.get_page_by_id(page.id)
                page.body_storage = full_page.body_storage
                # Also update version info from full fetch
                if full_page.version.number:
                    page.version = full_page.version
                if full_page.webui:
                    page.webui = full_page.webui
            except Exception as exc:
                message = f"could not fetch body for {page.title}: {exc}"
                print(f"  Warning: {message}", file=sys.stderr)
                diagnostics.append(
                    ExportDiagnostic(
                        severity="error",
                        page_id=page.id,
                        page_title=page.title,
                        code="body_fetch_failed",
                        message=message,
                    )
                )
                return []

        # Get attachments from cache
        attachments = cs.attachments.get(page.id, [])

        # Build page path
        path = page_path(cs.pages, page.id)

        # Download media
        written: list[Path] = []
        if self.download_media and attachments:
            media_dir = ensure_media_dir(page_dir)
            written.extend(download_attachments(self.client, attachments, media_dir))

        drawio_rendered, drawio_failures, generated_media = self._render_drawio_artifacts(
            page, page_dir, attachments, written
        )

        # Convert to markdown after draw.io render results are known.
        try:
            markdown = convert_page(
                page,
                base_url=self.base_url,
                space_key=space_key,
                path=path,
                attachments=attachments,
                user_resolver=self._resolve_user,
                diagnostics=diagnostics,
                drawio_rendered=drawio_rendered,
                drawio_failures=drawio_failures,
                render_drawio=self.render_drawio,
                download_media=self.download_media,
            )
        except Exception as exc:
            diagnostics.append(
                ExportDiagnostic(
                    severity="error",
                    page_id=page.id,
                    page_title=page.title,
                    code="conversion_failed",
                    message=f"page conversion failed: {exc}",
                )
            )
            return []

        # Write markdown file
        base_filename = sanitize_filename(page.title)
        md_path = page_dir / (base_filename + ".md")
        md_path.write_text(markdown, encoding="utf-8")
        written.append(md_path)
        diagnostics.extend(
            validate_markdown(
                markdown,
                md_path,
                page,
                validate_media_refs=self.download_media,
                generated_media_paths=generated_media,
            )
        )

        # Debug: save raw HTML alongside markdown
        if self.debug:
            html_path = page_dir / (base_filename + ".html")
            html_path.write_text(page.body_storage, encoding="utf-8")
            written.append(html_path)

        return written
