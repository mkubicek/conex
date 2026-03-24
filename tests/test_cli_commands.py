"""Tests for CLI command dispatch and integration."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from confluence_export.cli import main
from confluence_export.client import AuthenticationError
from confluence_export.config import Config
from confluence_export.types import CachedSpace, Page, Space, Version


def _make_space():
    return Space(id="1", key="TEST", name="Test Space", type="global", status="current")


def _make_cached_space():
    pages = [
        Page(id="p1", title="Root", space_id="1", parent_type="space",
             version=Version(number=1), body_storage="<p>Root</p>"),
    ]
    return CachedSpace(space=_make_space(), pages=pages, attachments={},
                       updated_at="2025-01-01T00:00:00Z")


def _patch_config(base_url="https://x.atlassian.net", api_token="tok"):
    return patch("confluence_export.cli.load_config",
                 return_value=Config(base_url=base_url, email="", api_token=api_token))


def _patch_client(spaces=None):
    mock = MagicMock()
    mock.get_spaces.return_value = spaces or [_make_space()]
    mock._get.return_value = {"results": []}
    return patch("confluence_export.cli.ConfluenceClient", return_value=mock), mock


class TestNoCommand:
    def test_prints_help(self, capsys):
        with patch("sys.argv", ["confluence-export"]):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1


class TestSpaces:
    def test_lists_spaces(self, capsys):
        client_patch, mock_client = _patch_client()
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             _patch_config(), client_patch:
            main()
        output = capsys.readouterr().out
        assert "TEST" in output
        assert "Test Space" in output

    def test_no_spaces(self, capsys):
        client_patch, mock_client = _patch_client()
        mock_client.get_spaces.return_value = []
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             _patch_config(), client_patch:
            main()
        assert "No spaces found" in capsys.readouterr().out


class TestTree:
    def test_shows_tree(self, capsys):
        client_patch, mock_client = _patch_client()
        mock_cache = MagicMock()
        mock_cache.ensure_loaded.return_value = _make_cached_space()
        with patch("sys.argv", ["confluence-export", "tree", "TEST"]), \
             _patch_config(), client_patch, \
             patch("confluence_export.cli.CacheStore", return_value=mock_cache):
            main()
        output = capsys.readouterr().out
        assert "Root" in output


class TestCookieFlag:
    def test_cookie_sets_credentials(self, capsys):
        client_patch, mock_client = _patch_client()
        with patch("sys.argv", ["confluence-export", "--cookie", "session=abc", "spaces"]), \
             _patch_config():
            with client_patch:
                main()
        mock_client.set_cookies.assert_called_once_with("session=abc")


class TestFind:
    def test_find_pages(self, capsys):
        client_patch, mock_client = _patch_client()
        mock_cache = MagicMock()
        cs = _make_cached_space()
        mock_cache.ensure_loaded.return_value = cs
        with patch("sys.argv", ["confluence-export", "find", "TEST", "Root"]), \
             _patch_config(), client_patch, \
             patch("confluence_export.cli.CacheStore", return_value=mock_cache):
            main()
        assert "Root" in capsys.readouterr().out

    def test_find_no_results(self, capsys):
        client_patch, mock_client = _patch_client()
        mock_cache = MagicMock()
        mock_cache.ensure_loaded.return_value = _make_cached_space()
        with patch("sys.argv", ["confluence-export", "find", "TEST", "nonexistent"]), \
             _patch_config(), client_patch, \
             patch("confluence_export.cli.CacheStore", return_value=mock_cache):
            main()
        assert "No pages matching" in capsys.readouterr().out


class TestExportCommand:
    def test_export(self, tmp_path, capsys):
        client_patch, mock_client = _patch_client()
        mock_cache = MagicMock()
        mock_cache.ensure_loaded.return_value = _make_cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media"]), \
             _patch_config(), client_patch, \
             patch("confluence_export.cli.CacheStore", return_value=mock_cache):
            main()
        assert "Exported" in capsys.readouterr().out


class TestRefresh:
    def test_refresh(self, capsys):
        client_patch, mock_client = _patch_client()
        mock_cache = MagicMock()
        cs = _make_cached_space()
        mock_cache.refresh.return_value = cs
        with patch("sys.argv", ["confluence-export", "refresh", "TEST"]), \
             _patch_config(), client_patch, \
             patch("confluence_export.cli.CacheStore", return_value=mock_cache):
            main()
        assert "Cache refreshed" in capsys.readouterr().out


class TestConfigure:
    def test_configure_basic(self, tmp_path):
        inputs = iter(["https://x.atlassian.net", "", "my-token"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=tmp_path / "nope.json"), \
             patch("confluence_export.cli.save_config") as mock_save:
            main()
        mock_save.assert_called_once()
        cfg = mock_save.call_args[0][0]
        assert cfg.base_url == "https://x.atlassian.net"
        assert cfg.api_token == "my-token"

    def test_configure_missing_base_url(self, tmp_path, capsys):
        inputs = iter(["", "", "tok"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=tmp_path / "nope.json"):
            with pytest.raises(SystemExit):
                main()
        assert "base_url" in capsys.readouterr().err


class TestNeedsToken:
    def test_prompts_when_no_token(self, capsys):
        client_patch, mock_client = _patch_client()
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             _patch_config(api_token=""), client_patch, \
             patch("confluence_export.cli._apply_browser_credentials") as mock_apply:
            main()
        mock_apply.assert_called_once()


class TestConfigError:
    def test_missing_config_exits(self, capsys):
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_config", side_effect=ValueError("base_url is required")):
            with pytest.raises(SystemExit):
                main()
        assert "base_url" in capsys.readouterr().err
