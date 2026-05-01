"""Export orchestrator: walk tree, convert, download, write."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from confluence_export.cache import CacheStore
from confluence_export.client import ConfluenceClient
from confluence_export.converter import convert_page, sanitize_filename
from confluence_export.drawio import (
    find_drawio_attachments,
    render_drawio_to_png,
    replace_drawio_placeholders,
)
from confluence_export.media import download_attachments, ensure_media_dir
from confluence_export.tree import (
    build_tree,
    find_node_by_path,
    format_tree,
    page_path,
)
from confluence_export.types import Attachment, CachedSpace, Page, PageNode, Space


@dataclass
class ExportResult:
    """Result of an export operation."""

    count: int = 0
    written_files: list[Path] = field(default_factory=list)


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
            cs = self.cache.refresh(self.client, space)
        else:
            cs = self.cache.ensure_loaded(self.client, space)

        self._prefetch_bodies(cs)

        roots = build_tree(cs.pages)

        if not include_archived:
            roots = [r for r in roots if r.page.id != "__archived__"]

        # Resolve subtree if path given
        if path_filter:
            node = find_node_by_path(roots, path_filter)
            if not node:
                print(f"Error: path '{path_filter}' not found in space {space.key}", file=sys.stderr)
                return ExportResult()
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
                    result.count += r.count
                    result.written_files.extend(r.written_files)
                return result

        # Export flat list (no_children case)
        result = ExportResult()
        for node in nodes_to_export:
            files = self._export_single_page(node.page, output_dir, cs, space.key)
            result.count += 1 if files else 0
            result.written_files.extend(files)
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

        files = self._export_single_page(page, page_dir, cs, space_key)
        result = ExportResult(count=1 if files else 0, written_files=list(files))

        for child in node.children:
            child_result = self._export_node(child, page_dir, cs, space_key, depth + 1)
            result.count += child_result.count
            result.written_files.extend(child_result.written_files)

        return result

    def _export_single_page(
        self,
        page: Page,
        page_dir: Path,
        cs: CachedSpace,
        space_key: str,
    ) -> list[Path]:
        """Export a single page. Returns list of files written (empty if skipped)."""
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
                print(f"  Warning: could not fetch body for {page.title}: {exc}", file=sys.stderr)
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

        # Convert to markdown
        markdown = convert_page(
            page,
            base_url=self.base_url,
            space_key=space_key,
            path=path,
            attachments=attachments,
            user_resolver=self._resolve_user,
        )

        # Handle draw.io diagrams
        if self.render_drawio:
            drawio_atts = find_drawio_attachments(attachments)
            if drawio_atts:
                rendered = {}
                media_dir = ensure_media_dir(page_dir)
                for att in drawio_atts:
                    drawio_file = media_dir / att.title
                    if drawio_file.exists():
                        png_path = render_drawio_to_png(drawio_file)
                        if png_path:
                            rendered[att.title] = png_path
                            written.append(png_path)
                if rendered:
                    markdown = replace_drawio_placeholders(markdown, rendered)

        # Write markdown file
        base_filename = sanitize_filename(page.title)
        md_path = page_dir / (base_filename + ".md")
        md_path.write_text(markdown, encoding="utf-8")
        written.append(md_path)

        # Debug: save raw HTML alongside markdown
        if self.debug:
            html_path = page_dir / (base_filename + ".html")
            html_path.write_text(page.body_storage, encoding="utf-8")
            written.append(html_path)

        return written
