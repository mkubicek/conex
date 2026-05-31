"""Export orchestrator: walk tree, convert, download, write."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from confluence_export.cache import CacheStore
from confluence_export.client import ConfluenceClient
from confluence_export.converter import convert_page, sanitize_filename
from confluence_export.layout import plan_layout
from confluence_export.drawio import (
    find_drawio_attachments,
    render_drawio_to_png,
)
from confluence_export.media import WORKSPACE_DIR_NAME, download_attachments, ensure_media_dir
from confluence_export.tree import (
    build_tree,
    collect_subtree,
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


def is_full_export(path_filter: str | None, no_children: bool) -> bool:
    """Whether this export writes the COMPLETE space tree (the only mode with
    full visibility). The single source of truth for the predicate that gates
    BOTH move/orphan reconciliation (exporter) and git stale-file pruning (cli):
    a partial export must do neither. Used by export_space and by cli's
    commit_export call so the two can never diverge."""
    return path_filter is None and not no_children


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
        # Per-export layout plan (page_id -> target_dir), set in export_space.
        # Empty when a page is exported via a direct _export_single_page call
        # (e.g. unit tests), in which case naming falls back to sanitize_filename.
        self._plan: dict[str, PurePosixPath] = {}

    def _planned_segment(self, page: Page) -> str:
        """The collision-free path segment for a page's dir leaf and md stem.

        Both the directory name and the markdown filename come from this single
        value, so they cannot desync. Falls back to raw sanitization when no
        plan entry exists (direct calls without a precomputed plan)."""
        target_dir = self._plan.get(page.id)
        if target_dir is not None:
            return target_dir.name
        return sanitize_filename(page.title)

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
            cs = self.cache.refresh(self.client, space, include_archived=include_archived)
        else:
            cs = self.cache.ensure_loaded(self.client, space, include_archived=include_archived)

        self._prefetch_bodies(cs)

        roots_full = build_tree(cs.pages)

        roots = roots_full
        if not include_archived:
            roots = [r for r in roots_full if r.page.id != "__archived__"]

        # Plan the collision-free on-disk layout once, up front, over the FULL
        # tree (including the synthetic __archived__ subtree). Both the directory
        # name and the markdown filename of every page are read from this plan,
        # so two pages whose titles sanitize to the same name get distinct,
        # stable paths instead of silently overwriting (issue #11). Planning over
        # the full tree also keeps an archived page's plan entry alive at its
        # _archived/ target, so it is relocated into / out of _archived/ when
        # archived or unarchived instead of being stranded at its old path.
        self._plan = plan_layout(roots_full)

        # Reconcile moved pages and heal orphans BEFORE writing — on ANY full
        # export (the one mode with complete tree visibility), whether the tree
        # was freshly fetched or loaded from cache. Reconcile is idempotent and
        # derives its targets from the SAME plan the write walk uses, so a
        # --cached export heals the on-disk layout to match the cached plan it is
        # also writing — without reconcile, a moved page's old path would be left
        # as an orphan while git pruned only its tracked files (issue #27). A
        # later --force-refresh re-heals to the fresh positions. Filtered /
        # single-subtree / no-children runs never reconcile, nor prune
        # (commit_export is_full=False).
        #
        # Window: reconcile drops a moved page's old markdown/.media BEFORE the
        # write walk regenerates them. The disposable artifacts are recomputable
        # from the API (a --cached export uses the cached bodies, which the cache
        # carries — get_pages_in_space fetches body.storage inline), so a failed
        # write self-heals on the next successful full export; only the user's
        # .workspace is irreplaceable, and it is never dropped. Same recoverable
        # window the --force-refresh path already had — #27 just widens it to
        # --cached (verified during review).
        is_full = is_full_export(path_filter, no_children)
        if is_full:
            from confluence_export.reconcile import reconcile

            # Reconcile only over what this run actually writes. When archived
            # pages are out of scope (no --include-archived) they are left
            # untouched on disk rather than relocated into _archived/. Restrict
            # the full plan by removing the archived page ids (rather than
            # re-running plan_layout over a different tree) so every retained
            # page's target is byte-identical to the write walk's — otherwise a
            # real page titled "_archived" could get a different target in the two
            # plans and be seen as a spurious move.
            if include_archived:
                reconcile_plan = self._plan
            else:
                archived_node = next(
                    (r for r in roots_full if r.page.id == "__archived__"), None
                )
                archived_ids = (
                    {n.page.id for n in collect_subtree(archived_node)}
                    if archived_node else set()
                )
                reconcile_plan = {
                    pid: t for pid, t in self._plan.items() if pid not in archived_ids
                }
            try:
                reconcile(
                    reconcile_plan, output_dir, space.key,
                    media_will_redownload=self.download_media,
                )
            except Exception as exc:
                # Reconciliation is a best-effort relayout; never let it abort the
                # export. The write walk still produces correct content at the
                # planned paths, and a transient failure heals on the next run
                # (reconcile is idempotent). Surface the error and continue.
                print(f"Warning: layout reconciliation skipped ({exc})", file=sys.stderr)
        else:
            print(
                "Note: orphan/move healing happens on a full export of the space.",
                file=sys.stderr,
            )

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
        page_dir = parent_dir / self._planned_segment(page)
        page_dir.mkdir(parents=True, exist_ok=True)

        # Create workspace directory for user's preparation files. Folders are
        # structural-only and never hold prep files, so they get no .workspace —
        # this also means a renamed folder leaves a genuinely-empty directory the
        # reconciler can prune instead of a stranded .workspace orphan.
        # Dot-prefix avoids collision with a Confluence page titled "workspace"
        # (sanitize_filename strips dots, so no page can produce ".workspace")
        if page.status != "folder":
            (page_dir / WORKSPACE_DIR_NAME).mkdir(exist_ok=True)

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
        media_dir: Path | None = None
        if self.download_media and attachments:
            media_dir = ensure_media_dir(page_dir)
            written.extend(download_attachments(self.client, attachments, media_dir))

        # Render draw.io diagrams BEFORE conversion so the converter can emit a
        # real <img> inline. (Previously the converter wrote a [drawio:NAME] text
        # sentinel that a post-pass string-replaced; markdownify escaped `_` in
        # that sentinel so the replace silently failed (#9), and a failed render
        # left the raw sentinel in the output (#8). Rendering first removes both.)
        rendered: dict[str, Path] = {}
        if self.render_drawio:
            drawio_atts = find_drawio_attachments(attachments)
            if drawio_atts:
                media_dir = media_dir or ensure_media_dir(page_dir)
                for att in drawio_atts:
                    drawio_file = media_dir / att.title
                    if drawio_file.exists():
                        png_path = render_drawio_to_png(drawio_file)
                        if png_path:
                            rendered[att.title] = png_path
                            written.append(png_path)

        # Convert to markdown (drawio macros become real images via `rendered`,
        # or a graceful "not rendered" note when a diagram has no PNG).
        markdown = convert_page(
            page,
            base_url=self.base_url,
            space_key=space_key,
            path=path,
            attachments=attachments,
            user_resolver=self._resolve_user,
            rendered=rendered,
        )

        # Write markdown file. Same allocated segment as the page directory
        # (via the shared plan), so the dir name and file stem stay in sync.
        base_filename = self._planned_segment(page)
        md_path = page_dir / (base_filename + ".md")
        md_path.write_text(markdown, encoding="utf-8")
        written.append(md_path)

        # Debug: save raw HTML alongside markdown
        if self.debug:
            html_path = page_dir / (base_filename + ".html")
            html_path.write_text(page.body_storage, encoding="utf-8")
            written.append(html_path)

        return written
