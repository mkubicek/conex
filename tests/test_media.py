"""Tests for media download helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_export.media import download_attachments, ensure_media_dir
from confluence_export.types import Attachment


class TestEnsureMediaDir:
    def test_creates_dir(self, tmp_path):
        page_dir = tmp_path / "page"
        page_dir.mkdir()
        media = ensure_media_dir(page_dir)
        assert media.exists()
        assert media.name == "media"


class TestDownloadAttachments:
    def test_skip_existing(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        existing = media_dir / "img.png"
        existing.write_bytes(b"x" * 100)

        att = Attachment(id="a1", title="img.png", file_size=100,
                         download_link="/wiki/download/a1")
        client = MagicMock()

        result = download_attachments(client, [att], media_dir, skip_existing=True)
        assert len(result) == 1
        client.download_attachment_to_file.assert_not_called()

    def test_downloads_new(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        att = Attachment(id="a1", title="new.png", file_size=50,
                         download_link="/wiki/download/a1")
        client = MagicMock()
        client.download_attachment_to_file.return_value = 50

        result = download_attachments(client, [att], media_dir, skip_existing=True)
        assert len(result) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_no_download_link(self, tmp_path, capsys):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        att = Attachment(id="a1", title="nolink.png", file_size=50, download_link="")
        client = MagicMock()

        result = download_attachments(client, [att], media_dir)
        assert len(result) == 0
        assert "no download link" in capsys.readouterr().err

    def test_download_failure(self, tmp_path, capsys):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        att = Attachment(id="a1", title="fail.png", file_size=50,
                         download_link="/wiki/download/a1")
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [att], media_dir)
        assert len(result) == 0
        assert "failed to download" in capsys.readouterr().err

    def test_prepends_wiki_prefix(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()

        att = Attachment(id="a1", title="x.png", file_size=10,
                         download_link="/rest/api/content/a1/download")
        client = MagicMock()
        client.download_attachment_to_file.return_value = 10

        download_attachments(client, [att], media_dir)
        call_args = client.download_attachment_to_file.call_args[0]
        assert call_args[0].startswith("/wiki")
