"""Tests for cache store operations."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from confluence_export.cache import CacheStore
from confluence_export.types import Attachment, CachedSpace, Page, Space, Version


def _make_space():
    return Space(id="1", key="TEST", name="Test Space")


def _make_cached_space(include_archived: bool = False):
    return CachedSpace(
        space=_make_space(),
        pages=[Page(id="p1", title="Page", space_id="1", version=Version(number=1))],
        attachments={},
        updated_at="2025-01-01T00:00:00Z",
        include_archived=include_archived,
    )


class TestCacheStore:
    def test_save_and_load(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            cs = _make_cached_space()
            store.save(cs)

            loaded = store.load("TEST")
            assert loaded is not None
            assert loaded.space.key == "TEST"
            assert len(loaded.pages) == 1

    def test_load_missing(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            assert store.load("NOPE") is None

    def test_remove(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            cs = _make_cached_space()
            store.save(cs)
            assert store.load("TEST") is not None

            store.remove("TEST")
            assert store.load("TEST") is None

    def test_remove_nonexistent(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            store.remove("NOPE")  # should not raise

    def test_ensure_loaded_cached(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            cs = _make_cached_space()
            store.save(cs)

            client = MagicMock()
            result = store.ensure_loaded(client, _make_space())
            assert result.space.key == "TEST"
            client.get_pages_in_space.assert_not_called()

    def test_save_and_load_preserves_body_storage(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            cs = CachedSpace(
                space=_make_space(),
                pages=[Page(id="p1", title="Page", space_id="1",
                            body_storage="<p>cached body</p>",
                            version=Version(number=2))],
                attachments={},
                updated_at="2025-01-01T00:00:00Z",
            )
            store.save(cs)

            loaded = store.load("TEST")
            assert loaded is not None
            assert loaded.pages[0].body_storage == "<p>cached body</p>"

    def test_save_and_load_preserves_include_archived(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            cs = _make_cached_space(include_archived=True)
            store.save(cs)

            loaded = store.load("TEST")
            assert loaded is not None
            assert loaded.include_archived is True

    def test_ensure_loaded_refresh(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            client = MagicMock()
            client.get_pages_in_space.return_value = [
                Page(id="p1", title="Page", space_id="1", version=Version(number=1))
            ]
            client.get_attachments.return_value = []

            result = store.ensure_loaded(client, _make_space())
            assert result.space.key == "TEST"
            client.get_pages_in_space.assert_called_once()

    def test_ensure_loaded_uses_archived_cache_when_requested(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            store.save(_make_cached_space(include_archived=True))

            client = MagicMock()
            result = store.ensure_loaded(client, _make_space(), include_archived=True)

            assert result.include_archived is True
            client.get_pages_in_space.assert_not_called()

    def test_ensure_loaded_refreshes_current_only_cache_when_archived_requested(self, tmp_path):
        with patch("confluence_export.cache.cache_dir", return_value=tmp_path):
            store = CacheStore()
            store.save(_make_cached_space(include_archived=False))
            client = MagicMock()
            client.get_pages_in_space.return_value = [
                Page(id="p1", title="Page", space_id="1", version=Version(number=1)),
                Page(
                    id="p2",
                    title="Archived Page",
                    space_id="1",
                    status="archived",
                    version=Version(number=1),
                ),
            ]
            client.get_attachments.return_value = []

            result = store.ensure_loaded(client, _make_space(), include_archived=True)

            assert result.include_archived is True
            assert any(p.status == "archived" for p in result.pages)
            client.get_pages_in_space.assert_called_once_with(
                "1", include_archived=True
            )
