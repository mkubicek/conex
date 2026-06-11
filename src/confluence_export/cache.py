"""Local JSON cache per space, ported from Go reader's cache.go."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from confluence_export.config import cache_dir
from confluence_export.client import ConfluenceClient
from confluence_export.types import CachedSpace, Page, Space


class CacheStore:
    """Manages per-space JSON cache files."""

    def __init__(self) -> None:
        self.dir = cache_dir()
        self.dir.mkdir(parents=True, exist_ok=True)

    def _space_file(self, space_key: str) -> Path:
        return self.dir / f"{space_key}.json"

    def save(self, cs: CachedSpace) -> None:
        data = cs.to_dict()
        with open(self._space_file(cs.space.key), "w") as f:
            json.dump(data, f, indent=2)

    def load(self, space_key: str) -> CachedSpace | None:
        p = self._space_file(space_key)
        if not p.exists():
            return None
        with open(p) as f:
            data = json.load(f)
        return CachedSpace.from_dict(data)

    def remove(self, space_key: str) -> None:
        p = self._space_file(space_key)
        if p.exists():
            p.unlink()

    def refresh(
        self,
        client: ConfluenceClient,
        space: Space,
        include_archived: bool = False,
        *,
        fetch_attachments: bool = True,
    ) -> CachedSpace:
        """Fetch pages (and, unless ``fetch_attachments`` is False, per-page
        attachment metadata) from the API and cache them.

        ``fetch_attachments=False`` is a PAGE-ONLY refresh for commands that only
        need the page tree/versions (tree, find, diff): it skips the one-request-
        per-page attachment listing, which dominates refresh cost on large spaces.
        The resulting cache has ``attachments_complete=False`` so export never
        treats it as authoritative for attachments (#39)."""
        verbose = getattr(client, "verbose", False) is True
        t0 = time.monotonic()
        print(f"Fetching pages for space {space.key}...", file=sys.stderr)
        pages = client.get_pages_in_space(space.id, include_archived=include_archived)
        print(f"Found {len(pages)} pages.", file=sys.stderr)
        t_pages = time.monotonic()

        # A 0-page response over a populated cache is ambiguous: the space may be
        # genuinely empty, or the API may be hiccupping. Warn loudly but proceed,
        # so a genuinely-emptied space stays representable. Acting on an empty
        # result is safe — the reconciler leaves every on-disk page that is absent
        # from the plan untouched, and an export with no written files prunes
        # nothing, so no data is lost either way.
        if not pages:
            # A corrupt/unreadable prior cache must not abort the refresh — refresh
            # exists precisely to overwrite it. Treat it as "no populated cache".
            try:
                prior = self.load(space.key)
            except (
                json.JSONDecodeError,
                OSError,
                TypeError,
                AttributeError,
                KeyError,
                ValueError,
            ):
                prior = None
            if prior is not None and any(p.status != "folder" for p in prior.pages):
                print(
                    f"Warning: the API returned 0 pages for space {space.key}, "
                    "but a populated cache exists. Treating the space as empty — "
                    "if this is a transient API issue rather than a genuinely "
                    "emptied space, re-run to refresh.",
                    file=sys.stderr,
                )

        # Resolve folders: pages may reference parent IDs that are folders,
        # not pages. Fetch these as synthetic Page entries so the tree is complete.
        pages = self._resolve_folders(client, pages)
        t_folders = time.monotonic()

        attachments: dict[str, list] = {}
        real_pages = [p for p in pages if p.status != "folder"]
        if fetch_attachments:
            total = len(real_pages)
            counter = [0]
            lock = threading.Lock()

            def fetch_one(page: Page) -> tuple[str, list]:
                atts = client.get_attachments(page.id)
                with lock:
                    counter[0] += 1
                    print(
                        f"\rFetching attachments ({counter[0]}/{total})...",
                        end="",
                        file=sys.stderr,
                    )
                return page.id, atts

            with ThreadPoolExecutor(max_workers=8) as pool:
                for page_id, atts in pool.map(fetch_one, real_pages):
                    if atts:
                        attachments[page_id] = atts
            print(file=sys.stderr)
        t_atts = time.monotonic()

        if verbose:
            stats = getattr(client, "stats", {}) or {}
            att_calls = len(real_pages) if fetch_attachments else 0
            msg = (
                f"Refresh timing: {len(pages)} pages in {t_pages - t0:.1f}s, "
                f"folders in {t_folders - t_pages:.1f}s, "
                f"{att_calls} attachment-list call(s) in {t_atts - t_folders:.1f}s"
            )
            if stats:
                msg += (
                    f"; {stats.get('requests', 0)} requests, "
                    f"{stats.get('retries', 0)} retr(y/ies), "
                    f"{stats.get('rate_limit_sleep_s', 0.0):.1f}s slept on rate limits"
                )
            print(msg, file=sys.stderr)

        # v2 always returns current+archived regardless of the requested flag, so
        # the cache is archive-capable even when the caller asked for current-only.
        # Derive the bit from the client's delivered shape, not just the request.
        cache_includes_archived = include_archived or client.returns_archived_pages
        cs = CachedSpace(
            space=space,
            pages=pages,
            attachments=attachments,
            updated_at=datetime.now(timezone.utc).isoformat(),
            include_archived=cache_includes_archived,
            attachments_complete=fetch_attachments,
        )
        self.save(cs)
        return cs

    @staticmethod
    def _resolve_folders(client: ConfluenceClient, pages: list[Page]) -> list[Page]:
        """Fetch folders referenced as parents but missing from the page list."""
        page_ids = {p.id for p in pages}
        missing = set()
        for p in pages:
            if p.parent_id and p.parent_id not in page_ids:
                missing.add(p.parent_id)

        if not missing:
            return pages

        print(f"Resolving {len(missing)} folder(s)...", file=sys.stderr)
        # Iteratively resolve: folders can also have folder parents
        while missing:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(client.get_folder_by_id, list(missing)))
            new_missing = set()
            for data in results:
                if not data:
                    continue
                # `or` coalescing (#47 class): the v2 dialect returns the RAW
                # folder dict (no builder), so an explicit null must coalesce
                # HERE — a None title/position crashes the layout planner/tree
                # sort outside every per-page guard, aborting the whole export.
                folder_page = Page(
                    id=str(data.get("id") or ""),
                    title=data.get("title") or "",
                    space_id=str(data.get("spaceId") or ""),
                    parent_id=str(data.get("parentId") or ""),
                    parent_type=data.get("parentType") or "",
                    position=data.get("position") or 0,
                    status="folder",
                )
                pages.append(folder_page)
                page_ids.add(folder_page.id)
                if folder_page.parent_id and folder_page.parent_id not in page_ids:
                    new_missing.add(folder_page.parent_id)
            missing = new_missing

        return pages

    def ensure_loaded(
        self,
        client: ConfluenceClient,
        space: Space,
        include_archived: bool = False,
        *,
        need_attachments: bool = True,
    ) -> CachedSpace:
        """Load from cache, or refresh if the cache cannot satisfy the request.

        ``need_attachments=False`` lets page-only commands (tree, find) accept — or
        create — a page-only cache. A cache satisfies the request only if it covers
        archived pages when asked AND has complete attachment metadata when needed;
        otherwise it is refreshed (page-only when attachments are not needed). Older
        cache files have no provenance bits: include_archived falls into the refresh
        branch, and attachments_complete defaults True (they were always full)."""
        cs = self.load(space.key)
        if (
            cs is not None
            and (cs.include_archived or not include_archived)
            and (cs.attachments_complete or not need_attachments)
        ):
            return cs
        return self.refresh(
            client,
            space,
            include_archived=include_archived,
            fetch_attachments=need_attachments,
        )
