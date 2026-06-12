"""End-to-end test suite for conex v2 — wave 4.

Each test exercises a complete scenario end-to-end: either through
cli.main(argv) where the CLI flow matters, or through pull+build+gitio
composition where the CLI adds nothing.

Monkeypatching strategy:
- conex.cli.resolve_config  → returns a pre-built ResolvedConfig pointing
  at our FakeConfluenceAPI.
- conex.cli.make_api / conex.api.make_api → returns the fake.
- conex.build._run_drawio_render / conex.drawio → controlled per scenario.
- shutil.which → None for drawio-absent scenarios.

Real components used (no mocking):
- Filesystem: real tmp directories.
- BlobStore, StateStore, SnapshotStore, ExportLock: real implementations.
- plan_layout, build, pull, gitio (all real).
- Git: real subprocess calls against temp repos configured with explicit
  user.name and user.email (no global git config dependency).

Naming: one function per spec scenario.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent

# Ensure PYTHONPATH includes src/ when running standalone.
sys.path.insert(0, str(ROOT / "src"))

from tests_v2.fake_api import FakeConfluenceAPI

from conex.config import Dialect, ResolvedConfig
from conex.models import Space


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_cfg(site_url: str = "https://fake.atlassian.net") -> ResolvedConfig:
    """Return a ResolvedConfig that points at the fake API."""
    return ResolvedConfig(
        site_url=site_url,
        api_base_url=site_url,
        auth_headers={"Authorization": "Basic ZmFrZTpmYWtl"},
        dialect=Dialect.CLOUD_V2,
        email="test@fake.net",
        verbose=False,
        source_description="test/fake",
    )


def _init_git_repo(root: Path) -> None:
    """Init a git repo in root with a test user identity."""
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=root, check=True, capture_output=True,
    )


def _git_log(root: Path) -> list[str]:
    """Return commit messages, newest first."""
    result = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _git_log_count(root: Path) -> int:
    return len(_git_log(root))


def _run_export(
    api: FakeConfluenceAPI,
    space_key: str,
    output_dir: Path,
    extra_argv: list[str] | None = None,
) -> None:
    """Run cli.main(export ...) with the fake API injected.

    cli.py imports make_api lazily via ``from conex.api import make_api`` so we
    patch the function on the conex.api module object, not on cli's namespace.
    resolve_config is imported at module level in cli.py and can be patched
    directly on conex.cli.
    """
    cfg = _fake_cfg()
    argv = ["export", space_key, "-o", str(output_dir), "--no-author-lookup"] + (extra_argv or [])
    with (
        patch("conex.cli.resolve_config", return_value=cfg),
        patch("conex.api.make_api", return_value=api),
    ):
        from conex.cli import main
        main(argv)


def _run_refresh(
    api: FakeConfluenceAPI,
    space_key: str,
    output_dir: Path,
) -> None:
    """Run cli.main(refresh ...) with the fake API injected."""
    cfg = _fake_cfg()
    argv = ["refresh", space_key, "-o", str(output_dir)]
    with (
        patch("conex.cli.resolve_config", return_value=cfg),
        patch("conex.api.make_api", return_value=api),
    ):
        from conex.cli import main
        main(argv)


def _state(output_dir: Path):
    """Load ExportState from output_dir/.conex/state.json."""
    from conex.store.state import StateStore
    return StateStore(output_dir).load()


def _snapshot(output_dir: Path):
    """Load Snapshot from output_dir/.conex/snapshot.json."""
    from conex.store.state import SnapshotStore
    return SnapshotStore(output_dir).load()


def _md_files(output_dir: Path) -> set[str]:
    """Return all .md file paths relative to output_dir."""
    return {
        str(p.relative_to(output_dir)).replace(os.sep, "/")
        for p in output_dir.rglob("*.md")
        if ".conex" not in p.parts
    }


def _all_files(output_dir: Path) -> set[str]:
    """Return all file paths relative to output_dir excluding .conex/."""
    return {
        str(p.relative_to(output_dir)).replace(os.sep, "/")
        for p in output_dir.rglob("*")
        if p.is_file() and ".conex" not in p.parts
    }


def _make_space_api(
    pages: list[dict],
    space_key: str = "TS",
    space_id: str = "SP1",
    space_name: str = "Test Space",
) -> FakeConfluenceAPI:
    """Convenience: build a FakeConfluenceAPI from a list of page dicts."""
    api = FakeConfluenceAPI(
        space_key=space_key,
        space_id=space_id,
        space_name=space_name,
    )
    for p in pages:
        api.add_page(**p)
    return api


# ---------------------------------------------------------------------------
# Scenario 1: First export
# ---------------------------------------------------------------------------


def test_first_export(tmp_path):
    """First export: correct tree shape, frontmatter, .media, state.json, git log."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Parent Page", parent_id="", body="<p>Hello parent</p>", version=1)
    api.add_page("p2", "Child Page", parent_id="p1", parent_type="page", body="<p>Child content</p>", version=1)
    api.add_attachment(
        att_id="a1", title="image.png", page_id="p2",
        media_type="image/png", version=1,
        content=b"\x89PNG\r\n\x1a\n fake png data",
    )

    _run_export(api, "TS", root)

    # Tree shape: space dir + parent dir + child dir
    state = _state(root)
    assert state is not None
    assert len(state.pages) == 2
    assert state.space_key == "TS"

    # All .md files exist
    md = _md_files(root)
    assert any("Parent-Page" in f for f in md)
    assert any("Child-Page" in f for f in md)

    # Frontmatter on parent page
    parent_state = next(ps for ps in state.pages.values() if ps.title == "Parent Page")
    md_content = (root / parent_state.file).read_text()
    assert "page_id:" in md_content
    assert "title:" in md_content
    assert "Parent Page" in md_content

    # Media: attachment for child page
    child_state = next(ps for ps in state.pages.values() if ps.title == "Child Page")
    media_dir = root / child_state.dir / ".media"
    assert media_dir.is_dir()
    assert any(media_dir.iterdir())

    # Attachment blob recorded in state
    assert len(child_state.attachments) == 1

    # Git: one commit
    assert _git_log_count(root) == 1
    log = _git_log(root)
    assert any("conex export" in msg or "TS" in msg for msg in log)

    # snapshot.json exists
    assert (root / ".conex" / "snapshot.json").is_file()
    assert (root / ".conex" / "state.json").is_file()


# ---------------------------------------------------------------------------
# Scenario 2: Idempotent re-run
# ---------------------------------------------------------------------------


def test_idempotent_rerun(tmp_path):
    """Second export with no changes: zero writes, no new commit, mtimes unchanged."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Alpha", parent_id="", body="<p>Alpha</p>", version=1)
    api.add_page("p2", "Beta", parent_id="p1", body="<p>Beta</p>", version=1)

    _run_export(api, "TS", root)
    assert _git_log_count(root) == 1

    # Capture mtimes of all .md files
    md_mtimes: dict[str, float] = {}
    for p in root.rglob("*.md"):
        if ".conex" not in p.parts:
            md_mtimes[str(p)] = p.stat().st_mtime

    # Small sleep to ensure timestamps would differ if files were rewritten.
    time.sleep(0.05)

    # Second run — identical API state.
    _run_export(api, "TS", root)

    # No new git commit.
    assert _git_log_count(root) == 1

    # All .md mtimes unchanged.
    for p_str, old_mtime in md_mtimes.items():
        new_mtime = Path(p_str).stat().st_mtime
        assert new_mtime == old_mtime, f"mtime changed for {p_str}"

    # BuildResult skipped == 2, written == [].
    # (verified indirectly through no new commit and unchanged mtimes)
    state1 = _state(root)
    assert state1 is not None
    assert len(state1.pages) == 2


# ---------------------------------------------------------------------------
# Scenario 3: Title change → move with non-empty .workspace carried
# ---------------------------------------------------------------------------


def test_title_change_move_workspace(tmp_path):
    """Rename a page → new dir path, non-empty .workspace is carried along."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Old Title", parent_id="", body="<p>Old</p>", version=1)

    _run_export(api, "TS", root)

    state1 = _state(root)
    assert state1 is not None
    old_dir = root / state1.pages["p1"].dir

    # Plant a non-empty .workspace in the old dir.
    ws = old_dir / ".workspace"
    ws.mkdir()
    (ws / "note.txt").write_text("user note")

    # Rename the page (bump version so fingerprint changes).
    api.rename_page("p1", "New Title", version=2)

    _run_export(api, "TS", root)

    state2 = _state(root)
    assert state2 is not None
    new_dir = root / state2.pages["p1"].dir

    # Page directory changed.
    assert str(old_dir) != str(new_dir)
    assert state2.pages["p1"].title == "New Title"

    # New .md exists at new location.
    assert (root / state2.pages["p1"].file).is_file()

    # .workspace was carried to the new dir.
    new_ws = new_dir / ".workspace"
    assert new_ws.is_dir()
    assert (new_ws / "note.txt").read_text() == "user note"

    # Old dir is gone (was emptied).
    assert not old_dir.exists()


# ---------------------------------------------------------------------------
# Scenario 4: Reparent → move
# ---------------------------------------------------------------------------


def test_reparent_move(tmp_path):
    """Reparent a page to a different parent → directory path changes."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Parent A", parent_id="", body="<p>A</p>", version=1, position=0)
    api.add_page("p2", "Parent B", parent_id="", body="<p>B</p>", version=1, position=1)
    api.add_page("p3", "Child", parent_id="p1", body="<p>child</p>", version=1)

    _run_export(api, "TS", root)

    state1 = _state(root)
    old_child_dir = state1.pages["p3"].dir

    # Reparent child from p1 to p2.
    api.reparent_page("p3", "p2", version=2)

    _run_export(api, "TS", root)

    state2 = _state(root)
    new_child_dir = state2.pages["p3"].dir

    assert old_child_dir != new_child_dir
    assert "Parent-B" in new_child_dir or "Parent B" in new_child_dir or state2.pages["p3"].dir.startswith(state2.pages["p2"].dir)
    assert (root / state2.pages["p3"].file).is_file()


# ---------------------------------------------------------------------------
# Scenario 5: Upstream delete → prune + non-empty .workspace left + warning
# ---------------------------------------------------------------------------


def test_upstream_delete_prune_workspace(tmp_path, capsys):
    """Upstream page delete → .md removed, non-empty .workspace left with warning."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Keep Me", parent_id="", body="<p>Keep</p>", version=1)
    api.add_page("p2", "Delete Me", parent_id="", body="<p>Delete</p>", version=1)

    _run_export(api, "TS", root)

    state1 = _state(root)
    del_dir = root / state1.pages["p2"].dir
    del_md = root / state1.pages["p2"].file

    # Plant a non-empty .workspace.
    ws = del_dir / ".workspace"
    ws.mkdir()
    (ws / "user-file.txt").write_text("keep me")

    # Remove the page from the API.
    api.remove_page("p2")

    _run_export(api, "TS", root)

    # .md is gone.
    assert not del_md.exists()

    # .workspace was left behind (non-empty).
    assert ws.is_dir()
    assert (ws / "user-file.txt").exists()

    # State no longer contains p2.
    state2 = _state(root)
    assert "p2" not in state2.pages

    # Warning was emitted (check stderr).
    captured = capsys.readouterr()
    assert ".workspace" in captured.err


# ---------------------------------------------------------------------------
# Scenario 6: Archived — include then plain re-run preserves _archived/ (I3)
# ---------------------------------------------------------------------------


def test_archived_preservation_i3(tmp_path):
    """I3: plain re-run does NOT prune archived pages from a prior include-archived run."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Live Page", parent_id="", body="<p>live</p>", version=1)
    api.add_page("p2", "Archived Page", parent_id="", body="<p>archived</p>", version=1, status="archived")

    # First run with --include-archived.
    _run_export(api, "TS", root, extra_argv=["--include-archived"])

    state1 = _state(root)
    assert "p2" in state1.pages
    assert state1.pages["p2"].status == "archived"
    archived_md = root / state1.pages["p2"].file

    # Second run WITHOUT --include-archived (I3: archived must survive).
    _run_export(api, "TS", root)

    state2 = _state(root)
    assert "p2" in state2.pages, "I3 violated: archived page was pruned"
    assert archived_md.exists(), "I3 violated: archived .md was deleted"


# ---------------------------------------------------------------------------
# Scenario 7: Zero-pages guard (I2)
# ---------------------------------------------------------------------------


def test_zero_pages_guard_i2(tmp_path, capsys):
    """I2: fake returns no pages → nothing deleted, warning emitted."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Page One", parent_id="", body="<p>one</p>", version=1)

    _run_export(api, "TS", root)
    state1 = _state(root)
    md_file = root / state1.pages["p1"].file
    assert md_file.is_file()

    # Now return zero pages.
    api_empty = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")

    cfg = _fake_cfg()
    argv = ["export", "TS", "-o", str(root), "--no-author-lookup"]
    with (
        patch("conex.cli.resolve_config", return_value=cfg),
        patch("conex.api.make_api", return_value=api_empty),
    ):
        from conex.cli import main
        main(argv)

    # .md file must still be present.
    assert md_file.is_file(), "I2 violated: page was deleted when empty plan was returned"

    # Warning was emitted.
    captured = capsys.readouterr()
    # The CLI prints result.warnings to stderr; the I2 guard warns via warnings.warn.
    assert "I2" in captured.err or "skipping all pruning" in captured.err, (
        f"I2 warning not found in stderr: {captured.err!r}"
    )
    state2 = _state(root)
    # State must still record p1 (I2 returns prev state unchanged).
    assert "p1" in state2.pages


# ---------------------------------------------------------------------------
# Scenario 8: Lock contention (I5)
# ---------------------------------------------------------------------------


def test_lock_contention_i5(tmp_path):
    """I5: second export under a held lock raises LockHeldError, exits 1."""
    root = tmp_path / "export"
    root.mkdir()

    from conex.store.lock import ExportLock
    from conex.errors import LockHeldError

    # Hold the lock in this process.
    lock = ExportLock(root)
    lock.__enter__()
    try:
        api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
        api.add_page("p1", "Page", parent_id="", body="<p>x</p>")

        cfg = _fake_cfg()
        argv = ["export", "TS", "-o", str(root), "--no-author-lookup", "--no-git"]
        with (
            patch("conex.cli.resolve_config", return_value=cfg),
            patch("conex.api.make_api", return_value=api),
        ):
            from conex.cli import main
            with pytest.raises(SystemExit) as exc_info:
                main(argv)
            assert exc_info.value.code == 1
    finally:
        lock.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Scenario 9: --no-media preserves existing media
# ---------------------------------------------------------------------------


def test_no_media_preserves_existing(tmp_path):
    """--no-media does not delete previously materialised media files."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Page With Att", parent_id="", body="<p>x</p>", version=1)
    api.add_attachment(
        att_id="a1", title="doc.pdf", page_id="p1",
        media_type="application/pdf", version=1,
        content=b"pdf-content",
    )

    # First run: media downloaded.
    _run_export(api, "TS", root)
    state1 = _state(root)
    media_dir = root / state1.pages["p1"].dir / ".media"
    assert media_dir.is_dir()
    media_files_before = set(media_dir.iterdir())
    assert media_files_before

    # Update body to force re-render (bumping version).
    api.update_page_body("p1", "<p>updated</p>", version=2)

    # Second run with --no-media.
    _run_export(api, "TS", root, extra_argv=["--no-media"])

    # Media dir should still have the same files.
    assert media_dir.is_dir()
    media_files_after = set(p.name for p in media_dir.iterdir())
    media_files_before_names = set(p.name for p in media_files_before)
    assert media_files_before_names <= media_files_after


# ---------------------------------------------------------------------------
# Scenario 10: Attachment update re-downloads + page re-renders
# ---------------------------------------------------------------------------


def test_attachment_update_redownloads(tmp_path):
    """New attachment version → re-downloaded blob, page re-rendered."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Att Page", parent_id="", body="<p>text</p>", version=1)
    api.add_attachment(
        att_id="a1", title="file.txt", page_id="p1",
        media_type="text/plain", version=1,
        content=b"version-1-content",
    )

    _run_export(api, "TS", root)
    state1 = _state(root)
    media_dir = root / state1.pages["p1"].dir / ".media"
    old_content = (media_dir / "file.txt").read_bytes()
    assert old_content == b"version-1-content"

    # Update the attachment to version 2 with different content.
    api.update_attachment("a1", b"version-2-content", new_version=2)
    # Also bump page version so fingerprint changes.
    api.update_page_body("p1", "<p>text</p>", version=2)

    _run_export(api, "TS", root)
    state2 = _state(root)

    media_dir2 = root / state2.pages["p1"].dir / ".media"
    new_content = (media_dir2 / "file.txt").read_bytes()
    assert new_content == b"version-2-content", "attachment was not re-downloaded"

    att_state = state2.pages["p1"].attachments.get("a1")
    assert att_state is not None
    assert att_state.version == 2


# ---------------------------------------------------------------------------
# Scenario 11: drawio preview-first
# ---------------------------------------------------------------------------


def test_drawio_preview_fresh_png_used(tmp_path):
    """Preview-first: fresh .png sibling (arch.drawio.png) is used when its
    created_at >= xml created_at, and the drawio CLI is absent.

    The PNG sibling MUST be named '<source>.drawio.png' so find_drawio_pairs
    can pair it.  The page body contains a drawio macro so the rendered_drawio
    dict is consulted by convert and produces an img reference.
    """
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    # Body contains a drawio macro referencing the diagram by name.
    body = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch.drawio</ac:parameter>'
        '</ac:structured-macro>'
    )
    api.add_page("p1", "Diagram Page", parent_id="", body=body, version=1)
    # XML is older than PNG → preview is fresh.
    api.add_attachment(
        att_id="xml1", title="arch.drawio", page_id="p1",
        media_type="application/vnd.jgraph.mxfile",
        version=1, created_at="2024-01-01T00:00:00Z",
        content=b"<mxfile>xml</mxfile>",
    )
    # PNG sibling MUST be named '<source>.drawio.png' for find_drawio_pairs to pair it.
    api.add_attachment(
        att_id="png1", title="arch.drawio.png", page_id="p1",
        media_type="image/png",
        version=1, created_at="2024-06-01T00:00:00Z",  # newer → fresh
        content=b"\x89PNG\r\n\x1a\n fake-preview-bytes",
    )

    # drawio CLI absent: _DRAWIO_CLI=False prevents any render attempt.
    with patch("conex.drawio._DRAWIO_CLI", False):
        _run_export(api, "TS", root)

    state = _state(root)
    assert state is not None
    media_dir = root / state.pages["p1"].dir / ".media"

    # The preview PNG must be materialised on disk with the planned name.
    preview_on_disk = media_dir / "arch.drawio.png"
    assert preview_on_disk.is_file(), (
        f"Preview PNG not materialised at {preview_on_disk}; "
        f"contents of .media: {list(media_dir.iterdir()) if media_dir.is_dir() else 'absent'}"
    )
    # Bytes must equal the preview attachment content, proving the preview was
    # used rather than a batch-rendered image.
    assert preview_on_disk.read_bytes() == b"\x89PNG\r\n\x1a\n fake-preview-bytes", (
        "Preview PNG bytes do not match the fresh attachment content"
    )

    # The rendered markdown must reference the preview PNG via the drawio macro.
    md_content = (root / state.pages["p1"].file).read_text()
    assert "arch.drawio.png" in md_content, (
        f"Markdown does not reference arch.drawio.png; md snippet:\n{md_content[:500]}"
    )
    # Must NOT contain the 'not rendered' placeholder.
    assert "not rendered" not in md_content, (
        "Markdown contains 'not rendered' placeholder; preview-first path was not taken"
    )


def test_drawio_stale_png_cli_absent_placeholder(tmp_path):
    """Stale png + absent CLI → preview NOT used, 'not rendered' placeholder emitted.

    The PNG sibling MUST be named 'old.drawio.png' so find_drawio_pairs pairs
    it.  Because png.created_at < xml.created_at the preview is stale; because
    the CLI is absent render_batch returns {}; the macro handler emits the
    [Draw.io diagram not rendered: ...] placeholder.
    """
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    # Body contains a drawio macro so convert_page exercises _emit_drawio.
    body = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">old.drawio</ac:parameter>'
        '</ac:structured-macro>'
    )
    api.add_page("p1", "Stale Diagram", parent_id="", body=body, version=1)
    # XML is NEWER than PNG → preview is stale.
    api.add_attachment(
        att_id="xml2", title="old.drawio", page_id="p1",
        media_type="application/vnd.jgraph.mxfile",
        version=1, created_at="2024-06-01T00:00:00Z",  # xml newer → preview stale
        content=b"<mxfile>xml</mxfile>",
    )
    # PNG sibling MUST be named '<source>.drawio.png' for find_drawio_pairs to pair it.
    api.add_attachment(
        att_id="png2", title="old.drawio.png", page_id="p1",
        media_type="image/png",
        version=1, created_at="2024-01-01T00:00:00Z",  # png older → stale
        content=b"\x89PNG\r\n\x1a\n stale-preview-bytes",
    )

    # drawio CLI absent: render_batch returns {} because no CLI is available.
    with patch("conex.drawio._DRAWIO_CLI", False):
        _run_export(api, "TS", root)

    # Build must succeed and state written.
    state = _state(root)
    assert state is not None
    assert "p1" in state.pages

    # The markdown must NOT reference old.drawio.png as a rendered image —
    # the stale preview must not be promoted as if it were fresh.
    md_content = (root / state.pages["p1"].file).read_text()
    # The 'not rendered' placeholder must appear because CLI is absent and
    # preview is stale (no render path succeeded).
    assert "not rendered" in md_content, (
        "Expected 'not rendered' placeholder for stale preview + absent CLI; "
        f"md snippet:\n{md_content[:500]}"
    )


# ---------------------------------------------------------------------------
# Scenario 12: Crash simulation — state.json not corrupted; re-run converges
# ---------------------------------------------------------------------------


def test_crash_simulation_state_not_corrupted(tmp_path):
    """Crash mid-walk (os.replace after first page) → state.json intact,
    .conex/tmp leftover present, re-run converges to the same tree.

    This exercises I6 end-to-end:
    (a) a true mid-walk crash (os.replace raises after the 1st page lands)
        leaves the OLD state.json untouched and a .conex/tmp/*.tmp leftover.
    (b) A subsequent re-run through cli.main clears .conex/tmp and converges
        to exactly the same .md bytes as an uninterrupted run, proving the
        leftover did not poison the result.
    We also keep a save-crash sub-case (StateStore.save raises) to confirm
    that path too leaves the old state intact.
    """
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Page One", parent_id="", body="<p>one</p>", version=1)
    api.add_page("p2", "Page Two", parent_id="", body="<p>two</p>", version=1)

    # Do a successful first export to establish prev state.
    _run_export(api, "TS", root)
    state_before = _state(root)
    assert state_before is not None

    # Update both pages so the second run would need to rewrite them.
    api.update_page_body("p1", "<p>one updated</p>", version=2)
    api.update_page_body("p2", "<p>two updated</p>", version=2)

    # -----------------------------------------------------------------------
    # Sub-case A: mid-walk crash via os.replace raising after the first page.
    #
    # build.py writes each page as:
    #   md_tmp = tmp_dir / f"page-{pid}.md.tmp"
    #   md_tmp.write_text(...)                     ← tmp file exists here
    #   os.replace(md_tmp, planned_file_abs)       ← we crash here for page 2
    #
    # We filter on .md.tmp in the source path so blob-store and snapshot
    # os.replace calls are unaffected.  After the crash:
    #   - page-p2.md.tmp leftover exists in .conex/tmp/
    #   - state.json is the OLD state (never written because crash precedes Step 7)
    # -----------------------------------------------------------------------
    import conex.build as _build_mod

    original_replace = os.replace
    md_replace_call_count = [0]

    def crashing_replace(src, dst):
        # Only intercept .md.tmp writes; let all other os.replace calls through.
        if str(src).endswith(".md.tmp"):
            md_replace_call_count[0] += 1
            if md_replace_call_count[0] == 2:
                # First page already landed; crash on the second page's .md.tmp.
                raise RuntimeError("simulated mid-walk crash on os.replace (page 2)")
        return original_replace(src, dst)

    with patch.object(_build_mod.os, "replace", crashing_replace):
        try:
            _run_export(api, "TS", root, extra_argv=["--no-git"])
        except (RuntimeError, SystemExit):
            pass

    # (a) state.json must still be the OLD state (I6 — written only at the end).
    state_after_crash = _state(root)
    assert state_after_crash is not None, "state.json was corrupted by mid-walk crash"
    assert set(state_after_crash.pages.keys()) == set(state_before.pages.keys()), (
        "state.json page set changed after crash; I6 violated"
    )
    # Old versions must be preserved.
    for pid in state_before.pages:
        assert state_after_crash.pages[pid].version == state_before.pages[pid].version, (
            f"state.json version for {pid!r} changed after crash"
        )

    # (b) A .conex/tmp leftover must exist (the second page's tmp was written
    #     before os.replace raised for it).
    tmp_dir = root / ".conex" / "tmp"
    tmp_leftovers = list(tmp_dir.glob("*.tmp")) if tmp_dir.is_dir() else []
    assert tmp_leftovers, (
        f"Expected a .conex/tmp/*.tmp leftover after mid-walk crash; "
        f"tmp dir contents: {list(tmp_dir.iterdir()) if tmp_dir.is_dir() else 'absent'}"
    )

    # (c) Re-run through cli.main clears tmp and converges.
    _run_export(api, "TS", root, extra_argv=["--no-git"])
    state_final = _state(root)
    assert state_final is not None
    assert "p1" in state_final.pages
    assert "p2" in state_final.pages
    assert state_final.pages["p1"].version == 2
    assert state_final.pages["p2"].version == 2

    # The .md bytes must match an uninterrupted run on a clean copy.
    root2 = tmp_path / "export_clean"
    root2.mkdir()
    _init_git_repo(root2)
    api2 = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api2.add_page("p1", "Page One", parent_id="", body="<p>one updated</p>", version=2)
    api2.add_page("p2", "Page Two", parent_id="", body="<p>two updated</p>", version=2)
    _run_export(api2, "TS", root2, extra_argv=["--no-git"])
    state_clean = _state(root2)
    for pid in ("p1", "p2"):
        crashed_md = (root / state_final.pages[pid].file).read_text()
        clean_md = (root2 / state_clean.pages[pid].file).read_text()
        assert crashed_md == clean_md, (
            f"Re-run after crash produced different .md for {pid!r}; "
            f"leftover likely poisoned the result"
        )

    # -----------------------------------------------------------------------
    # Sub-case B: save-crash (StateStore.save raises) also leaves old state.
    # -----------------------------------------------------------------------
    api.update_page_body("p1", "<p>one v3</p>", version=3)
    api.update_page_body("p2", "<p>two v3</p>", version=3)
    state_before_save_crash = _state(root)

    from conex.store import state as state_mod

    def crashing_save(self, state_obj):
        raise RuntimeError("simulated crash mid-save")

    with patch.object(state_mod.StateStore, "save", crashing_save):
        try:
            _run_export(api, "TS", root, extra_argv=["--no-git"])
        except (RuntimeError, SystemExit):
            pass

    state_after_save_crash = _state(root)
    assert state_after_save_crash is not None, "state.json corrupted by save-crash"
    assert set(state_after_save_crash.pages.keys()) == set(state_before_save_crash.pages.keys())


# ---------------------------------------------------------------------------
# Scenario 13: --cached offline
# ---------------------------------------------------------------------------


def test_cached_offline(tmp_path):
    """--cached: build from cached snapshot with API removed/forbidden."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Cached Page", parent_id="", body="<p>cached</p>", version=1)

    # First run: pull + build.
    _run_export(api, "TS", root)
    state1 = _state(root)
    assert "p1" in state1.pages

    # Delete output to force a re-build from cache.
    md_file = root / state1.pages["p1"].file
    md_file.unlink()

    # Second run with --cached and a broken API.
    broken_api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    # broken_api has no pages — but --cached must NOT call the API.

    cfg = _fake_cfg()
    argv = ["export", "TS", "-o", str(root), "--no-author-lookup", "--cached"]
    with (
        patch("conex.cli.resolve_config", return_value=cfg),
        patch("conex.api.make_api", return_value=broken_api),
    ):
        from conex.cli import main
        main(argv)

    # Page was rebuilt from the snapshot.
    state2 = _state(root)
    assert "p1" in state2.pages
    assert (root / state2.pages["p1"].file).is_file()


def test_cached_no_snapshot_error(tmp_path, capsys):
    """--cached with no snapshot file → clean error, exit 1."""
    root = tmp_path / "export"
    root.mkdir()

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    cfg = _fake_cfg()
    argv = ["export", "TS", "-o", str(root), "--no-author-lookup", "--cached", "--no-git"]
    with (
        patch("conex.cli.resolve_config", return_value=cfg),
        patch("conex.api.make_api", return_value=api),
    ):
        from conex.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(argv)
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "cached" in captured.err.lower() or "snapshot" in captured.err.lower()


# ---------------------------------------------------------------------------
# Scenario 14: Subtree export does NOT prune outside scope
# ---------------------------------------------------------------------------


def test_subtree_does_not_prune_outside_scope(tmp_path):
    """--path /Section-B restricts build to Section B; Section A pages untouched."""
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("pa1", "Section A", parent_id="", body="<p>A</p>", version=1, position=0)
    api.add_page("pa2", "Section A Child", parent_id="pa1", body="<p>A child</p>", version=1)
    api.add_page("pb1", "Section B", parent_id="", body="<p>B</p>", version=1, position=1)
    api.add_page("pb2", "Section B Child", parent_id="pb1", body="<p>B child</p>", version=1)

    # Full export first to populate state.
    _run_export(api, "TS", root)
    state1 = _state(root)
    assert len(state1.pages) == 4

    # Remember where Section A's files are.
    a_md = root / state1.pages["pa1"].file
    a_child_md = root / state1.pages["pa2"].file
    assert a_md.is_file()
    assert a_child_md.is_file()

    # Subtree-only export for Section B.
    _run_export(api, "TS", root, extra_argv=["--path", "/Section B"])

    # Section A files must still exist.
    assert a_md.is_file(), "Section A pruned despite being outside subtree scope"
    assert a_child_md.is_file(), "Section A child pruned despite being outside subtree scope"

    # Section B is still present.
    state2 = _state(root)
    assert "pb1" in state2.pages
    assert "pb2" in state2.pages


# ---------------------------------------------------------------------------
# Scenario 15: Git log shape — local user edit + upstream change
# ---------------------------------------------------------------------------


def test_git_log_shape_local_then_export(tmp_path):
    """Local user change committed BEFORE the export commit; unrelated tracked-
    but-modified file does NOT appear in the export commit (I8 exact-delta).

    The key I8 property: a tracked file modified by the user (not conex-owned)
    must NOT appear in the EXPORT commit.  An untracked file proves nothing
    because `git add -- <specific>` never picks it up; only a tracked-but-
    modified file exercises the real I8 boundary.
    """
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Page", parent_id="", body="<p>v1</p>", version=1)

    # First export to establish tracked files.
    _run_export(api, "TS", root)
    assert _git_log_count(root) == 1

    state1 = _state(root)
    md_path = root / state1.pages["p1"].file

    # Create and commit an unrelated tracked file (it will be MODIFIED before
    # the next export to prove exact-delta staging).
    doc_txt = root / "doc.txt"
    doc_txt.write_text("original content")
    subprocess.run(["git", "add", str(doc_txt)], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add doc.txt"],
        cwd=root, check=True, capture_output=True,
    )

    # User edits the tracked .md file (this should end up in the "Local
    # changes before export" commit, NOT in the export commit).
    original = md_path.read_text()
    md_path.write_text(original + "\n<!-- user note -->")

    # User also MODIFIES the unrelated tracked file (dirty, unstaged).
    # This must NOT appear in the EXPORT commit (I8 exact-delta).
    doc_txt.write_text("modified by user")

    # User also creates an UNTRACKED scratch file (must not be committed at all).
    untracked = root / "scratch.txt"
    untracked.write_text("scratch notes")

    # Upstream change: bump page version so the export rewrites the .md.
    api.update_page_body("p1", "<p>v2</p>", version=2)

    _run_export(api, "TS", root)

    log = _git_log(root)
    assert len(log) >= 3, f"Expected at least 3 commits, got: {log}"

    # Commits are newest-first:
    #   log[0] = export commit (conex export TS ...)
    #   log[1] = "Local changes before export"
    #   log[2] = "Add doc.txt"
    #   log[3] = first export commit
    assert "Local changes before export" in log[1], f"Unexpected commit order: {log}"
    assert "conex export" in log[0] or "TS" in log[0], f"Unexpected export commit: {log}"

    # -----------------------------------------------------------------------
    # I8 primary proof: the EXPORT commit must NOT contain doc.txt.
    # The tracked-but-modified doc.txt landed in "Local changes before export"
    # (via git add -u), so it must not be re-staged in the export commit.
    # -----------------------------------------------------------------------
    export_commit_files = subprocess.run(
        ["git", "show", "HEAD", "--name-only", "--format="],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    export_commit_files = [f for f in export_commit_files if f.strip()]

    assert "doc.txt" not in export_commit_files, (
        f"doc.txt (tracked-but-modified unrelated file) incorrectly included "
        f"in the export commit: {export_commit_files}"
    )

    # The export commit must contain the rebuilt page .md.
    page_relpath = state1.pages["p1"].file.replace(os.sep, "/")
    assert any(page_relpath in f or Path(page_relpath).name in f for f in export_commit_files), (
        f"Expected page .md in export commit; got: {export_commit_files}"
    )

    # Untracked scratch.txt must not appear in any commit.
    all_commits_files = subprocess.run(
        ["git", "show", "HEAD", "--name-only", "--format="],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    assert "scratch.txt" not in all_commits_files


# ---------------------------------------------------------------------------
# Extra: git delta is exact (only changed files staged)
# ---------------------------------------------------------------------------


def test_git_exact_delta_staging(tmp_path):
    """commit_export stages ONLY written+deleted paths (I8 exact-delta).

    A tracked-but-modified unrelated file must NOT appear in the export commit.
    (An untracked file proves nothing — git add -- <path> never picks it up
    regardless of correctness; only a tracked-modified file is the real test.)
    """
    root = tmp_path / "export"
    root.mkdir()
    _init_git_repo(root)

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Page One", parent_id="", body="<p>v1</p>", version=1)
    api.add_page("p2", "Page Two", parent_id="", body="<p>v2</p>", version=1)

    _run_export(api, "TS", root)

    # Stage and commit a user file so it is tracked.
    readme = root / "README.md"
    readme.write_text("# Project")
    subprocess.run(["git", "add", str(readme)], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add README"],
        cwd=root, check=True, capture_output=True,
    )

    # Modify the tracked file (dirty, unstaged) — this is the real I8 proof.
    readme.write_text("# Project\n\nuser edit")

    # Only update p1 to cause an export commit.
    api.update_page_body("p1", "<p>v1 updated</p>", version=2)

    _run_export(api, "TS", root)

    # The export commit (HEAD) must include p1's .md but NOT README.md.
    # README.md should have landed in the "Local changes before export" commit.
    result = subprocess.run(
        ["git", "show", "HEAD", "--name-only", "--format="],
        cwd=root, capture_output=True, text=True, check=True,
    )
    changed_files = [f for f in result.stdout.splitlines() if f.strip()]
    assert "README.md" not in changed_files, (
        f"README.md (tracked-but-modified) incorrectly included in export commit: {changed_files}"
    )

    # p1's .md must be in the export commit.
    state = _state(root)
    p1_file = state.pages["p1"].file.replace(os.sep, "/")
    assert any(p1_file in f or Path(p1_file).name in f for f in changed_files), (
        f"p1 .md not in export commit; got: {changed_files}"
    )


# ---------------------------------------------------------------------------
# Extra: pull + build round-trip without CLI
# ---------------------------------------------------------------------------


def test_pull_build_direct(tmp_path):
    """Direct pull+build without CLI: produces correct tree and state."""
    root = tmp_path / "export"
    root.mkdir()

    from conex.build import BuildOptions, build
    from conex.pull import PullOptions, pull
    from conex.store.blobs import BlobStore
    from conex.store.state import StateStore, SnapshotStore

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Direct Page", parent_id="", body="<p>direct</p>", version=1)

    blobs = BlobStore(root)
    # Clear tmp directory.
    tmp_dir = root / ".conex" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    snap = pull(api, "TS", root, blobs, None, PullOptions(fetch_media=False, author_lookup=False))
    assert len(snap.pages) == 1

    result, state = build(root, snap, blobs, None, BuildOptions(media=False))
    assert len(result.written) == 1
    assert len(state.pages) == 1
    assert state.pages["p1"].title == "Direct Page"
    assert (root / state.pages["p1"].file).is_file()


# ---------------------------------------------------------------------------
# Extra: refresh flow
# ---------------------------------------------------------------------------


def test_refresh_flow(tmp_path):
    """refresh command updates snapshot without touching the output tree."""
    root = tmp_path / "export"
    root.mkdir()

    api = FakeConfluenceAPI(space_key="TS", space_id="SP1", space_name="Test Space")
    api.add_page("p1", "Refresh Page", parent_id="", body="<p>r1</p>", version=1)

    _run_export(api, "TS", root, extra_argv=["--no-git"])

    api.update_page_body("p1", "<p>r2</p>", version=2)

    _run_refresh(api, "TS", root)

    snap = _snapshot(root)
    assert snap is not None
    # Snapshot has the page still (body is in blobs).
    assert len(snap.pages) == 1
    assert snap.pages[0].version.number == 2
