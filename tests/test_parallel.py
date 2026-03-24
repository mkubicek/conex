"""Tests for parallel fetch behaviour in cache, media, and exporter."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from confluence_export.cache import CacheStore
from confluence_export.media import download_attachments
from confluence_export.types import Attachment, Page, Space, Version


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_page(pid: str, *, parent_id: str = "", status: str = "current") -> Page:
    return Page(
        id=pid,
        title=f"Page {pid}",
        space_id="100",
        parent_id=parent_id,
        parent_type="page" if parent_id else "space",
        status=status,
    )


def _make_attachment(aid: str, page_id: str) -> Attachment:
    return Attachment(
        id=aid,
        title=f"file_{aid}.png",
        media_type="image/png",
        file_size=100,
        page_id=page_id,
        download_link=f"/wiki/download/{aid}",
    )


def _make_space() -> Space:
    return Space(id="100", key="TEST", name="Test Space")


# ---------------------------------------------------------------------------
# CacheStore.refresh – attachment collection
# ---------------------------------------------------------------------------


class TestRefreshCollectsAllAttachments:
    def test_all_pages_fetched(self, tmp_path: Path):
        pages = [_make_page(str(i)) for i in range(1, 11)]
        atts_by_page = {
            "1": [_make_attachment("a1", "1")],
            "5": [_make_attachment("a5", "5")],
            "10": [_make_attachment("a10", "10")],
        }

        client = MagicMock()
        client.get_pages_in_space.return_value = pages
        client.get_attachments.side_effect = lambda pid: atts_by_page.get(pid, [])
        client.get_folder_by_id.return_value = None

        store = CacheStore()
        store.dir = tmp_path

        cs = store.refresh(client, _make_space())

        assert client.get_attachments.call_count == 10
        assert set(cs.attachments.keys()) == {"1", "5", "10"}
        assert len(cs.attachments["1"]) == 1

    def test_folders_excluded_from_attachment_fetch(self, tmp_path: Path):
        pages = [
            _make_page("1"),
            _make_page("2", status="folder"),
            _make_page("3"),
        ]

        client = MagicMock()
        client.get_pages_in_space.return_value = pages
        client.get_attachments.return_value = []
        client.get_folder_by_id.return_value = None

        store = CacheStore()
        store.dir = tmp_path

        store.refresh(client, _make_space())

        # Only non-folder pages should have attachments fetched
        fetched_ids = [call.args[0] for call in client.get_attachments.call_args_list]
        assert "2" not in fetched_ids
        assert len(fetched_ids) == 2


# ---------------------------------------------------------------------------
# CacheStore._resolve_folders – multi-level
# ---------------------------------------------------------------------------


class TestResolveFoldersMultilevel:
    def test_two_level_chain(self):
        """Page -> folder_A -> folder_B (root). Both folders resolved."""
        pages = [_make_page("1", parent_id="f1")]

        folder_data = {
            "f1": {
                "id": "f1",
                "title": "Folder A",
                "spaceId": "100",
                "parentId": "f2",
                "parentType": "page",
                "position": 0,
            },
            "f2": {
                "id": "f2",
                "title": "Folder B",
                "spaceId": "100",
                "parentId": "",
                "parentType": "space",
                "position": 0,
            },
        }

        client = MagicMock()
        client.get_folder_by_id.side_effect = lambda fid: folder_data.get(fid)

        result = CacheStore._resolve_folders(client, pages)

        ids = {p.id for p in result}
        assert ids == {"1", "f1", "f2"}
        assert all(p.status == "folder" for p in result if p.id.startswith("f"))

    def test_no_missing_folders(self):
        """When all parents exist, no API calls made."""
        pages = [_make_page("1"), _make_page("2", parent_id="1")]

        client = MagicMock()
        result = CacheStore._resolve_folders(client, pages)

        client.get_folder_by_id.assert_not_called()
        assert len(result) == 2


# ---------------------------------------------------------------------------
# download_attachments – parallel error isolation
# ---------------------------------------------------------------------------


class TestDownloadParallelErrorIsolation:
    def test_one_failure_others_succeed(self, tmp_path: Path):
        att_ok = Attachment(
            id="ok",
            title="good.png",
            media_type="image/png",
            file_size=100,
            page_id="1",
            download_link="/wiki/download/ok",
        )
        att_fail = Attachment(
            id="fail",
            title="bad.png",
            media_type="image/png",
            file_size=100,
            page_id="1",
            download_link="/wiki/download/fail",
        )

        client = MagicMock()

        def mock_download(path: str, dest: str) -> int:
            if "fail" in path:
                raise ConnectionError("simulated failure")
            Path(dest).write_bytes(b"x" * 100)
            return 100

        client.download_attachment_to_file.side_effect = mock_download

        media_dir = tmp_path / "media"
        media_dir.mkdir()

        result = download_attachments(client, [att_ok, att_fail], media_dir, skip_existing=False)

        assert len(result) == 1
        assert result[0].name == "good.png"


# ---------------------------------------------------------------------------
# Progress counter reaches total
# ---------------------------------------------------------------------------


class TestProgressReachesTotal:
    def test_counter_reaches_n(self, tmp_path: Path, capsys):
        n = 5
        pages = [_make_page(str(i)) for i in range(1, n + 1)]

        client = MagicMock()
        client.get_pages_in_space.return_value = pages
        client.get_attachments.return_value = []
        client.get_folder_by_id.return_value = None

        store = CacheStore()
        store.dir = tmp_path

        store.refresh(client, _make_space())

        captured = capsys.readouterr()
        assert f"{n}/{n}" in captured.err
