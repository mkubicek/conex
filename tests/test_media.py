"""Tests for media download helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from confluence_export.media import (
    _VERSIONS_FILE,
    download_attachments,
    ensure_media_dir,
)
from confluence_export.types import Attachment, Version


def _att(title="img.png", version=1, file_size=100, download_link="/wiki/download/a1"):
    return Attachment(
        id="a1", title=title, file_size=file_size,
        download_link=download_link, version=Version(number=version),
    )


def _attachment_paths(result: list[Path]) -> list[Path]:
    """Filter out the manifest from download results."""
    return [p for p in result if p.name != _VERSIONS_FILE]


class TestEnsureMediaDir:
    def test_creates_dir(self, tmp_path):
        page_dir = tmp_path / "page"
        page_dir.mkdir()
        media = ensure_media_dir(page_dir)
        assert media.exists()
        assert media.name == "media"


class TestDownloadAttachments:
    def test_skip_when_version_matches(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"x" * 100)
        (media_dir / _VERSIONS_FILE).write_text('{"img.png": 3}')

        client = MagicMock()
        result = download_attachments(client, [_att(version=3)], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_not_called()

    def test_redownload_when_version_changes(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"x" * 100)
        (media_dir / _VERSIONS_FILE).write_text('{"img.png": 2}')

        client = MagicMock()
        result = download_attachments(client, [_att(version=3)], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_download_when_no_manifest(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"x" * 100)
        # No .versions.json — file exists but we don't know its version

        client = MagicMock()
        result = download_attachments(client, [_att(version=3)], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_saves_version_after_download(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        client = MagicMock()
        download_attachments(client, [_att(title="new.png", version=5)], media_dir)

        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["new.png"] == 5

    def test_downloads_new(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        client = MagicMock()
        result = download_attachments(client, [_att()], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_includes_manifest_in_result(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        client = MagicMock()
        result = download_attachments(client, [_att()], media_dir)

        manifest_paths = [p for p in result if p.name == _VERSIONS_FILE]
        assert len(manifest_paths) == 1

    def test_no_download_link(self, tmp_path, capsys):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        att = _att(download_link="")
        client = MagicMock()

        result = download_attachments(client, [att], media_dir)
        assert len(_attachment_paths(result)) == 0
        assert "no download link" in capsys.readouterr().err

    def test_download_failure(self, tmp_path, capsys):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [_att()], media_dir)
        assert len(_attachment_paths(result)) == 0
        assert "failed to download" in capsys.readouterr().err

    def test_prepends_wiki_prefix(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        att = _att(download_link="/rest/api/content/a1/download")
        client = MagicMock()

        download_attachments(client, [att], media_dir)
        call_args = client.download_attachment_to_file.call_args[0]
        assert call_args[0].startswith("/wiki")
