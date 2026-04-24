"""Attachment download and media directory management."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from confluence_export.client import ConfluenceClient
from confluence_export.types import Attachment

_VERSIONS_FILE = ".versions.json"
MEDIA_DIR_NAME = ".media"


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
    for dirpath in sorted(root_dir.rglob("media"), reverse=True):
        if not dirpath.is_dir() or dirpath.name != "media":
            continue
        if not (dirpath / _VERSIONS_FILE).exists():
            continue
        new_path = dirpath.parent / MEDIA_DIR_NAME
        if new_path.exists():
            continue
        dirpath.rename(new_path)
        renamed.append((dirpath, new_path))
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
    to_download: list[tuple[Attachment, Path]] = []

    for att in attachments:
        dest = media_dir / att.title
        if (
            skip_existing
            and dest.exists()
            and att.version.number > 0
            and versions.get(att.title) == att.version.number
        ):
            downloaded.append(dest)
            continue
        if not att.download_link:
            print(f"  Warning: no download link for {att.title}", file=sys.stderr)
            continue
        to_download.append((att, dest))

    def _download_one(item: tuple[Attachment, Path]) -> Path:
        att, dest = item
        download_path = att.download_link
        if not download_path.startswith("/wiki"):
            download_path = f"/wiki{download_path}"
        client.download_attachment_to_file(download_path, str(dest))
        return dest

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_download_one, item): item for item in to_download}
        for future in as_completed(futures):
            att, dest = futures[future]
            try:
                downloaded.append(future.result())
                versions[att.title] = att.version.number
            except Exception as exc:
                print(f"  Warning: failed to download {att.title}: {exc}", file=sys.stderr)

    # Also record versions for skipped files (in case manifest was missing)
    for att in attachments:
        if att.version.number > 0:
            versions.setdefault(att.title, att.version.number)

    _save_versions(media_dir, versions)
    downloaded.append(media_dir / _VERSIONS_FILE)

    return downloaded
