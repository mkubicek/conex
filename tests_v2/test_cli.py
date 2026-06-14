"""Tests for conex.cli.

Contracts verified:
- Command dispatch: each command invokes the right handler.
- export flow ORDER: resolve_config → preflight → lock → clear_tmp → pull →
  commit_user_changes → build → commit_export (recorded via call-sequence list).
- tmp cleared once-and-only-once per locked command.
- --cached without snapshot → clean ConexError (exit 1, clean error message).
- Lock held → clean LockHeldError message on stderr, exit 1.
- GitError → warn and continue (export still succeeds, exit 0).
- Exit codes: 0 with warnings; 1 on ConexError.
- Banner has no credentials.
- Flag plumbing: every export flag lands in the right Options field including
    --no-author-lookup → both PullOptions.author_lookup=False AND BuildOptions.author_lookup=False
    --path → BuildOptions.subtree AND PullOptions subtree is NOT set (only build uses it)
    --no-children → BuildOptions.no_children=True
- configure flows call save_global_config / save_local_config.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_mock_cfg(
    site_url: str = "https://example.atlassian.net",
    dialect_name: str = "CLOUD_V2",
    email: str = "user@example.com",
):
    """Return a mock ResolvedConfig."""
    from conex.config import Dialect, ResolvedConfig

    dialect = getattr(Dialect, dialect_name)
    auth_headers = {"Authorization": "Basic dXNlcjp0b2tlbg=="}
    return ResolvedConfig(
        site_url=site_url,
        api_base_url=site_url,
        auth_headers=auth_headers,
        dialect=dialect,
        email=email,
        verbose=False,
        source_description="~/.config/confluence-export/config.json",
    )


def _make_mock_snapshot(page_count: int = 2):
    """Return a minimal mock Snapshot."""
    from conex.store.state import Snapshot
    from conex.models import Space, Page

    space = Space(id="S1", key="TS", name="Test Space")
    pages = [
        Page(id=f"P{i}", title=f"Page {i}", space_id="S1")
        for i in range(page_count)
    ]
    return Snapshot(
        space=space,
        pages=pages,
        fetched_at="2024-01-01T00:00:00+00:00",
        include_archived=False,
    )


def _make_mock_build_result():
    """Return a minimal mock BuildResult."""
    from conex.build import BuildResult

    return BuildResult(
        written=[],
        deleted=[],
        skipped=0,
        moved=[],
        warnings=[],
    )


def _make_mock_export_state():
    """Return a minimal mock ExportState."""
    from conex.store.state import ExportState

    return ExportState(space_key="TS", space_id="S1", pages={})


# ---------------------------------------------------------------------------
# Test: preflight banner never contains credentials
# ---------------------------------------------------------------------------


def test_banner_no_credentials(capsys, tmp_path):
    """Preflight banner must not emit credential values (base64 token, raw secret)."""
    from conex.cli import _print_preflight_banner

    cfg = _make_mock_cfg()
    _print_preflight_banner(cfg, tmp_path)

    captured = capsys.readouterr()
    output = captured.err + captured.out

    # Must show non-secret info
    assert "https://example.atlassian.net" in output
    # Must NOT show the raw base64-encoded credential value
    assert "dXNlcjp0b2tlbg==" not in output
    # Must NOT show the full "Authorization: Basic <base64>" header value inline
    assert "Authorization" not in output


def test_banner_contents(capsys, tmp_path):
    """Banner must include config source, auth, API mode, site, output dir."""
    from conex.cli import _print_preflight_banner

    cfg = _make_mock_cfg()
    _print_preflight_banner(cfg, tmp_path)

    captured = capsys.readouterr()
    out = captured.err

    assert "Config source:" in out
    assert "Auth:" in out
    assert "API mode:" in out
    assert "Site:" in out
    assert "Output:" in out


# ---------------------------------------------------------------------------
# Test: _clear_tmp clears and recreates tmp/
# ---------------------------------------------------------------------------


def test_clear_tmp_creates_directory(tmp_path):
    """_clear_tmp must create .conex/tmp if it does not exist."""
    from conex.cli import _clear_tmp

    _clear_tmp(tmp_path)
    assert (tmp_path / ".conex" / "tmp").is_dir()


def test_clear_tmp_removes_existing_files(tmp_path):
    """_clear_tmp must remove files that were in .conex/tmp."""
    from conex.cli import _clear_tmp

    tmp_dir = tmp_path / ".conex" / "tmp"
    tmp_dir.mkdir(parents=True)
    (tmp_dir / "old_file.tmp").write_text("stale", encoding="utf-8")

    _clear_tmp(tmp_path)

    assert (tmp_dir).is_dir()
    assert not (tmp_dir / "old_file.tmp").exists()


def test_clear_tmp_idempotent(tmp_path):
    """_clear_tmp may be called again and must not crash."""
    from conex.cli import _clear_tmp

    _clear_tmp(tmp_path)
    _clear_tmp(tmp_path)
    assert (tmp_path / ".conex" / "tmp").is_dir()


def test_clear_tmp_preserves_blobs_and_state(tmp_path):
    """_clear_tmp must clear ONLY .conex/tmp — never the blob store or state.json.
    A regression that rmtree'd .conex/ would destroy the crash-safe export data."""
    from conex.cli import _clear_tmp

    conex = tmp_path / ".conex"
    (conex / "blobs" / "aa").mkdir(parents=True)
    (conex / "blobs" / "aa" / "deadbeef").write_bytes(b"blob")
    (conex / "state.json").write_text('{"schema_version": 1}', encoding="utf-8")
    (conex / "tmp").mkdir()
    (conex / "tmp" / "stale").write_text("x", encoding="utf-8")

    _clear_tmp(tmp_path)

    assert (conex / "blobs" / "aa" / "deadbeef").read_bytes() == b"blob"
    assert (conex / "state.json").exists()
    assert not (conex / "tmp" / "stale").exists()
    assert (conex / "tmp").is_dir()


def test_clear_tmp_refuses_symlinked_conex(tmp_path):
    """A planted `.conex -> elsewhere` symlink must not be rmtree'd through —
    that would escape the export root."""
    from conex.cli import _clear_tmp
    from conex.errors import StateError

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tmp").mkdir(parents=True)
    victim = elsewhere / "tmp" / "precious"
    victim.write_text("do not delete", encoding="utf-8")
    root = tmp_path / "export"
    root.mkdir()
    (root / ".conex").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(StateError, match="symlink"):
        _clear_tmp(root)
    assert victim.exists(), "must not delete through the symlinked .conex"


# ---------------------------------------------------------------------------
# Test: --cached without snapshot → clean error, exit 1
# ---------------------------------------------------------------------------


def test_cached_without_snapshot_exits_cleanly(tmp_path, capsys):
    """--cached with no snapshot must produce a clean error and exit 1."""
    from conex.cli import main

    with patch("conex.cli.resolve_config") as mock_cfg, \
         patch("conex.store.state.SnapshotStore.load", return_value=None):
        mock_cfg.return_value = _make_mock_cfg()

        with pytest.raises(SystemExit) as exc_info:
            main(["export", "TS", "-o", str(tmp_path), "--cached"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "cached" in captured.err.lower() or "snapshot" in captured.err.lower()


# ---------------------------------------------------------------------------
# Test: lock held → clean error, exit 1
# ---------------------------------------------------------------------------


def test_lock_held_exits_cleanly(tmp_path, capsys):
    """When the lock is held, the CLI must exit 1 with a clear message."""
    from conex.cli import main
    from conex.errors import LockHeldError

    with patch("conex.cli.resolve_config") as mock_cfg, \
         patch("conex.store.lock.ExportLock.__enter__",
               side_effect=LockHeldError(
                   f"another conex run holds {tmp_path}/.conex/lock; "
                   "wait for it to finish (the lock releases automatically when "
                   "that run exits — do not delete the lock file)"
               )):
        mock_cfg.return_value = _make_mock_cfg()

        with pytest.raises(SystemExit) as exc_info:
            main(["export", "TS", "-o", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "lock" in captured.err.lower() or "holds" in captured.err.lower()


# ---------------------------------------------------------------------------
# Test: git missing → warn and continue (exit 0)
# ---------------------------------------------------------------------------


def test_git_missing_warns_and_continues(tmp_path, capsys):
    """GitError during ensure_repo must warn but not abort the export."""
    from conex.cli import main
    from conex.errors import GitError

    snapshot = _make_mock_snapshot()
    build_result = _make_mock_build_result()
    state = _make_mock_export_state()

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api"), \
         patch("conex.pull.pull", return_value=snapshot), \
         patch("conex.build.build", return_value=(build_result, state)), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", side_effect=GitError("git not found")):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        # Should not raise — git error is caught
        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code != 0:
                raise

    captured = capsys.readouterr()
    assert "Warning" in captured.err or "git" in captured.err.lower()


# ---------------------------------------------------------------------------
# Test: exit 0 with warnings (ConexError-free run with build warnings)
# ---------------------------------------------------------------------------


def test_export_exits_0_with_build_warnings(tmp_path, capsys):
    """A successful export with build warnings must exit 0 (no exception raised)."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()

    from conex.build import BuildResult
    result_with_warnings = BuildResult(
        written=[tmp_path / "some.md"],
        deleted=[],
        skipped=0,
        moved=[],
        warnings=["some warning from build"],
    )

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api"), \
         patch("conex.pull.pull", return_value=snapshot), \
         patch("conex.build.build", return_value=(result_with_warnings, state)), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", return_value=True), \
         patch("conex.gitio.commit_user_changes", return_value=False), \
         patch("conex.gitio.commit_export", return_value=True):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        # Must NOT raise SystemExit (exit 0 means main() returns normally)
        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            assert e.code in (0, None), f"Expected exit 0 but got exit {e.code}"

    # Warnings appear on stderr
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or "Warning" in captured.err


# ---------------------------------------------------------------------------
# Test: exit 1 on ConexError
# ---------------------------------------------------------------------------


def test_conex_error_exits_1(tmp_path, capsys):
    """Any ConexError must produce exit 1 with a clean message."""
    from conex.cli import main
    from conex.errors import ConfigError

    with patch("conex.cli.resolve_config", side_effect=ConfigError("bad config")):
        with pytest.raises(SystemExit) as exc_info:
            main(["export", "TS", "-o", str(tmp_path)])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "bad config" in captured.err


# ---------------------------------------------------------------------------
# Test: export flow ORDER
# ---------------------------------------------------------------------------


def test_export_flow_order(tmp_path):
    """Export must call: resolve_config → pull → commit_user_changes → build → commit_export."""
    from conex.cli import main

    call_order: list[str] = []
    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()
    from conex.build import BuildResult
    # include a written path so commit_export is actually called
    build_result = BuildResult(
        written=[Path("/tmp/some.md")],
        deleted=[],
        skipped=0,
        moved=[],
        warnings=[],
    )

    def fake_resolve_config(*a, **kw):
        call_order.append("resolve_config")
        return _make_mock_cfg()

    def fake_pull(*a, **kw):
        call_order.append("pull")
        return snapshot

    def fake_build(*a, **kw):
        call_order.append("build")
        return build_result, state

    def fake_commit_user(*a, **kw):
        call_order.append("commit_user_changes")
        return False

    def fake_commit_export(*a, **kw):
        call_order.append("commit_export")
        return False

    def fake_ensure_repo(*a, **kw):
        call_order.append("ensure_repo")
        return True

    with patch("conex.cli.resolve_config", side_effect=fake_resolve_config), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", side_effect=fake_pull), \
         patch("conex.build.build", side_effect=fake_build), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", side_effect=fake_ensure_repo), \
         patch("conex.gitio.commit_user_changes", side_effect=fake_commit_user), \
         patch("conex.gitio.commit_export", side_effect=fake_commit_export):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    # Verify ordering (resolve_config first, then pull before build, etc.)
    assert call_order.index("resolve_config") < call_order.index("pull")
    assert call_order.index("pull") < call_order.index("build")
    # commit_user_changes must come before build
    assert call_order.index("commit_user_changes") < call_order.index("build")
    # build before commit_export
    assert call_order.index("build") < call_order.index("commit_export")


# ---------------------------------------------------------------------------
# Test: tmp cleared exactly once per locked command
# ---------------------------------------------------------------------------


def test_tmp_cleared_once(tmp_path):
    """_clear_tmp must be called exactly once per locked export."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()
    build_result = _make_mock_build_result()

    clear_calls: list[str] = []

    def fake_clear_tmp(root):
        clear_calls.append(str(root))

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp", side_effect=fake_clear_tmp), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", return_value=snapshot), \
         patch("conex.build.build", return_value=(build_result, state)), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", return_value=True), \
         patch("conex.gitio.commit_user_changes", return_value=False), \
         patch("conex.gitio.commit_export", return_value=False):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert len(clear_calls) == 1, f"Expected 1 clear_tmp call, got {len(clear_calls)}"


# ---------------------------------------------------------------------------
# Test: flag plumbing
# ---------------------------------------------------------------------------


def _run_export_with_flags(tmp_path, extra_flags: list[str]) -> tuple:
    """Run export with extra flags, return (pull_opts, build_opts) passed to pull/build."""
    from conex.cli import main
    from conex.pull import PullOptions
    from conex.build import BuildOptions

    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()
    build_result = _make_mock_build_result()

    captured_pull_opts: list[PullOptions] = []
    captured_build_opts: list[BuildOptions] = []

    def fake_pull(api, space_key, root, blobs, prev, opts):
        captured_pull_opts.append(opts)
        return snapshot

    def fake_build(root, snapshot, blobs, prev, opts, api=None):
        captured_build_opts.append(opts)
        return build_result, state

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", side_effect=fake_pull), \
         patch("conex.build.build", side_effect=fake_build), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", return_value=True), \
         patch("conex.gitio.commit_user_changes", return_value=False), \
         patch("conex.gitio.commit_export", return_value=False):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["export", "TS", "-o", str(tmp_path)] + extra_flags)
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    return (
        captured_pull_opts[0] if captured_pull_opts else None,
        captured_build_opts[0] if captured_build_opts else None,
    )


def test_flag_no_author_lookup_propagates(tmp_path):
    """--no-author-lookup must set author_lookup=False in both PullOptions and BuildOptions."""
    pull_opts, build_opts = _run_export_with_flags(tmp_path, ["--no-author-lookup"])
    assert pull_opts is not None
    assert build_opts is not None
    assert pull_opts.author_lookup is False
    assert build_opts.author_lookup is False


def test_flag_path_propagates_to_build(tmp_path):
    """--path PAGE must set BuildOptions.subtree (path must resolve to a node)."""
    _, build_opts = _run_export_with_flags(tmp_path, ["--path", "Page 0"])
    assert build_opts is not None
    assert build_opts.subtree == "Page 0"


def test_unknown_path_exits_1_not_silent(tmp_path, capsys):
    """A --path that resolves to no node must fail loudly, not export nothing."""
    with pytest.raises(SystemExit) as exc:
        _run_export_with_flags(tmp_path, ["--path", "Does/Not/Exist"])
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_flag_no_children_propagates(tmp_path):
    """--no-children must set BuildOptions.no_children=True."""
    _, build_opts = _run_export_with_flags(tmp_path, ["--no-children"])
    assert build_opts is not None
    assert build_opts.no_children is True


def test_flag_include_archived(tmp_path):
    """--include-archived must set PullOptions.include_archived=True."""
    pull_opts, _ = _run_export_with_flags(tmp_path, ["--include-archived"])
    assert pull_opts is not None
    assert pull_opts.include_archived is True


def test_flag_no_media(tmp_path):
    """--no-media must set PullOptions.fetch_media=False and BuildOptions.media=False."""
    pull_opts, build_opts = _run_export_with_flags(tmp_path, ["--no-media"])
    assert pull_opts is not None
    assert build_opts is not None
    assert pull_opts.fetch_media is False
    assert build_opts.media is False


def test_flag_no_drawio_render(tmp_path):
    """--no-drawio-render must set BuildOptions.render_drawio=False."""
    _, build_opts = _run_export_with_flags(tmp_path, ["--no-drawio-render"])
    assert build_opts is not None
    assert build_opts.render_drawio is False


def test_flag_include_html(tmp_path):
    """--include-html must set BuildOptions.include_html=True."""
    _, build_opts = _run_export_with_flags(tmp_path, ["--include-html"])
    assert build_opts is not None
    assert build_opts.include_html is True


# ---------------------------------------------------------------------------
# Test: configure flows
# ---------------------------------------------------------------------------


def test_configure_calls_config_configure(tmp_path, monkeypatch):
    """configure command must call config.configure()."""
    from conex.cli import main
    from conex.config import Dialect, ResolvedConfig

    mock_cfg = _make_mock_cfg()
    configure_calls: list = []

    def fake_configure(*a, **kw):
        configure_calls.append((a, kw))
        return mock_cfg

    with patch("conex.cli.config_configure", side_effect=fake_configure):
        try:
            main(["configure"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert len(configure_calls) == 1
    _, kw = configure_calls[0]
    assert kw.get("local") is False  # global configure: local=False


def test_configure_local_flag(tmp_path):
    """configure --local DIR must call config.configure with local=True."""
    from conex.cli import main

    mock_cfg = _make_mock_cfg()
    configure_calls: list = []

    def fake_configure(*a, **kw):
        configure_calls.append((a, kw))
        return mock_cfg

    with patch("conex.cli.config_configure", side_effect=fake_configure):
        try:
            main(["configure", "--local", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert len(configure_calls) == 1
    _, kw = configure_calls[0]
    assert kw.get("local") is True
    assert kw.get("output_dir") == str(tmp_path)


# ---------------------------------------------------------------------------
# Test: no-command shows help and exits 1
# ---------------------------------------------------------------------------


def test_no_command_exits_1(capsys):
    """Invoking conex with no subcommand must exit 1."""
    from conex.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main([])

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Test: refresh and diff also clear tmp exactly once
# ---------------------------------------------------------------------------


def test_refresh_clears_tmp_once(tmp_path):
    """refresh must clear tmp exactly once after taking the lock."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()
    clear_calls: list = []

    def fake_clear_tmp(root):
        clear_calls.append(root)

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp", side_effect=fake_clear_tmp), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.pull.pull", return_value=snapshot):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["refresh", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert len(clear_calls) == 1


def test_diff_clears_tmp_once(tmp_path):
    """diff must clear tmp exactly once after taking the lock."""
    from conex.cli import main
    from conex.store.state import ExportState

    snapshot = _make_mock_snapshot()
    prev_state = _make_mock_export_state()
    clear_calls: list = []

    def fake_clear_tmp(root):
        clear_calls.append(root)

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp", side_effect=fake_clear_tmp), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=prev_state), \
         patch("conex.pull.pull", return_value=snapshot):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["diff", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert len(clear_calls) == 1


# ---------------------------------------------------------------------------
# Test: diff with no previous state emits clean message
# ---------------------------------------------------------------------------


def test_diff_no_prev_state(tmp_path, capsys):
    """diff with no previous export state must print a helpful message."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.pull.pull", return_value=snapshot):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["diff", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    assert "export" in (captured.out + captured.err).lower()


# ---------------------------------------------------------------------------
# Test: summary line format
# ---------------------------------------------------------------------------


def test_export_summary_line(tmp_path, capsys):
    """Export summary must include written/skipped/moved/pruned counts."""
    from conex.cli import main
    from conex.build import BuildResult
    from conex.store.state import ExportState, PageState

    snapshot = _make_mock_snapshot()

    # Prev state has 1 page that won't appear in new state → pruned=1
    prev_state = ExportState(
        space_key="TS",
        space_id="S1",
        pages={"OLD_PAGE": PageState(dir="Test-Space/Old-Page", file="Test-Space/Old-Page/Old-Page.md", title="Old Page")},
    )
    new_state = ExportState(space_key="TS", space_id="S1", pages={})

    build_result = BuildResult(
        written=[tmp_path / "a.md", tmp_path / "b.md"],
        deleted=[],
        skipped=3,
        moved=[("old/dir", "new/dir")],
        warnings=[],
    )

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=prev_state), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", return_value=snapshot), \
         patch("conex.build.build", return_value=(build_result, new_state)), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", return_value=True), \
         patch("conex.gitio.commit_user_changes", return_value=False), \
         patch("conex.gitio.commit_export", return_value=False):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    out = captured.out
    # The summary must report the ACTUAL counts (written=2, skipped=3, moved=1,
    # pruned=1 from the mocked result/state above), not just the labels.
    assert "2 written" in out
    assert "3 skipped" in out
    assert "1 moved" in out
    assert "1 pruned" in out


# ---------------------------------------------------------------------------
# Test: no-git flag skips git entirely
# ---------------------------------------------------------------------------


def test_no_git_skips_all_git_calls(tmp_path):
    """--no-git must not call any gitio functions."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()
    build_result = _make_mock_build_result()

    git_calls: list[str] = []

    def fake_ensure(root):
        git_calls.append("ensure_repo")
        return True

    def fake_commit_user(root):
        git_calls.append("commit_user")
        return False

    def fake_commit_export(root, result, message):
        git_calls.append("commit_export")
        return False

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", return_value=snapshot), \
         patch("conex.build.build", return_value=(build_result, state)), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", side_effect=fake_ensure), \
         patch("conex.gitio.commit_user_changes", side_effect=fake_commit_user), \
         patch("conex.gitio.commit_export", side_effect=fake_commit_export):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["export", "TS", "-o", str(tmp_path), "--no-git"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert git_calls == [], f"Expected no git calls, got: {git_calls}"


# ---------------------------------------------------------------------------
# Test: auth_mode label helpers
# ---------------------------------------------------------------------------


def test_auth_mode_label_basic():
    from conex.cli import _auth_mode_label
    from conex.config import Dialect, ResolvedConfig

    cfg = ResolvedConfig(
        site_url="https://x.atlassian.net",
        api_base_url="https://x.atlassian.net",
        auth_headers={"Authorization": "Basic abc=="},
        dialect=Dialect.CLOUD_V2,
    )
    assert "basic" in _auth_mode_label(cfg).lower() or "api" in _auth_mode_label(cfg).lower()


def test_auth_mode_label_cookie():
    from conex.cli import _auth_mode_label
    from conex.config import Dialect, ResolvedConfig

    cfg = ResolvedConfig(
        site_url="https://x.atlassian.net",
        api_base_url="https://x.atlassian.net",
        auth_headers={"Cookie": "session=xyz"},
        dialect=Dialect.COOKIE_V1,
    )
    label = _auth_mode_label(cfg)
    assert "cookie" in label.lower()


def test_api_mode_labels():
    from conex.cli import _api_mode_label
    from conex.config import Dialect, ResolvedConfig

    for dialect, expected_fragment in [
        (Dialect.CLOUD_V2, "v2"),
        (Dialect.GATEWAY_V2, "gateway"),
        (Dialect.COOKIE_V1, "v1"),
    ]:
        cfg = ResolvedConfig(
            site_url="https://x.atlassian.net",
            api_base_url="https://x.atlassian.net",
            auth_headers={},
            dialect=dialect,
        )
        label = _api_mode_label(cfg)
        assert expected_fragment.lower() in label.lower(), (
            f"Expected {expected_fragment!r} in label for {dialect}, got {label!r}"
        )


# ---------------------------------------------------------------------------
# Test: __main__ entry point
# ---------------------------------------------------------------------------


def test_main_module_entry(capsys):
    """python -m conex with no args must exit 1."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "conex"],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(
            Path(__file__).parent.parent / "src"
        )},
    )
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Test: overrides_from_args
# ---------------------------------------------------------------------------


def test_overrides_from_args():
    """_overrides_from_args must extract credential flags correctly."""
    from conex.cli import _overrides_from_args, _build_parser

    parser = _build_parser()
    args = parser.parse_args([
        "--site-url", "https://example.atlassian.net",
        "--email", "alice@example.com",
        "--api-token", "token123",
        "--cloud-id", "cloud-abc",
        "export", "TS", "-o", "/tmp/out",
    ])

    overrides = _overrides_from_args(args)
    assert overrides["site_url"] == "https://example.atlassian.net"
    assert overrides["email"] == "alice@example.com"
    assert overrides["api_token"] == "token123"
    assert overrides["cloud_id"] == "cloud-abc"


# ---------------------------------------------------------------------------
# Test: cached flow skips pull
# ---------------------------------------------------------------------------


def test_cached_skips_pull(tmp_path):
    """--cached must not call pull(); must use the existing snapshot."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()
    build_result = _make_mock_build_result()

    pull_calls: list = []

    def fake_pull(*a, **kw):
        pull_calls.append(a)
        return snapshot

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=snapshot), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", side_effect=fake_pull), \
         patch("conex.build.build", return_value=(build_result, state)), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", return_value=True), \
         patch("conex.gitio.commit_user_changes", return_value=False), \
         patch("conex.gitio.commit_export", return_value=False):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["export", "TS", "-o", str(tmp_path), "--cached"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    assert pull_calls == [], "pull() must not be called with --cached"


def test_cached_space_mismatch_aborts(tmp_path, capsys):
    """--cached snapshot for a different space must abort before build()."""
    from conex.cli import main

    snapshot = _make_mock_snapshot()  # space.key == "TS"
    build_calls: list = []

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=snapshot), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.build.build", side_effect=lambda *a, **k: build_calls.append(a)):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        with pytest.raises(SystemExit) as exc:
            main(["export", "OTHER", "-o", str(tmp_path), "--cached"])
        assert exc.value.code == 1

    assert build_calls == [], "build() must not run on a cached space mismatch"
    err = capsys.readouterr().err
    assert "OTHER" in err and "TS" in err


# ---------------------------------------------------------------------------
# Test: find happy-path (covers BLOCKER — PurePosixPath must be importable)
# ---------------------------------------------------------------------------


def test_find_prints_match(tmp_path, capsys):
    """find must print the page id and path for a matching page, exit 0."""
    from conex.cli import main
    from conex.models import Space, Page, Folder
    from conex.store.state import Snapshot

    space = Space(id="S1", key="TS", name="Test Space")
    page = Page(id="P1", title="Alpha Page", space_id="S1")
    snapshot_pages = [page]

    mock_api = MagicMock()
    mock_api.get_space.return_value = space
    mock_api.get_pages.return_value = snapshot_pages
    mock_api.get_folders.return_value = []

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.api.make_api", return_value=mock_api):
        try:
            main(["find", "TS", "alpha"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    assert "P1" in captured.out
    # The path should contain the page title segment
    assert "Alpha" in captured.out or "alpha" in captured.out.lower()


def test_find_no_match_prints_message(tmp_path, capsys):
    """find with no matching pages must print a 'No pages' message."""
    from conex.cli import main
    from conex.models import Space, Page

    space = Space(id="S1", key="TS", name="Test Space")
    mock_api = MagicMock()
    mock_api.get_space.return_value = space
    mock_api.get_pages.return_value = [Page(id="P1", title="Unrelated", space_id="S1")]
    mock_api.get_folders.return_value = []

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.api.make_api", return_value=mock_api):
        try:
            main(["find", "TS", "zzz_no_match"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    assert "No pages" in captured.out or "no pages" in captured.out.lower()


# ---------------------------------------------------------------------------
# Test: tree happy-path
# ---------------------------------------------------------------------------


def test_tree_prints_page_titles(tmp_path, capsys):
    """tree must print page titles to stdout."""
    from conex.cli import main
    from conex.models import Space, Page

    space = Space(id="S1", key="TS", name="Test Space")
    pages = [
        Page(id="P1", title="Alpha", space_id="S1"),
        Page(id="P2", title="Beta", space_id="S1"),
    ]
    mock_api = MagicMock()
    mock_api.get_space.return_value = space
    mock_api.get_pages.return_value = pages
    mock_api.get_folders.return_value = []

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.api.make_api", return_value=mock_api):
        try:
            main(["tree", "TS"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    assert "Alpha" in captured.out
    assert "Beta" in captured.out
    # Top-level pages must be at depth 0 (no leading whitespace)
    lines = [l for l in captured.out.splitlines() if l.strip() in ("Alpha", "Beta")]
    for line in lines:
        assert not line.startswith("  "), f"Top-level page should not be indented: {line!r}"


# ---------------------------------------------------------------------------
# Test: spaces smoke test (mocked Http.get_json)
# ---------------------------------------------------------------------------


def test_spaces_lists_spaces(tmp_path, capsys):
    """spaces must call Http.get_json and print the space key/name/type."""
    from conex.cli import main

    spaces_response = {
        "results": [
            {"key": "ENG", "name": "Engineering", "type": "global"},
            {"key": "MKT", "name": "Marketing", "type": "global"},
        ],
        "_links": {},
    }

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.http.Http.get_json", return_value=spaces_response):
        try:
            main(["spaces"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    assert "ENG" in captured.out
    assert "Engineering" in captured.out
    assert "MKT" in captured.out


# ---------------------------------------------------------------------------
# Test: export flow ORDER (full chain: resolve→banner→lock→tmp→pull→commit_user→build→commit_export)
# ---------------------------------------------------------------------------


def test_export_flow_order_full_chain(tmp_path, capsys):
    """Full chain: resolve_config → banner → lock → clear_tmp → pull →
    commit_user_changes → build → commit_export (in that order)."""
    from conex.cli import main

    call_order: list[str] = []

    snapshot = _make_mock_snapshot()
    state = _make_mock_export_state()
    from conex.build import BuildResult
    build_result = BuildResult(
        written=[Path("/tmp/some.md")],
        deleted=[],
        skipped=0,
        moved=[],
        warnings=[],
    )

    def fake_resolve_config(*a, **kw):
        call_order.append("resolve_config")
        return _make_mock_cfg()

    def fake_banner(cfg, output_dir):
        call_order.append("banner")

    def fake_clear_tmp(root):
        call_order.append("clear_tmp")

    def fake_pull(*a, **kw):
        call_order.append("pull")
        return snapshot

    def fake_build(*a, **kw):
        call_order.append("build")
        return build_result, state

    def fake_commit_user(*a, **kw):
        call_order.append("commit_user_changes")
        return False

    def fake_commit_export(*a, **kw):
        call_order.append("commit_export")
        return False

    def fake_ensure_repo(*a, **kw):
        call_order.append("ensure_repo")
        return True

    real_lock_enter_called = []

    class FakeLock:
        def __init__(self, root):
            pass

        def __enter__(self):
            call_order.append("lock_enter")
            real_lock_enter_called.append(True)
            return self

        def __exit__(self, *a):
            return False

    with patch("conex.cli.resolve_config", side_effect=fake_resolve_config), \
         patch("conex.cli._print_preflight_banner", side_effect=fake_banner), \
         patch("conex.cli.ExportLock", FakeLock), \
         patch("conex.cli._clear_tmp", side_effect=fake_clear_tmp), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", side_effect=fake_pull), \
         patch("conex.build.build", side_effect=fake_build), \
         patch("conex.store.state.StateStore.save"), \
         patch("conex.gitio.ensure_repo", side_effect=fake_ensure_repo), \
         patch("conex.gitio.commit_user_changes", side_effect=fake_commit_user), \
         patch("conex.gitio.commit_export", side_effect=fake_commit_export):

        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    # Verify full ordering
    assert "resolve_config" in call_order
    assert "banner" in call_order
    assert "lock_enter" in call_order
    assert "clear_tmp" in call_order
    assert "pull" in call_order
    assert "commit_user_changes" in call_order
    assert "build" in call_order
    assert "commit_export" in call_order

    idx = call_order.index
    assert idx("resolve_config") < idx("banner")
    assert idx("banner") < idx("lock_enter")
    assert idx("lock_enter") < idx("clear_tmp")
    assert idx("clear_tmp") < idx("pull")
    assert idx("pull") < idx("commit_user_changes")
    assert idx("commit_user_changes") < idx("build")
    assert idx("build") < idx("commit_export")


# ---------------------------------------------------------------------------
# Test: diff --path scopes out-of-subtree pages (MAJOR fix)
# ---------------------------------------------------------------------------


def test_diff_path_does_not_report_out_of_scope_as_deleted(tmp_path, capsys):
    """diff --path P must NOT report pages outside the subtree as deleted."""
    from conex.cli import main
    from conex.models import Space, Page
    from conex.store.state import ExportState, PageState, Snapshot

    # Space with two top-level pages: Alpha and Beta
    space = Space(id="S1", key="TS", name="Test-Space")
    alpha = Page(id="P_ALPHA", title="Alpha", space_id="S1")
    beta = Page(id="P_BETA", title="Beta", space_id="S1")

    from conex.store.state import Snapshot as Snap
    snapshot = Snap(
        space=space,
        pages=[alpha, beta],
        fetched_at="2024-01-01T00:00:00+00:00",
        include_archived=False,
    )

    # prev_state records both pages with their plan dirs
    prev_state = ExportState(
        space_key="TS",
        space_id="S1",
        pages={
            "P_ALPHA": PageState(dir="Test-Space/Alpha", file="Test-Space/Alpha/Alpha.md", title="Alpha", version=1),
            "P_BETA": PageState(dir="Test-Space/Beta", file="Test-Space/Beta/Beta.md", title="Beta", version=1),
        },
    )

    mock_api = MagicMock()

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli.ExportLock") as mock_lock_cls, \
         patch("conex.cli._clear_tmp"), \
         patch("conex.api.make_api", return_value=mock_api), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.store.state.StateStore.load", return_value=prev_state), \
         patch("conex.pull.pull", return_value=snapshot):

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)
        mock_lock_cls.return_value = mock_lock

        try:
            main(["diff", "TS", "-o", str(tmp_path), "--path", "Alpha"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    captured = capsys.readouterr()
    output = captured.out + captured.err
    # Beta is out of scope — must NOT be reported as deleted
    assert "Beta" not in output or "Deleted" not in output, (
        "Out-of-scope page 'Beta' must not be reported as deleted in --path diff"
    )
    # More precise: 'Deleted' section must not mention Beta
    if "Deleted" in output:
        deleted_section = output[output.find("Deleted"):]
        assert "Beta" not in deleted_section, (
            "Beta should not appear in the Deleted section of a --path Alpha diff"
        )


# ---------------------------------------------------------------------------
# Test: KeyboardInterrupt releases the lock
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_releases_lock(tmp_path):
    """KeyboardInterrupt during pull must still release the ExportLock."""
    from conex.cli import main
    from conex.store.lock import ExportLock

    # We use a real ExportLock in a temp dir and verify it can be re-acquired
    # after a KeyboardInterrupt mid-pull.

    def fake_pull_raises(*a, **kw):
        raise KeyboardInterrupt()

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.cli._clear_tmp"), \
         patch("conex.store.state.SnapshotStore.load", return_value=None), \
         patch("conex.api.make_api", return_value=MagicMock()), \
         patch("conex.pull.pull", side_effect=fake_pull_raises):

        try:
            main(["export", "TS", "-o", str(tmp_path)])
        except (KeyboardInterrupt, SystemExit):
            pass

    # The real lock must be acquirable immediately after the interrupt.
    # If the lock were still held this would block (or raise LockHeldError).
    acquired = False
    with ExportLock(tmp_path):
        acquired = True
    assert acquired, "ExportLock must be released after KeyboardInterrupt"


# ---------------------------------------------------------------------------
# tree display: folders are shown and pages nest under them
# ---------------------------------------------------------------------------


def test_version_flag(capsys):
    from conex.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("conex 2.")


def test_print_tree_shows_folders_nested(capsys):
    from conex.cli import _print_tree
    from conex.layout import plan_layout
    from conex.models import Folder, Page, PageVersion, Space

    space = Space(id="S1", key="SP", name="My Space")
    folders = [Folder(id="F1", title="Docs", parent_id="", parent_type="space")]
    ver = PageVersion(number=1, created_at="2024-01-01T00:00:00Z")
    pages = [
        Page(id="p1", title="Top", space_id="S1", version=ver),
        Page(id="p2", title="Inside", space_id="S1",
             parent_id="F1", parent_type="folder", version=ver),
    ]
    plan = plan_layout(space, pages, folders)
    _print_tree(space, pages, folders, plan)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    docs = next(ln for ln in lines if ln.strip() == "Docs/")
    inside = next(ln for ln in lines if ln.strip() == "Inside")
    # Folder shown with a trailing marker at top level; its page nests deeper.
    assert not docs.startswith(" ")
    assert inside.startswith("  ")


# ---------------------------------------------------------------------------
# Bug 1: tree/find exclude archived pages (mirror plain export's default)
# ---------------------------------------------------------------------------


def test_tree_excludes_archived_pages(tmp_path, capsys):
    from conex.cli import main
    from conex.models import Page, Space

    space = Space(id="S1", key="TS", name="Test Space")
    mock_api = MagicMock()
    mock_api.get_space.return_value = space
    mock_api.get_pages.return_value = [
        Page(id="P1", title="LivePage", space_id="S1"),
        Page(id="P2", title="ArchivedPage", space_id="S1", status="archived"),
    ]
    mock_api.get_folders.return_value = []

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.api.make_api", return_value=mock_api):
        try:
            main(["tree", "TS"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    out = capsys.readouterr().out
    assert "LivePage" in out
    assert "ArchivedPage" not in out, "tree must not list archived pages"
    assert "1 pages" in out, "page count must exclude archived"


def test_find_excludes_archived_pages(tmp_path, capsys):
    from conex.cli import main
    from conex.models import Page, Space

    space = Space(id="S1", key="TS", name="Test Space")
    mock_api = MagicMock()
    mock_api.get_space.return_value = space
    mock_api.get_pages.return_value = [
        Page(id="P1", title="Report Live", space_id="S1"),
        Page(id="P2", title="Report Archived", space_id="S1", status="archived"),
    ]
    mock_api.get_folders.return_value = []

    with patch("conex.cli.resolve_config", return_value=_make_mock_cfg()), \
         patch("conex.api.make_api", return_value=mock_api):
        try:
            main(["find", "TS", "Report"])
        except SystemExit as e:
            if e.code not in (0, None):
                raise

    # find prints "<id>  <sanitized-path>"; the live page is listed, the
    # archived one (P2) must be absent.
    out = capsys.readouterr().out
    assert "P1" in out and "Report-Live" in out
    assert "P2" not in out and "Archived" not in out, "find must not match archived pages"
