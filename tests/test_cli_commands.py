"""CLI tests: verify actual behavior, exit codes, and output content."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import requests

from confluence_export.cli import _resolve_space, main
from confluence_export.config import (
    ApiDialect,
    AuthConfig,
    AuthMode,
    ConnectionProfile,
    ConnectionProfileError,
    resolve_cloud_id,
)
from confluence_export.exporter import ExportResult
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


def _profile(
    site_url="https://x.atlassian.net",
    api_base_url=None,
    auth_mode=AuthMode.BEARER_PAT,
    api_dialect=ApiDialect.CLOUD_V2,
    cloud_id=None,
    token="tok",
    email="",
    cookie_header="",
):
    auth = AuthConfig(
        type=auth_mode,
        email=email,
        token=token,
        cookie_header=cookie_header,
    )
    return ConnectionProfile(
        site_url=site_url,
        api_base_url=api_base_url or site_url,
        cloud_id=cloud_id,
        auth_mode=auth_mode,
        api_dialect=api_dialect,
        config_source="test config",
        interactive=False,
        auth=auth,
    )


def _mock_client(spaces=None):
    mock = MagicMock()
    space_list = spaces if spaces is not None else [_space()]
    mock.get_spaces.return_value = space_list

    def _by_key(key: str):
        for s in space_list:
            if s.key.upper() == key.upper():
                return s
        return None

    mock.get_space_by_key.side_effect = _by_key
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
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        out = capsys.readouterr().out
        assert "TEST" in out
        assert "Test Space" in out
        assert "KEY" in out  # header row

    def test_no_spaces_shows_message(self, capsys):
        client = _mock_client(spaces=[])
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        assert "No spaces found" in capsys.readouterr().out


class TestTreeCommand:
    def test_shows_page_hierarchy(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.ensure_loaded.return_value = _cached_space()
        with patch("sys.argv", ["confluence-export", "tree", "TEST"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
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
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
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
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
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
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        stdout = capsys.readouterr().out
        assert "Exported 2 page(s)" in stdout

        # Verify actual files were written
        md_files = list(Path(out).rglob("*.md"))
        assert len(md_files) == 2

    def test_prints_warning_summary_to_stderr_when_degraded(self, tmp_path, capsys):
        client = _mock_client()
        export_result = ExportResult(
            count=3,
            written_files=[tmp_path / "out" / "P" / "P.md"],
            warnings={"attachment unavailable (HTTP 404)": 2, "draw.io produced no output": 1},
        )
        exporter = MagicMock()
        exporter.export_space.return_value = export_result
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.Exporter", return_value=exporter):
            main()

        captured = capsys.readouterr()
        assert "Exported 3 page(s)" in captured.out
        # The grouped one-liner goes to stderr so it does not pollute piped stdout.
        assert "3 warning(s):" in captured.err
        assert "attachment unavailable (HTTP 404) ×2" in captured.err

    def test_no_warning_summary_when_clean(self, tmp_path, capsys):
        client = _mock_client()
        export_result = ExportResult(count=1, written_files=[], warnings={})
        exporter = MagicMock()
        exporter.export_space.return_value = export_result
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.Exporter", return_value=exporter):
            main()

        assert "warning(s):" not in capsys.readouterr().err


    def test_export_migrates_legacy_media_dirs(self, tmp_path, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        # Create a legacy media/ dir with .versions.json before export
        out_path = Path(out)
        out_path.mkdir(parents=True)
        legacy = out_path / "Root" / "media"
        legacy.mkdir(parents=True)
        (legacy / ".versions.json").write_text("{}")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media", "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        captured = capsys.readouterr()
        assert "Migrated 1 media/" in captured.err
        assert (out_path / "Root" / ".media" / ".versions.json").exists()

    def test_export_creates_git_commit(self, tmp_path, capsys):
        import subprocess

        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
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
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
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
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        assert not (Path(out) / ".git").exists()

    def test_git_runs_for_full_export_with_only_protected_paths(self, tmp_path):
        """A moved page can fail before writing any file, but still needs the
        post-export git path to restore tracked deletions under skipped paths."""
        client = _mock_client()
        export_result = ExportResult(
            count=0,
            written_files=[],
            skipped_paths=[tmp_path / "out" / "Old" / "Page"],
            preserved_page_paths=[tmp_path / "out" / "_archived" / "Known"],
            preserved_paths=[tmp_path / "out" / "_archived"],
            prune_media_dirs=[tmp_path / "out" / "Page" / ".media"],
        )
        exporter = MagicMock()
        exporter.export_space.return_value = export_result
        out = str(tmp_path / "out")

        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.Exporter", return_value=exporter), \
             patch("confluence_export.git.git_available", return_value=True), \
             patch("confluence_export.git.ensure_repo", return_value=True), \
             patch("confluence_export.git.commit_local_changes"), \
             patch("confluence_export.git.commit_export") as commit_export:
            main()

        commit_export.assert_called_once()
        # Scope is welded into the single typed ProtectionSet the exporter produces;
        # there is no untyped protected_dirs / protected_subtree_dirs slot to
        # mis-route. The whole bundle matches what result.protection() builds.
        protection = commit_export.call_args.kwargs["protection"]
        assert protection == export_result.protection(Path(out).resolve())
        # And scope routed correctly: archived pages page-EXACT, skipped pages into
        # the RECURSIVE subtree group alongside the blind _archived subtree.
        assert [p.path for p in protection.page_exact] == export_result.preserved_page_paths
        assert [s.path for s in protection.subtrees] == (
            export_result.preserved_paths + export_result.skipped_paths
        )

    def test_git_skips_full_export_with_only_preserved_subtrees(self, tmp_path):
        """An empty current-only export must not prune all live files merely
        because an existing archived subtree is being preserved."""
        client = _mock_client()
        export_result = ExportResult(
            count=0,
            written_files=[],
            preserved_paths=[tmp_path / "out" / "_archived"],
        )
        exporter = MagicMock()
        exporter.export_space.return_value = export_result
        out = str(tmp_path / "out")

        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.Exporter", return_value=exporter), \
             patch("confluence_export.git.git_available", return_value=True), \
             patch("confluence_export.git.ensure_repo", return_value=True), \
             patch("confluence_export.git.commit_local_changes"), \
             patch("confluence_export.git.commit_export") as commit_export:
            main()

        commit_export.assert_not_called()

    def test_git_skips_authoritative_archived_only_to_preserve_live_pages(self, tmp_path):
        """DATA-SAFETY DECISION 1 (fix ①): even when the cache AUTHORITATIVELY sees
        only archived pages (a v2 run that returned the archived set but ZERO
        current pages), a zero-live-write run must NOT prune. The empty live set is
        ambiguous (genuinely emptied OR a transient/pagination artifact), and
        pruning would git-rm the committed live pages. Archived-preservation sets
        are pure protection inputs — they never independently open the prune gate."""
        client = _mock_client()
        export_result = ExportResult(
            count=0,
            written_files=[],
            preserved_page_paths=[tmp_path / "out" / "_archived" / "Known"],
        )
        exporter = MagicMock()
        exporter.export_space.return_value = export_result
        out = str(tmp_path / "out")

        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.Exporter", return_value=exporter), \
             patch("confluence_export.git.git_available", return_value=True), \
             patch("confluence_export.git.ensure_repo", return_value=True), \
             patch("confluence_export.git.commit_local_changes"), \
             patch("confluence_export.git.commit_export") as commit_export:
            main()

        commit_export.assert_not_called()

    def test_git_skips_emptied_full_export_to_preserve_committed_pages(self, tmp_path):
        """DATA-SAFETY DECISION: a full export that returns ZERO pages with nothing
        to protect must NOT prune the previously-committed export. A zero-page
        result (genuinely emptied space OR a transient/auth failure) keeps the
        prior export rather than risk deleting it on a bad response."""
        client = _mock_client()
        export_result = ExportResult(count=0, written_files=[])  # nothing at all
        exporter = MagicMock()
        exporter.export_space.return_value = export_result
        out = str(tmp_path / "out")

        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.Exporter", return_value=exporter), \
             patch("confluence_export.git.git_available", return_value=True), \
             patch("confluence_export.git.ensure_repo", return_value=True), \
             patch("confluence_export.git.commit_local_changes"), \
             patch("confluence_export.git.commit_export") as commit_export:
            main()

        commit_export.assert_not_called()

    def test_no_author_lookup_flag_propagates_to_exporter(self, tmp_path):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        argv = [
            "confluence-export", "export", "TEST",
            "-o", out, "--no-media", "--no-git", "--no-author-lookup",
        ]
        with patch("sys.argv", argv), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache), \
             patch("confluence_export.cli.Exporter") as exporter_cls:
            exporter_cls.return_value.export_space.return_value = MagicMock(
                count=0, written_files=[]
            )
            main()

        assert exporter_cls.call_args.kwargs["skip_author_lookup"] is True

    def test_preflight_failure_before_writing_output(self, tmp_path, capsys):
        client = _mock_client()
        client.verify_auth.side_effect = Exception("auth failed")
        out = tmp_path / "out"
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", str(out), "--no-media", "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore") as cache_cls:
            with pytest.raises(SystemExit):
                main()

        assert not out.exists()
        cache_cls.assert_not_called()
        assert "Preflight failed" in capsys.readouterr().err


class TestRefreshCommand:
    def test_refreshes_and_reports(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        with patch("sys.argv", ["confluence-export", "refresh", "TEST"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()
        out = capsys.readouterr().out
        assert "Cache refreshed" in out
        assert "2 pages" in out


class TestConfigureCommand:
    def test_saves_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        inputs = iter(["https://x.atlassian.net", "a@b.com", "my-token"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            main()
        data = json.loads(config_file.read_text())
        assert data["version"] == 2
        assert data["site_url"] == "https://x.atlassian.net"
        assert data["auth"]["email"] == "a@b.com"
        assert data["auth"]["token"] == "my-token"

    def test_missing_site_url_exits(self, capsys, tmp_path):
        inputs = iter(["", "", "tok"])
        config_file = tmp_path / "missing.json"
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1
        assert "site_url" in capsys.readouterr().err

    def test_gateway_site_url_exits(self, capsys, tmp_path):
        inputs = iter(["https://api.atlassian.com/ex/confluence/cloud-123", "", "tok"])
        config_file = tmp_path / "config.json"
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            with pytest.raises(SystemExit):
                main()

        assert not config_file.exists()
        assert "OAuth gateway" in capsys.readouterr().err

    def test_http_site_url_exits(self, capsys, tmp_path):
        inputs = iter(["http://x.atlassian.net", "a@b.com", "tok"])
        config_file = tmp_path / "config.json"
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            with pytest.raises(SystemExit):
                main()

        assert not config_file.exists()
        assert "HTTPS" in capsys.readouterr().err

    def test_bearer_mode_when_no_email(self, capsys, tmp_path):
        config_file = tmp_path / "config.json"
        inputs = iter(["https://x.atlassian.net", "", "my-pat"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            main()
        assert "Bearer token" in capsys.readouterr().out
        data = json.loads(config_file.read_text())
        assert data["auth"]["type"] == "bearer_pat"

    def test_padded_pat_without_email_is_not_cookie(self, tmp_path):
        config_file = tmp_path / "config.json"
        inputs = iter(["https://x.atlassian.net", "", "abc123=="])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            main()

        data = json.loads(config_file.read_text())
        assert data["auth"]["type"] == "bearer_pat"
        assert data["auth"]["token"] == "abc123=="

    def test_scoped_token_without_email_exits_before_cookie_classification(self, capsys, tmp_path):
        config_file = tmp_path / "config.json"
        inputs = iter(["https://x.atlassian.net", "", "ATATT3xDummy=ADA456abc"])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            with pytest.raises(SystemExit):
                main()

        assert not config_file.exists()
        assert "scoped API tokens require an email" in capsys.readouterr().err

    def test_configure_local_writes_local_config(self, tmp_path):
        inputs = iter(["https://x.atlassian.net", "", "session=abc"])
        with patch("sys.argv", ["confluence-export", "configure", "--local", str(tmp_path)]), \
             patch("builtins.input", side_effect=inputs):
            main()

        data = json.loads((tmp_path / ".conex" / "config.json").read_text())
        assert data["auth"]["type"] == "cookie"


# -- Cookie and auth flags ---------------------------------------------------


class TestCookieFlag:
    def test_cookie_profile_is_first_class(self, capsys):
        client = _mock_client()
        profile = _profile(
            auth_mode=AuthMode.COOKIE,
            api_dialect=ApiDialect.COOKIE_V1,
            token="",
            cookie_header="session=abc; tok=xyz",
        )
        with patch("sys.argv", ["confluence-export", "--cookie", "session=abc; tok=xyz", "spaces"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=profile) as mock_load, \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        assert mock_load.call_args.kwargs["cookie"] == "session=abc; tok=xyz"
        assert "TEST" in capsys.readouterr().out

    def test_bad_cookie_export_preflight_exits(self, tmp_path, capsys):
        client = _mock_client()
        client.verify_auth.side_effect = Exception("401")
        with patch("sys.argv", ["confluence-export", "--cookie", "bad=val", "export", "TEST", "-o", str(tmp_path / "out")]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile(
                 auth_mode=AuthMode.COOKIE,
                 api_dialect=ApiDialect.COOKIE_V1,
                 token="",
                 cookie_header="bad=val",
             )), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            with pytest.raises(SystemExit):
                main()
        assert "Preflight failed" in capsys.readouterr().err

    def test_cookie_does_not_route_scoped_token_via_gateway(self):
        client = _mock_client()
        with patch("sys.argv", ["confluence-export", "--cookie", "session=abc", "spaces"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile(
                 auth_mode=AuthMode.COOKIE,
                 api_dialect=ApiDialect.COOKIE_V1,
                 token="",
                 cookie_header="session=abc",
             )), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client):
            main()
        client.get_spaces.assert_called_once()


class TestNeedsToken:
    def test_missing_credentials_fails_without_prompt(self, capsys):
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_connection_profile", side_effect=ConnectionProfileError("authentication credentials are required")):
            with pytest.raises(SystemExit):
                main()
        assert "authentication credentials" in capsys.readouterr().err


class TestResolveCloudId:
    """Cloud-ID lookup against the unauthenticated /_edge/tenant_info endpoint."""

    def _mock_response(self, *, status=200, body=None, raise_json=False):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if status >= 400:
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
                f"HTTP {status}"
            )
        if raise_json:
            resp.json.side_effect = ValueError("bad json")
        else:
            resp.json.return_value = body or {}
        return resp

    def test_happy_path_returns_cloud_id(self):
        resp = self._mock_response(body={"cloudId": "abc-123"})
        with patch("confluence_export.config.requests.get", return_value=resp) as mock_get:
            assert resolve_cloud_id("https://acme.atlassian.net/") == "abc-123"
        # Trailing slash stripped, /_edge/tenant_info appended.
        called_url = mock_get.call_args[0][0]
        assert called_url == "https://acme.atlassian.net/_edge/tenant_info"

    def test_network_error_returns_none(self):
        with patch(
            "confluence_export.config.requests.get",
            side_effect=requests.exceptions.ConnectionError("dns"),
        ):
            assert resolve_cloud_id("https://acme.atlassian.net") is None

    def test_http_error_returns_none(self):
        resp = self._mock_response(status=503)
        with patch("confluence_export.config.requests.get", return_value=resp):
            assert resolve_cloud_id("https://acme.atlassian.net") is None

    def test_bad_json_returns_none(self):
        resp = self._mock_response(raise_json=True)
        with patch("confluence_export.config.requests.get", return_value=resp):
            assert resolve_cloud_id("https://acme.atlassian.net") is None

    def test_missing_cloud_id_field_returns_none(self):
        resp = self._mock_response(body={"someOther": "value"})
        with patch("confluence_export.config.requests.get", return_value=resp):
            assert resolve_cloud_id("https://acme.atlassian.net") is None

    def test_non_string_cloud_id_returns_none(self):
        # Defensive: API contract says string, but guard against future drift.
        resp = self._mock_response(body={"cloudId": 12345})
        with patch("confluence_export.config.requests.get", return_value=resp):
            assert resolve_cloud_id("https://acme.atlassian.net") is None

    def test_empty_cloud_id_returns_none(self):
        resp = self._mock_response(body={"cloudId": ""})
        with patch("confluence_export.config.requests.get", return_value=resp):
            assert resolve_cloud_id("https://acme.atlassian.net") is None


class TestConfigError:
    def test_missing_config_shows_setup_hint(self, capsys):
        with patch("sys.argv", ["confluence-export", "spaces"]), \
             patch("confluence_export.cli.load_connection_profile", side_effect=ConnectionProfileError("site_url is required")):
            with pytest.raises(SystemExit):
                main()
        err = capsys.readouterr().err
        assert "site_url" in err
        assert "configure" in err  # suggests running configure


# -- diff command ------------------------------------------------------------


class TestDiffCommand:
    def _diff_cache(self):
        cache = MagicMock()
        cs = _cached_space()
        cache.load.return_value = cs
        cache.refresh.return_value = cs
        return cache

    def test_diff_reports_new_pages_against_empty_export(self, tmp_path, capsys):
        """End-to-end: scan an (empty) export dir, refresh from cache, and print a
        real diff. Both cached pages must show up as New since the export is empty."""
        client = _mock_client()
        cache = self._diff_cache()
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        with patch("sys.argv", ["confluence-export", "diff", "TEST", str(export_dir)]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        captured = capsys.readouterr()
        # Refresh was forced (diffing against stale cache is useless).
        cache.refresh.assert_called_once()
        assert "Scanned 0 page(s)" in captured.err
        assert "New (2)" in captured.out
        assert "/Root" in captured.out
        assert "/Root/Child" in captured.out

    def test_diff_nonexistent_dir_exits(self, tmp_path, capsys):
        client = _mock_client()
        missing = tmp_path / "nope"
        with patch("sys.argv", ["confluence-export", "diff", "TEST", str(missing)]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=MagicMock()):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1
        assert "is not a directory" in capsys.readouterr().err

    def test_diff_unknown_path_filter_exits(self, tmp_path, capsys):
        client = _mock_client()
        cache = self._diff_cache()
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        with patch("sys.argv", ["confluence-export", "diff", "TEST", str(export_dir), "--path", "/Does/Not/Exist"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1
        assert "not found in space" in capsys.readouterr().err

    def test_diff_path_filter_scopes_to_subtree(self, tmp_path, capsys):
        """A valid --path filter restricts the diff to that subtree only."""
        client = _mock_client()
        cache = self._diff_cache()
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        with patch("sys.argv", ["confluence-export", "diff", "TEST", str(export_dir), "--path", "/Root/Child"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        out = capsys.readouterr().out
        # Only the Child subtree is in scope; Root alone is not a separate "New".
        assert "New (1)" in out
        assert "/Root/Child" in out

    def test_diff_resolves_space_when_cache_empty(self, tmp_path, capsys):
        """When the cache has no prior snapshot, the space is resolved via the
        client before refreshing."""
        client = _mock_client()
        cache = MagicMock()
        cache.load.return_value = None  # cache miss → resolve via client
        cache.refresh.return_value = _cached_space()
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        with patch("sys.argv", ["confluence-export", "diff", "TEST", str(export_dir)]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        # Space was resolved through the client since the cache was empty.
        client.get_space_by_key.assert_called_with("TEST")
        assert "New (2)" in capsys.readouterr().out


# -- preflight helpers -------------------------------------------------------


class TestPreflightHelpers:
    def test_check_output_writable_rejects_file_at_output_path(self, tmp_path):
        from confluence_export.cli import _check_output_writable

        existing_file = tmp_path / "afile"
        existing_file.write_text("x")
        with pytest.raises(RuntimeError, match="exists and is not a directory"):
            _check_output_writable(str(existing_file))

    def test_check_output_writable_rejects_unwritable_parent(self, tmp_path):
        from confluence_export.cli import _check_output_writable

        target = tmp_path / "sub" / "out"
        with patch("confluence_export.cli.os.access", return_value=False):
            with pytest.raises(RuntimeError, match="is not writable"):
                _check_output_writable(str(target))

    def test_preflight_error_message_for_authentication_error(self):
        from confluence_export.cli import _preflight_error_message
        from confluence_export.client import AuthenticationError

        exc = AuthenticationError(403, "https://x.atlassian.net/api")
        assert _preflight_error_message(exc) == "HTTP 403 from https://x.atlassian.net/api"

    def test_preflight_error_message_for_http_error(self):
        from confluence_export.cli import _preflight_error_message

        resp = MagicMock()
        resp.status_code = 500
        exc = requests.exceptions.HTTPError("boom")
        exc.response = resp
        assert _preflight_error_message(exc) == "HTTP 500"

    def test_ensure_gateway_route_raises_when_no_cloud_id(self):
        from confluence_export.cli import _ensure_gateway_route

        profile = _profile(cloud_id=None)
        with pytest.raises(RuntimeError, match="cloud ID or gateway URL is missing"):
            _ensure_gateway_route(profile)

    def test_require_space_raises_when_not_found(self):
        from confluence_export.cli import _require_space

        client = _mock_client(spaces=[_space()])
        with pytest.raises(RuntimeError, match="space 'NOPE' not found"):
            _require_space(client, "NOPE")


class TestPreflightBranches:
    """Drive the full preflight via the export command to cover its branches."""

    def test_preflight_reports_cloud_id_and_gateway_route(self, tmp_path, capsys):
        """A scoped-token / gateway profile prints the Cloud ID line and runs the
        extra 'gateway route resolved' step (and the gateway api-mode label)."""
        from confluence_export.config import gateway_url

        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        profile = _profile(
            auth_mode=AuthMode.SCOPED_API_TOKEN,
            api_dialect=ApiDialect.GATEWAY_V2,
            cloud_id="cloud-123",
            api_base_url=gateway_url("cloud-123"),
            email="a@b.com",
        )
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media", "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=profile), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        err = capsys.readouterr().err
        assert "Cloud ID: cloud-123" in err
        assert "scoped API token" in err  # _auth_label SCOPED_API_TOKEN
        assert "OAuth gateway" in err  # _api_mode_label GATEWAY_V2
        assert "gateway route resolved" in err

    def test_preflight_skips_page_and_attachment_when_space_unresolved(self, tmp_path, capsys):
        """If the space cannot be resolved, the page-listing step is marked failed
        and the attachment step is skipped — and preflight aborts."""
        client = _mock_client(spaces=[_space(key="OTHER")])
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "MISSING", "-o", out, "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore") as cache_cls:
            with pytest.raises(SystemExit):
                main()

        err = capsys.readouterr().err
        assert "✗ page listing available" in err
        assert "Preflight failed" in err
        cache_cls.assert_not_called()

    def test_preflight_skips_attachment_listing_when_no_pages(self, tmp_path, capsys):
        """Space resolves but probe returns no sample page → attachment listing is
        skipped (not failed)."""
        client = _mock_client()
        client.probe_page_listing.return_value = None  # no sample page id
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-git"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            main()

        err = capsys.readouterr().err
        assert "attachment listing skipped (no pages)" in err

    def test_preflight_warns_when_git_unavailable(self, tmp_path, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.return_value = _cached_space()
        out = str(tmp_path / "out")
        with patch("sys.argv", ["confluence-export", "export", "TEST", "-o", out, "--no-media"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache), \
             patch("confluence_export.git.git_available", return_value=False):
            main()

        err = capsys.readouterr().err
        # Preflight prints the warning, and the export body re-checks git_available.
        assert "git unavailable; export will continue without git" in err
        assert "Warning: git not found" in err


# -- _find_space fallback ----------------------------------------------------


class TestFindSpaceFallback:
    def test_falls_back_to_full_list_on_case_mismatch(self):
        """When the server-side key lookup misses (case difference), the full-list
        fallback resolves the space."""
        from confluence_export.cli import _find_space

        client = MagicMock()
        client.get_space_by_key.return_value = None  # server-side filter misses
        client.get_spaces.return_value = [_space(key="TEST")]
        result = _find_space(client, "test", announce=False)
        assert result is not None
        assert result.key == "TEST"


# -- configure: existing-config defaults -------------------------------------


class TestConfigureExistingDefaults:
    def test_looks_like_cookie_header_rejects_empty_name_or_value(self):
        from confluence_export.cli import _looks_like_cookie_header

        assert _looks_like_cookie_header("=value") is False
        assert _looks_like_cookie_header("name=") is False

    def test_reuses_existing_v2_config_when_inputs_blank(self, tmp_path):
        """Pressing enter at each prompt keeps the values from an existing v2
        config file (covers the v2 default-extraction path)."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "version": 2,
            "site_url": "https://prior.atlassian.net",
            "auth": {"email": "prior@b.com", "token": "prior-token"},
        }))
        inputs = iter(["", "", ""])  # all blank → reuse existing
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            main()

        data = json.loads(config_file.read_text())
        assert data["site_url"] == "https://prior.atlassian.net"
        assert data["auth"]["email"] == "prior@b.com"
        assert data["auth"]["token"] == "prior-token"

    def test_legacy_gateway_base_url_is_dropped(self, tmp_path, capsys):
        """A legacy (v1) config whose base_url is an OAuth gateway URL must NOT be
        offered as the site-url default; the user is prompted for the real one."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://api.atlassian.com/ex/confluence/cloud-123",
            "email": "old@b.com",
            "api_token": "old-token",
        }))
        inputs = iter(["https://real.atlassian.net", "", ""])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file):
            main()

        assert "OAuth gateway URL" in capsys.readouterr().err
        data = json.loads(config_file.read_text())
        assert data["site_url"] == "https://real.atlassian.net"
        # email/token still reused from the legacy config defaults.
        assert data["auth"]["email"] == "old@b.com"

    def test_scoped_token_with_email_resolves_cloud_id(self, tmp_path, capsys):
        """A scoped token + email triggers cloud-id resolution and gateway routing
        during configure."""
        config_file = tmp_path / "config.json"
        inputs = iter([
            "https://acme.atlassian.net",
            "a@b.com",
            "ATATT3xDummy=ADA456abc",  # is_scoped_token → True
        ])
        with patch("sys.argv", ["confluence-export", "configure"]), \
             patch("builtins.input", side_effect=inputs), \
             patch("confluence_export.cli.config_path", return_value=config_file), \
             patch("confluence_export.cli.resolve_cloud_id", return_value="cloud-999") as resolve:
            main()

        resolve.assert_called_once_with("https://acme.atlassian.net")
        out = capsys.readouterr().out
        assert "scoped API token" in out  # _auth_label SCOPED_API_TOKEN
        data = json.loads(config_file.read_text())
        assert data["auth"]["type"] == "scoped_api_token"
        assert data["cloud_id"] == "cloud-999"
        assert data["api_base_url"] == "https://api.atlassian.com/ex/confluence/cloud-999"


class TestPageOnlyWiring:
    """#39: tree/find/diff refresh page-only (skip the per-page attachment listing)."""

    def _patches(self, argv, cache, client):
        return [
            patch("sys.argv", argv),
            patch("confluence_export.cli.load_connection_profile", return_value=_profile()),
            patch("confluence_export.cli.ConfluenceClient", return_value=client),
            patch("confluence_export.cli.CacheStore", return_value=cache),
        ]

    def _run(self, argv, cache, client):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches(argv, cache, client):
                stack.enter_context(p)
            main()

    def test_tree_uses_page_only(self):
        cache = MagicMock()
        cache.ensure_loaded.return_value = _cached_space()
        self._run(["confluence-export", "tree", "TEST"], cache, _mock_client())
        assert cache.ensure_loaded.call_args.kwargs.get("need_attachments") is False

    def test_find_uses_page_only(self):
        cache = MagicMock()
        cache.ensure_loaded.return_value = _cached_space()
        self._run(["confluence-export", "find", "TEST", "Child"], cache, _mock_client())
        assert cache.ensure_loaded.call_args.kwargs.get("need_attachments") is False

    def test_diff_uses_page_only(self, tmp_path):
        cache = MagicMock()
        cache.load.return_value = None
        cache.refresh.return_value = _cached_space()
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        self._run(
            ["confluence-export", "diff", "TEST", str(export_dir)], cache, _mock_client()
        )
        assert cache.refresh.call_args.kwargs.get("fetch_attachments") is False


class TestNetworkErrorHandling:
    """#39 follow-up: a network failure that exhausts retries exits cleanly, not as
    an uncaught traceback (a read timeout during refresh aborted a bulk export)."""

    def test_persistent_read_timeout_exits_cleanly(self, capsys):
        client = _mock_client()
        cache = MagicMock()
        cache.refresh.side_effect = requests.exceptions.ReadTimeout("read timed out")
        with patch("sys.argv", ["confluence-export", "refresh", "TEST"]), \
             patch("confluence_export.cli.load_connection_profile", return_value=_profile()), \
             patch("confluence_export.cli.ConfluenceClient", return_value=client), \
             patch("confluence_export.cli.CacheStore", return_value=cache):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "network request to Confluence failed" in err
        assert "re-run to retry" in err.lower()
