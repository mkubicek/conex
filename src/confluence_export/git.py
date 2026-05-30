"""Git versioning for export directories."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from confluence_export.media import WORKSPACE_DIR_NAME

# Conservative per-call argv budget for git add / git rm path lists. macOS
# ARG_MAX is 1 MiB (and includes the environment block), so 100 KiB leaves
# ~90% headroom for env vars, argv pointers, and the "git add --" prefix.
# Batches above this size risk OSError(7) "Argument list too long" on macOS.
_MAX_ARGV_BYTES = 100_000


def _chunked_paths(paths: list[str], max_bytes: int = _MAX_ARGV_BYTES) -> Iterator[list[str]]:
    """Yield batches of paths whose joined byte length stays under max_bytes."""
    batch: list[str] = []
    batch_bytes = 0
    for p in paths:
        # +1 accounts for the per-argv overhead (separator/pointer alignment)
        size = len(p.encode("utf-8")) + 1
        if batch and batch_bytes + size > max_bytes:
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(p)
        batch_bytes += size
    if batch:
        yield batch


def _is_secret_config_relpath(path: str) -> bool:
    """True for any file inside a local .conex directory."""
    return any(part.lower() == ".conex" for part in Path(path).parts)


def _is_secret_config_path(output_dir: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(output_dir.resolve())
        return _is_secret_config_relpath(str(rel))
    except ValueError:
        return _is_secret_config_relpath(str(path))


def git_available() -> bool:
    """Check if git is installed and accessible."""
    return shutil.which("git") is not None


def _run_git(
    repo_dir: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess | None:
    """Run a git command in repo_dir. Returns None on failure (with warning)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  Warning: git {args[0]} failed: {exc.stderr.strip()}", file=sys.stderr)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"  Warning: git {args[0]} failed: {exc}", file=sys.stderr)
        return None


def _has_commits(repo_dir: Path) -> bool:
    """Check whether the repo has at least one commit."""
    result = _run_git(repo_dir, "rev-parse", "HEAD", check=False)
    return result is not None and result.returncode == 0


def ensure_repo(output_dir: Path) -> bool:
    """Ensure output_dir is inside a git repo (init if needed). Returns True if usable."""
    # Check if already inside a git repo
    result = _run_git(output_dir, "rev-parse", "--git-dir", check=False)
    if result and result.returncode == 0:
        return True

    # Initialize a new repo with a fallback identity
    print("Initializing git repository...", file=sys.stderr)
    if _run_git(output_dir, "init") is None:
        return False
    _run_git(output_dir, "config", "user.name", "confluence-export")
    _run_git(output_dir, "config", "user.email", "confluence-export@localhost")
    return True


def commit_local_changes(output_dir: Path) -> bool:
    """Commit modifications to already-tracked files (pre-export safety commit).

    Only stages changes to tracked files (git add -u), ignoring untracked files.
    Skips entirely on a fresh repo with no commits.
    Returns True if a commit was made.
    """
    if not _has_commits(output_dir):
        return False

    # Stage only modifications to tracked files within output_dir
    if _run_git(output_dir, "add", "-u", ".") is None:
        return False
    _unstage_secret_configs(output_dir)

    # Check if anything was staged
    result = _run_git(output_dir, "diff", "--cached", "--quiet", check=False)
    if result is None or result.returncode == 0:
        return False

    return _run_git(output_dir, "commit", "-m", "Local changes before Confluence export") is not None


def commit_export(
    output_dir: Path,
    written_files: list[Path],
    space_key: str,
    *,
    is_full: bool = True,
) -> bool:
    """Stage exporter-written files and remove stale tracked files.

    Stages written_files, then (only on a full export) removes any tracked files
    under output_dir that are not in written_files (handles upstream deletions,
    renames, and moves). Returns True if a commit was made.

    ``is_full`` must be False for filtered / single-subtree / no-children
    exports: those write only part of the tree, so written_files covers only
    that subset. Pruning then would ``git rm`` the entire rest of the repo. A
    partial export is therefore strictly add-only.

    A moved page is handled as a plain delete + add: the reconciler drops the old
    path's markdown/.media and the write walk regenerates them at the new path,
    so the stale-file prune removes the old path and git's own rename detection
    makes history follow — no sidecar relocation or index patching needed (issue
    #17, Option B). The user's ``.workspace`` is never moved; it is preserved by
    the prune skip below and stays where the user put it.
    """
    # Stage only the files the exporter wrote. Chunk to avoid hitting the
    # OS argv limit on big spaces (thousands of paths joined into one exec
    # call exceeds macOS ARG_MAX).
    paths = [str(f) for f in written_files if not _is_secret_config_path(output_dir, f)]
    for batch in _chunked_paths(paths):
        if _run_git(output_dir, "add", "--", *batch) is None:
            return False
    _unstage_secret_configs(output_dir)

    # Remove stale tracked files (deletions/renames/moves upstream). Only on a
    # full export — a partial export's written_files is not the whole tree.
    if is_full:
        _remove_stale_files(output_dir, written_files)

    # Check if anything was staged
    result = _run_git(output_dir, "diff", "--cached", "--quiet", check=False)
    if result is None or result.returncode == 0:
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"Export Confluence space {space_key} ({timestamp})"
    return _run_git(output_dir, "commit", "-m", msg) is not None


def _fs_is_case_insensitive(output_dir: Path) -> bool:
    """Whether output_dir's filesystem folds case (macOS/Windows default), by
    probing it live. git's core.ignorecase is NOT a reliable oracle here: it is
    fixed at init/clone time and drifts from reality when a repo created on one
    filesystem is later exported on another (e.g. a Linux/CI clone opened on a
    Mac, or vice versa) — getting it wrong either re-introduces the stale-prune
    churn or wrongly spares a genuinely-deleted file. Measuring the actual
    filesystem is what governs whether a write and the index share an inode.

    The probe is a uniquely-named temp file created with O_EXCL (via mkstemp), so
    it can never clobber or be spoofed by a real user file: it tests whether an
    upper-cased variant of *that unique name* resolves to the same file."""
    try:
        fd, probe_name = tempfile.mkstemp(dir=output_dir, prefix=".conex-case-")
    except OSError:
        return False
    os.close(fd)
    probe = Path(probe_name)
    try:
        variant = probe.with_name(probe.name.upper())
        return variant != probe and variant.exists()
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _remove_stale_files(
    output_dir: Path,
    written_files: list[Path],
) -> None:
    """Remove tracked files that are no longer part of the export."""
    if not _has_commits(output_dir):
        return

    result = _run_git(output_dir, "ls-files", "-z", ".", check=False)
    if result is None or not result.stdout.strip("\0"):
        return

    # Normalize the comparison key on two axes that git itself folds but
    # Path.resolve() does not:
    #   - Unicode form: a macOS attachment title can arrive NFD (decomposed); git
    #     with core.precomposeunicode (the macOS default) stores it NFC, so
    #     `git ls-files` returns NFC while `written_files` is still NFD. Comparing
    #     raw strings flags the file stale and `git rm` then fails on its just-
    #     staged content (issue #15). Fold both sides to NFC.
    #   - Case: on a case-insensitive filesystem a fresh write and the index can
    #     differ only in case (a re-cased title, or legacy content from another
    #     machine); git folds those to one path, so we must too, or the same file
    #     is both git-added (new case) and flagged stale (old case).
    _nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
    fold = (
        (lambda s: _nfc(s).casefold())
        if _fs_is_case_insensitive(output_dir)
        else _nfc
    )
    written_keys = {fold(str(f.resolve())) for f in written_files}

    stale = []
    for rel_path in result.stdout.strip("\0").split("\0"):
        # Preserve user workspace directories across re-exports. On a move conex
        # leaves a committed .workspace tracked at its old path (it is never
        # relocated — issue #17, Option B); the user moves it themselves.
        parts = Path(rel_path).parts
        if WORKSPACE_DIR_NAME in parts:
            continue
        if _is_secret_config_relpath(rel_path):
            continue
        full = (output_dir / rel_path).resolve()
        if fold(str(full)) not in written_keys:
            stale.append(rel_path)

    if stale:
        # Same chunking rationale as commit_export: a large stale list can
        # exceed argv limits when passed to a single git rm call. If a batch
        # fails (e.g. one path has a staged rename from reconcile), fall back to
        # per-path removal so one un-removable path can't block pruning the rest.
        for batch in _chunked_paths(stale):
            result = _run_git(output_dir, "rm", "--quiet", "--", *batch, check=False)
            if result is None or result.returncode != 0:
                for path in batch:
                    r = _run_git(output_dir, "rm", "--quiet", "--", path, check=False)
                    if r is None or r.returncode != 0:
                        print(
                            f"  Warning: could not remove stale file '{path}'; "
                            "it survived the prune",
                            file=sys.stderr,
                        )


def _unstage_secret_configs(output_dir: Path) -> None:
    """Undo any accidental staging of local .conex files."""
    result = _run_git(output_dir, "diff", "--cached", "--name-only", "-z", check=False)
    if result is None or not result.stdout:
        return
    secret_paths = [
        path for path in result.stdout.strip("\0").split("\0")
        if path and _is_secret_config_relpath(path)
    ]
    for batch in _chunked_paths(secret_paths):
        _run_git(output_dir, "reset", "-q", "HEAD", "--", *batch, check=False)
