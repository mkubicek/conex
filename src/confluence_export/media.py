"""Attachment download and media directory management."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from confluence_export.client import ConfluenceClient
from confluence_export.types import Attachment


def ensure_media_dir(page_dir: Path) -> Path:
    """Create and return the media/ subdirectory for a page."""
    media_dir = page_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def download_attachments(
    client: ConfluenceClient,
    attachments: list[Attachment],
    media_dir: Path,
    skip_existing: bool = True,
) -> list[Path]:
    """Download attachments to media_dir. Returns list of downloaded file paths.

    Skips files that already exist with matching size when skip_existing=True.
    """
    downloaded: list[Path] = []
    to_download: list[tuple[Attachment, Path]] = []

    for att in attachments:
        dest = media_dir / att.title
        if skip_existing and dest.exists() and dest.stat().st_size == att.file_size:
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
            except Exception as exc:
                print(f"  Warning: failed to download {att.title}: {exc}", file=sys.stderr)

    return downloaded
