"""Local JSON cache per space, ported from Go reader's cache.go."""

from __future__ import annotations

import json
import sys
import threading
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

    def refresh(self, client: ConfluenceClient, space: Space) -> CachedSpace:
        """Fetch all pages + attachments from the API and cache them."""
        print(f"Fetching pages for space {space.key}...", file=sys.stderr)
        pages = client.get_pages_in_space(space.id)
        print(f"Found {len(pages)} pages.", file=sys.stderr)

        # Resolve folders: pages may reference parent IDs that are folders,
        # not pages. Fetch these as synthetic Page entries so the tree is complete.
        pages = self._resolve_folders(client, pages)

        attachments: dict[str, list] = {}
        real_pages = [p for p in pages if p.status != "folder"]
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

        cs = CachedSpace(
            space=space,
            pages=pages,
            attachments=attachments,
            updated_at=datetime.now(timezone.utc).isoformat(),
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
                folder_page = Page(
                    id=str(data.get("id", "")),
                    title=data.get("title", ""),
                    space_id=str(data.get("spaceId", "")),
                    parent_id=str(data.get("parentId", "") or ""),
                    parent_type=data.get("parentType", ""),
                    position=data.get("position", 0),
                    status="folder",
                )
                pages.append(folder_page)
                page_ids.add(folder_page.id)
                if folder_page.parent_id and folder_page.parent_id not in page_ids:
                    new_missing.add(folder_page.parent_id)
            missing = new_missing

        return pages

    def ensure_loaded(self, client: ConfluenceClient, space: Space) -> CachedSpace:
        """Load from cache, or refresh if not cached."""
        cs = self.load(space.key)
        if cs is not None:
            return cs
        return self.refresh(client, space)
