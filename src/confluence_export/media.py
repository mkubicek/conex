"""Attachment download and media directory management."""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from confluence_export.client import ConfluenceClient
from confluence_export.paths import resolve_within, safe_attachment_name
from confluence_export.types import Attachment

_VERSIONS_FILE = ".versions.json"
MEDIA_DIR_NAME = ".media"
# User preparation files attached to a page (scripts, notes). Preserved across
# re-exports and, when a page moves, deliberately left in place (never
# auto-relocated — issue #17, Option B); the user is told where the page went.
# Shared here so the exporter, reconciler, git prune, and frontmatter scan agree.
WORKSPACE_DIR_NAME = ".workspace"


def ensure_media_dir(page_dir: Path) -> Path:
    """Create and return the .media/ subdirectory for a page."""
    media_dir = page_dir / MEDIA_DIR_NAME
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


# TODO(migration): Remove after 2027-01-01 — all users will have migrated by then
def migrate_media_dirs(root_dir: Path) -> list[tuple[Path, Path]]:
    """Rename legacy media/ directories to .media/ throughout an export tree.

    Only renames directories that contain .versions.json (the manifest created
    by download_attachments), which reliably identifies attachment directories
    vs. page directories that happen to be named "media".

    Returns list of (old_path, new_path) tuples for each renamed directory.
    """
    renamed: list[tuple[Path, Path]] = []
    # Prune heavy/irrelevant trees DURING traversal (P1): never descend git
    # internals, already-migrated .media, user .workspace, or local .conex. This
    # keeps the walk O(page dirs) instead of O(entire export tree, including
    # gigabytes of attachments) on every export, and is also a correctness guard
    # (a legacy "media/" inside .git is not ours to migrate).
    skip = {".git", MEDIA_DIR_NAME, WORKSPACE_DIR_NAME, ".conex"}
    for dirpath, dirnames, _filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if "media" not in dirnames:
            continue
        candidate = Path(dirpath) / "media"
        if not (candidate / _VERSIONS_FILE).exists():
            continue
        new_path = candidate.parent / MEDIA_DIR_NAME
        if new_path.exists():
            continue
        candidate.rename(new_path)
        renamed.append((candidate, new_path))
        # Don't descend into the just-renamed (now migrated) attachment dir.
        dirnames.remove("media")
    return renamed


def _load_versions(media_dir: Path) -> dict[str, int]:
    """Load the version manifest from a media directory."""
    p = media_dir / _VERSIONS_FILE
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_versions(media_dir: Path, versions: dict[str, int]) -> None:
    """Save the version manifest to a media directory."""
    with open(media_dir / _VERSIONS_FILE, "w") as f:
        json.dump(versions, f, indent=2)


def download_attachments(
    client: ConfluenceClient,
    attachments: list[Attachment],
    media_dir: Path,
    skip_existing: bool = True,
) -> list[Path]:
    """Download attachments to media_dir. Returns list of downloaded file paths.

    Skips files whose local version matches the API version when skip_existing=True.
    """
    versions = _load_versions(media_dir) if skip_existing else {}
    downloaded: list[Path] = []
    to_download: list[tuple[Attachment, str, Path]] = []

    for att in attachments:
        # S1: an untrusted attachment title must never write outside .media/.
        # safe_attachment_name keeps benign titles verbatim (so existing links
        # and the manifest still resolve) and neutralizes only escaping ones;
        # resolve_within is the defence-in-depth assert at the write site.
        name = safe_attachment_name(att.title)
        dest = resolve_within(media_dir, name)
        if (
            skip_existing
            and dest.exists()
            and att.version.number > 0
            and versions.get(name) == att.version.number
        ):
            downloaded.append(dest)
            continue
        if not att.download_link:
            print(f"  Warning: no download link for {att.title}", file=sys.stderr)
            continue
        to_download.append((att, name, dest))

    def _download_one(item: tuple[Attachment, str, Path]) -> Path:
        att, _name, dest = item
        # Prefer the v1 REST attachment-download endpoint over the legacy
        # `_links.download` path (`/wiki/download/attachments/...`). The REST
        # endpoint works on both the site URL and the OAuth gateway URL used
        # for scoped API tokens, whereas the legacy download path 401s through
        # the gateway. Fall back to the legacy path only when the cached
        # attachment has no page_id (very old caches written before this field
        # existed).
        if att.page_id and att.id:
            download_path = (
                f"/wiki/rest/api/content/{att.page_id}"
                f"/child/attachment/{att.id}/download"
            )
        else:
            download_path = att.download_link
            if not download_path.startswith("/wiki"):
                download_path = f"/wiki{download_path}"
        client.download_attachment_to_file(download_path, str(dest))
        return dest

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_download_one, item): item for item in to_download}
        for future in as_completed(futures):
            att, name, dest = futures[future]
            try:
                downloaded.append(future.result())
                versions[name] = att.version.number
            except Exception as exc:
                print(f"  Warning: failed to download {att.title}: {exc}", file=sys.stderr)

    # Also record versions for skipped files (in case manifest was missing)
    for att in attachments:
        if att.version.number > 0:
            versions.setdefault(safe_attachment_name(att.title), att.version.number)

    _save_versions(media_dir, versions)
    downloaded.append(media_dir / _VERSIONS_FILE)

    return downloaded
