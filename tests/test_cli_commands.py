"""CLI tests: verify actual behavior, exit codes, and output content."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from confluence_export.cli import main, _resolve_space
from confluence_export.config import Config
from confluence_export.types import CachedSpace, Page, Space, Version


def _space(key="TEST", name="Test Space"):
    return Space(id="1", key=key, name=name, type="global", status="current")


def _cached_space():
    pages = [
        Page(id="p1", title="Root", space_id="1", parent_type="space",
             version=Version(number=1), body_storage="<p>Root content</p>"),
        Page(id="p2", title="Child", space_id="1", parent_id="p1", parent_type="page",
             version=Version(number=2), body_storage="<p>Child content</p>"),
    ]
    return CachedSpace(space=_space(), pages=pages, attachments={},
                       updated_at="2025-01-01T00:00:00Z")


def _config(base_url="https://x.atlassian.net", api_token="tok"):
    return Config(base_url=base_url, email="", api_token=api_token)


def _mock_client(spaces=None):
    mock = MagicMock()
    mock.get_spaces.return_value = spaces if spaces is not None else [_space()]
    mock._get.return_value = {"results": []}
    return mock


# -- _resolve_space ----------------------------------------------------------


class TestResolveSpace:
    def test_finds_space_case_insensitive(self):
        client = _mock_client()
        result = _resolve_space(client, "test")
        assert result.key == "TEST"

    def test_unknown_space_exits_1(self, capsys):
        client = _mock_client(spaces=[_space()])
        with pytest.raises(SystemExit) as exc:
            _resolve_space(client, "NOPE")
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err


# -- CLI dispatch ------------------------------------------------------------


class TestNoCommand:
    def test_exits_with_help(self):
        with patch("sys.argv", ["confluence-export"]):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1


class TestSpacesCommand:
    def test_lists_spaces_with_columns(self, capsys):
        client = _mock_client()
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        out = capsys.readouterr().out
        assert "TEST" in out
        assert "Test Space" in out
        assert "KEY" in out  # header row

    def test_no_spaces_shows_message(self, capsys):
        client = _mock_client(spaces=[])
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        assert "No spaces found" in capsys.readouterr().out


class TestTreeCommand:
    def test_shows_page_hierarchy(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.ensure_loaded.return_value = _cached_space()
        with patch("sys.argv", ["confluence-export", "tree", "TEST"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()
        out = capsys.readouterr().out
        assert "Root" in out
        assert "Child" in out
        assert "2 pages" in out


class TestFindCommand:
    def test_finds_matching_pages(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.ensure_loaded.return_value = _cached_space()
        with patch("sys.argv", ["confluence-export", "find", "TEST", "Child"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()
        out = capsys.readouterr().out
        assert "Child" in out
        assert "p2" in out  # page ID shown

    def test_no_match_shows_message(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.ensure_loaded.return_value = _cached_space()
        with patch("sys.argv", ["confluence-export", "find", "TEST", "zzz-nonexistent"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()
        assert "No pages matching" in capsys.readouterr().out


class TestExportCommand:
    def test_exports_pages_to_directory(self, tmp_path, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media", "--no-git"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        stdout = capsys.readouterr().out
        assert "Exported 2 page(s)" in stdout

        # Verify actual files were written
        md_files = list(Path(out).rglob("*.md"))
        assert len(md_files) == 2


    def test_export_creates_git_commit(self, tmp_path, capsys):
        import subprocess

        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        # Verify git repo was created and export was committed
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=out, capture_output=True, text=True
        )
        assert "Export Confluence space TEST" in log.stdout

        # Verify working tree is clean (no untracked files from export)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=out, capture_output=True, text=True
        )
        assert status.stdout.strip() == ""

    def test_export_with_relative_path_inside_repo(self, tmp_path, capsys, monkeypatch):
        """Relative output path (e.g. ./output) works with git versioning."""
        import subprocess

        # Initialize a parent repo (simulates user's project repo)
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, capture_output=True)

        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        # Use a relative path from tmp_path
        monkeypatch.chdir(tmp_path)
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", "output", "--no-media"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        out = tmp_path / "output"
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=out, capture_output=True, text=True
        )
        assert "Export Confluence space TEST" in log.stdout

        md_files = list(out.rglob("*.md"))
        assert len(md_files) == 2

    def test_no_git_flag_skips_versioning(self, tmp_path):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media", "--no-git"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        assert not (Path(out) / ".git").exists()


class TestRefreshCommand:
    def test_refreshes_and_reports(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        with patch("sys.argv", ["confluence-export", "refresh", "TEST"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()
        out = capsys.readouterr().out
        assert "Cache refreshed" in out
        assert "2 pages" in out


class TestConfigureCommand:
    def test_saves_config(self):
        inputs = iter(["https://x.atlassian.net", "a@b.com", "my-token"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=Path("/tmp/nope.json")), \
             patch("confluence_export.cli.save_config") as mock_save:
            main()
        cfg = mock_save.call_args[0][0]
        assert cfg.base_url == "https://x.atlassian.net"
        assert cfg.email == "a@b.com"
        assert cfg.api_token == "my-token"

    def test_missing_base_url_exits(self, capsys):
        inputs = iter(["", "", "tok"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=Path("/tmp/nope.json")):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1
        assert "base_url" in capsys.readouterr().err

    def test_bearer_mode_when_no_email(self, capsys):
        inputs = iter(["https://x.atlassian.net", "", "my-pat"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=Path("/tmp/nope.json")), \
             patch("confluence_export.cli.save_config"):
            main()
        assert "Bearer token" in capsys.readouterr().out


# -- Cookie and auth flags ---------------------------------------------------


class TestCookieFlag:
    def test_sets_cookies_and_verifies(self, capsys):
        client = _mock_client()
        with patch("sys.argv", ["confluence-export", "--cookie", "session=abc; tok=xyz", "spaces"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        client.set_cookies.assert_called_once_with("session=abc; tok=xyz")
        assert "Authenticated" in capsys.readouterr().err

    def test_bad_cookie_exits(self, capsys):
        client = _mock_client()
        client._get.side_effect = Exception("401")
        with patch("sys.argv", ["confluence-export", "--cookie", "bad=val", "spaces"]), \
             patch("confluence_export.cli.load_config", return_value=_config()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            with pytest.raises(SystemExit):
                main()
        assert "failed" in capsys.readouterr().err


class TestNeedsToken:
    def test_prompts_when_no_token_configured(self):
        client = _mock_client()
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_config", return_value=_config(api_token="")), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli._apply_browser_credentials") as mock_apply:
            main()
        mock_apply.assert_called_once()


class TestConfigError:
    def test_missing_config_shows_setup_hint(self, capsys):
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_config", side_effect=ValueError("base_url is required")):
            with pytest.raises(SystemExit):
                main()
        err = capsys.readouterr().err
        assert "base_url" in err
        assert "configure" in err  # suggests running configure
