"""Export orchestrator: walk tree, convert, download, write."""

from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from confluence_export.cache import CacheStore
from confluence_export.client import ConfluenceClient
from confluence_export.converter import (
    convert_page,
    sanitize_base,
    sanitize_filename,
    truncate_with_suffix,
)
from confluence_export.diff import scan_export_dir
from confluence_export.drawio import (
    find_drawio_attachments,
    render_drawio_to_png,
    replace_drawio_placeholders,
)
from confluence_export.git import relocate_subtree
from confluence_export.media import download_attachments, ensure_media_dir
from confluence_export.tree import (
    build_tree,
    find_node_by_path,
    page_path,
)
from confluence_export.types import CachedSpace, Page, PageNode, Space


MANIFEST_FILENAME = "path_manifest.json"


@dataclass
class ExportResult:
    """Result of an export operation."""

    count: int = 0
    written_files: list[Path] = field(default_factory=list)
    relocated: int = 0
    disambiguated: int = 0


@dataclass
class _ManifestEntry:
    """One row in path_manifest.json."""

    path: str  # on-disk path relative to output_dir, forward-slashes
    title: str
    parent_id: str
    is_folder: bool


@dataclass
class _ExportRun:
    """Mutable state shared across a single export_space invocation."""

    output_dir: Path
    use_git: bool
    manifest: dict[str, _ManifestEntry] = field(default_factory=dict)
    desired_paths: dict[str, str] = field(default_factory=dict)  # page_id → on-disk path
    relocated: int = 0
    disambiguated: int = 0


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
        use_git: bool = False,
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

        run = _ExportRun(
            output_dir=output_dir,
            use_git=use_git,
            manifest=self._load_manifest(output_dir, space.key),
        )

        if path_filter:
            node = find_node_by_path(roots, path_filter)
            if not node:
                print(f"Error: path '{path_filter}' not found in space {space.key}", file=sys.stderr)
                return ExportResult()
            if no_children:
                result = ExportResult()
                files = self._export_single_page(node.page, output_dir, cs, space.key)
                result.count += 1 if files else 0
                result.written_files.extend(files)
                self._finalize(run, result, space.key)
                return result
            result = self._export_siblings([node], output_dir, cs, space.key, run, depth=0)
            self._finalize(run, result, space.key)
            return result

        if no_children:
            result = ExportResult()
            for node in roots:
                files = self._export_single_page(node.page, output_dir, cs, space.key)
                result.count += 1 if files else 0
                result.written_files.extend(files)
            self._finalize(run, result, space.key)
            return result

        result = self._export_siblings(roots, output_dir, cs, space.key, run, depth=0)
        self._finalize(run, result, space.key)
        return result

    # ------------------------------------------------------------------ siblings

    def _export_siblings(
        self,
        siblings: list[PageNode],
        parent_dir: Path,
        cs: CachedSpace,
        space_key: str,
        run: _ExportRun,
        depth: int,
    ) -> ExportResult:
        """Export a list of siblings under parent_dir in three phases.

        A. Allocate all names per parent (collision-safe).
        B. Pre-write relocate: any sibling whose manifest path differs from
           its desired path is moved before any new content is written.
        C. Write + recurse.
        """
        # Phase A — allocate stable names
        taken: set[str] = set()
        allocations: list[tuple[PageNode, str]] = []
        preferred_first = sorted(
            siblings,
            key=lambda n: (
                # Pages with a manifest preference go first so they reclaim
                # their previous name before new pages bid on it.
                0 if n.page.id in run.manifest else 1,
                n.page.position,
                n.page.id,
            ),
        )
        for node in preferred_first:
            preferred = None
            entry = run.manifest.get(node.page.id)
            if entry is not None:
                # Use the leaf of the previously-recorded on-disk path as the
                # preferred name. The full path may have changed (move) but
                # we still want to keep the same leaf if possible.
                preferred = Path(entry.path).name
            name, suffixed = self._allocate_name(node.page.title, taken, preferred=preferred)
            if suffixed:
                run.disambiguated += 1
            taken.add(name.casefold())
            allocations.append((node, name))

        # Restore the natural tree order for actual processing.
        order = {id(n): i for i, n in enumerate(siblings)}
        allocations.sort(key=lambda pair: order[id(pair[0])])

        # Phase B — relocate any sibling whose recorded path differs.
        result = ExportResult()
        for node, name in allocations:
            page_id = node.page.id
            new_dir = parent_dir / name
            new_rel = new_dir.relative_to(run.output_dir).as_posix()
            run.desired_paths[page_id] = new_rel

            entry = run.manifest.get(page_id)
            if entry is None or entry.path == new_rel:
                continue
            old_dir = run.output_dir / entry.path
            if not old_dir.exists():
                # Already gone (manual delete or partial prior run): nothing
                # to relocate. The write phase will create new_dir fresh.
                continue
            if new_dir.exists():
                # Park the conflicting sibling through a tmp slot. The other
                # page will park itself the same way before its own move,
                # so the cycle resolves in two passes.
                park = run.output_dir / f".__conex_tmp_{page_id}"
                if not park.exists():
                    relocate_subtree(
                        old_dir, park, output_dir=run.output_dir, use_git=run.use_git
                    )
                # Update manifest in-memory so the second pass finds it.
                self._rewrite_manifest_prefix(
                    run.manifest, entry.path, park.relative_to(run.output_dir).as_posix()
                )
                continue

            moved = relocate_subtree(
                old_dir, new_dir, output_dir=run.output_dir, use_git=run.use_git
            )
            if moved:
                run.relocated += 1
                # Update in-memory manifest so descendants don't trigger
                # spurious follow-up relocations.
                self._rewrite_manifest_prefix(run.manifest, entry.path, new_rel)

        # Second pass: drain any parked entries into their final slots.
        for node, name in allocations:
            page_id = node.page.id
            park = run.output_dir / f".__conex_tmp_{page_id}"
            if not park.exists():
                continue
            new_dir = parent_dir / name
            if new_dir.exists():
                print(
                    f"  Warning: cannot drain parked {park} → {new_dir}: "
                    f"destination exists",
                    file=sys.stderr,
                )
                continue
            new_rel = new_dir.relative_to(run.output_dir).as_posix()
            old_rel = park.relative_to(run.output_dir).as_posix()
            moved = relocate_subtree(
                park, new_dir, output_dir=run.output_dir, use_git=run.use_git
            )
            if moved:
                run.relocated += 1
                self._rewrite_manifest_prefix(run.manifest, old_rel, new_rel)

        # Phase C — write each sibling and recurse into its children.
        for node, name in allocations:
            page = node.page
            page_dir = parent_dir / name
            page_dir.mkdir(parents=True, exist_ok=True)

            # User-workspace dir; dot-prefix avoids collision with any page
            # titled "workspace" (sanitize strips dots).
            (page_dir / ".workspace").mkdir(exist_ok=True)

            indent = "  " * depth
            print(f"{indent}Exporting: {page.title}")

            files = self._export_single_page(page, page_dir, cs, space_key, name=name)
            if files:
                result.count += 1
                result.written_files.extend(files)

            new_rel = page_dir.relative_to(run.output_dir).as_posix()
            run.manifest[page.id] = _ManifestEntry(
                path=new_rel,
                title=page.title,
                parent_id=page.parent_id,
                is_folder=page.status == "folder",
            )

            if node.children:
                child_result = self._export_siblings(
                    node.children, page_dir, cs, space_key, run, depth + 1
                )
                result.count += child_result.count
                result.written_files.extend(child_result.written_files)

        return result

    # ------------------------------------------------------------------ allocator

    def _allocate_name(
        self, title: str, taken: set[str], *, preferred: str | None = None
    ) -> tuple[str, bool]:
        """Return a unique safe name for `title` under a parent dir.

        `taken` is a casefold()'d set of names already claimed by earlier
        siblings in this batch. `preferred` is an optional previous name
        (from the manifest) that we try to reclaim verbatim before
        falling back to numeric disambiguation.

        Returns (name, was_disambiguated).
        """
        base = sanitize_base(title)
        candidate = truncate_with_suffix(base)

        if preferred and preferred.casefold() not in taken:
            if preferred == candidate or _matches_base_with_suffix(preferred, candidate):
                return preferred, preferred != candidate

        if candidate.casefold() not in taken:
            return candidate, False

        n = 2
        while True:
            suffix = f"-{n}"
            name = truncate_with_suffix(base, suffix=suffix)
            if name.casefold() not in taken:
                return name, True
            n += 1

    # ------------------------------------------------------------------ manifest

    def _manifest_path(self, output_dir: Path, space_key: str) -> Path:
        return output_dir / f".{space_key.lower()}.{MANIFEST_FILENAME}"

    def _load_manifest(
        self, output_dir: Path, space_key: str
    ) -> dict[str, _ManifestEntry]:
        """Load manifest from disk, or reconstruct from frontmatter on first run."""
        mpath = self._manifest_path(output_dir, space_key)
        if mpath.exists():
            try:
                raw = json.loads(mpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict) and "pages" in raw:
                entries: dict[str, _ManifestEntry] = {}
                for pid, row in raw["pages"].items():
                    if not isinstance(row, dict):
                        continue
                    entries[str(pid)] = _ManifestEntry(
                        path=str(row.get("path", "")),
                        title=str(row.get("title", "")),
                        parent_id=str(row.get("parent_id", "")),
                        is_folder=bool(row.get("is_folder", False)),
                    )
                return entries

        return self._reconstruct_manifest_from_disk(output_dir, space_key)

    def _reconstruct_manifest_from_disk(
        self, output_dir: Path, space_key: str
    ) -> dict[str, _ManifestEntry]:
        """Rebuild the manifest by scanning .md frontmatter under output_dir.

        Uses the on-disk parent directory of each .md file (not the YAML
        `path` field) so that previously-disambiguated names like
        "Page-2" survive into the manifest.
        """
        if not output_dir.is_dir():
            return {}
        exported = scan_export_dir(output_dir, space_key)
        entries: dict[str, _ManifestEntry] = {}
        for page_id, exp in exported.items():
            try:
                rel = exp.file_path.parent.relative_to(output_dir).as_posix()
            except ValueError:
                continue
            entries[page_id] = _ManifestEntry(
                path=rel,
                title=exp.title,
                parent_id="",
                is_folder=False,
            )
        return entries

    def _write_manifest(
        self, output_dir: Path, space_key: str, manifest: dict[str, _ManifestEntry]
    ) -> Path:
        """Serialize the manifest deterministically and return its path."""
        payload = {
            "version": 1,
            "space_key": space_key,
            "pages": {
                pid: {
                    "path": entry.path,
                    "title": entry.title,
                    "parent_id": entry.parent_id,
                    "is_folder": entry.is_folder,
                }
                for pid, entry in sorted(manifest.items(), key=lambda kv: kv[0])
            },
        }
        mpath = self._manifest_path(output_dir, space_key)
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        mpath.write_text(text, encoding="utf-8")
        return mpath

    def _rewrite_manifest_prefix(
        self,
        manifest: dict[str, _ManifestEntry],
        old_prefix: str,
        new_prefix: str,
    ) -> None:
        """Rewrite manifest entries whose path starts with old_prefix.

        Used after a subtree relocation so descendants don't trigger spurious
        follow-up relocations during the same export run.
        """
        if old_prefix == new_prefix:
            return
        for pid, entry in manifest.items():
            if entry.path == old_prefix:
                entry.path = new_prefix
            elif entry.path.startswith(old_prefix + "/"):
                entry.path = new_prefix + entry.path[len(old_prefix):]

    def _finalize(self, run: _ExportRun, result: ExportResult, space_key: str) -> None:
        """Write manifest, transfer run-level counters onto the result."""
        result.relocated = run.relocated
        result.disambiguated = run.disambiguated
        if not result.written_files and not run.manifest:
            return
        mpath = self._write_manifest(run.output_dir, space_key, run.manifest)
        result.written_files.append(mpath)

    # ------------------------------------------------------------------ page

    def _export_single_page(
        self,
        page: Page,
        page_dir: Path,
        cs: CachedSpace,
        space_key: str,
        *,
        name: str | None = None,
    ) -> list[Path]:
        """Export a single page. Returns list of files written (empty if skipped)."""
        if page.status == "folder":
            return []

        if not page.body_storage:
            try:
                full_page = self.client.get_page_by_id(page.id)
                page.body_storage = full_page.body_storage
                if full_page.version.number:
                    page.version = full_page.version
                if full_page.webui:
                    page.webui = full_page.webui
            except Exception as exc:
                print(f"  Warning: could not fetch body for {page.title}: {exc}", file=sys.stderr)
                return []

        attachments = cs.attachments.get(page.id, [])
        path = page_path(cs.pages, page.id)

        written: list[Path] = []
        if self.download_media and attachments:
            media_dir = ensure_media_dir(page_dir)
            written.extend(download_attachments(self.client, attachments, media_dir))

        markdown = convert_page(
            page,
            base_url=self.base_url,
            space_key=space_key,
            path=path,
            attachments=attachments,
            user_resolver=self._resolve_user,
        )

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

        base_filename = name if name is not None else sanitize_filename(page.title)
        md_path = page_dir / (base_filename + ".md")
        md_path.write_text(markdown, encoding="utf-8")
        written.append(md_path)

        if self.debug:
            html_path = page_dir / (base_filename + ".html")
            html_path.write_text(page.body_storage, encoding="utf-8")
            written.append(html_path)

        return written


def _matches_base_with_suffix(candidate: str, base_name: str) -> bool:
    """Return True if `candidate` looks like `base_name` plus an optional -N suffix."""
    if candidate == base_name:
        return True
    return re.fullmatch(r".+-\d+", candidate) is not None
