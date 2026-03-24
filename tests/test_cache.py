"""Tests for cache store operations."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from confluence_export.cache import CacheStore
from confluence_export.types import Attachment, CachedSpace, Page, Space, Version


def _make_space():
    return Space(id="1", key="TEST", name="Test Space")


def _make_cached_space():
    return CachedSpace(
        space=_make_space(),
        pages=[Page(id="p1", title="Page", space_id="1", version=Version(number=1))],
        attachments={},
        updated_at="2025-01-01T00:00:00Z",
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
