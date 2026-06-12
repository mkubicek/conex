"""Thin git layer for conex v2.

Invariants:
- I8: export commits stage EXACTLY the build's delta (written + deleted),
  chunked to respect argv limits. Never git add -A/-u for export commits.
- .conex/ is gitignored; any accidentally force-added .conex path is
  unstaged before every commit (PORT v1 _unstage_secret_configs).
- User-modified tracked files are committed BEFORE the export commit.
- Missing git binary raises GitError; cli degrades to a warning.

BuildResult seam: imported lazily from conex.build when available.  Tests
supply a duck-typed stand-in.  The interface is frozen per SPEC-V2.md:
    written: list[Path], deleted: list[Path], skipped: int,
    moved: list[tuple[str, str]], warnings: list[str]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conex.errors import GitError

if TYPE_CHECKING:
    # Import only for type-checking; the module may not exist at runtime.
    from conex.build import BuildResult  # noqa: F401

# Conservative per-call argv budget (macOS ARG_MAX ≈ 1 MiB; 100 KiB leaves
# ample headroom for env vars, argv pointers, and the "git add --" prefix).
_MAX_ARGV_BYTES: int = 100_000

# Seam constant used by tests to force small batches.
_CHUNK_SIZE_BYTES: int = _MAX_ARGV_BYTES


def _chunked_paths(paths: list[str], max_bytes: int | None = None) -> Iterator[list[str]]:
    """Yield batches of paths whose joined byte length stays under max_bytes.

    Invariant: every non-empty input path list produces at least one batch.
    """
    limit = max_bytes if max_bytes is not None else _CHUNK_SIZE_BYTES
    batch: list[str] = []
    batch_bytes = 0
    for p in paths:
        size = len(p.encode("utf-8")) + 1  # +1 for per-argv overhead
        if batch and batch_bytes + size > limit:
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(p)
        batch_bytes += size
    if batch:
        yield batch


def _is_conex_relpath(path: str) -> bool:
    """True for any file inside a .conex directory (any case-match)."""
    return any(part.lower() == ".conex" for part in Path(path).parts)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run_git(repo_dir: Path, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    """Run a git command in repo_dir.

    Raises GitError on CalledProcessError, TimeoutExpired, or FileNotFoundError
    (missing binary). check=False suppresses CalledProcessError but still
    raises GitError for infrastructure failures.
    """
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
        raise GitError(f"git {args[0]} failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    except FileNotFoundError as exc:
        raise GitError("git binary not found; install git and try again") from exc


def _has_commits(repo_dir: Path) -> bool:
    """Return True if the repo has at least one commit."""
    try:
        result = _run_git(repo_dir, "rev-parse", "HEAD", check=False)
        return result.returncode == 0
    except GitError:
        return False


def _ensure_gitignore_has_conex(root: Path) -> None:
    """Ensure root/.gitignore contains the .conex/ entry."""
    gitignore = root / ".gitignore"
    entry = ".conex/"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if entry in content.splitlines():
            return
        sep = "" if content.endswith("\n") else "\n"
        gitignore.write_text(content + sep + entry + "\n", encoding="utf-8")
    else:
        gitignore.write_text(entry + "\n", encoding="utf-8")


def _unstage_conex_paths(repo_dir: Path) -> None:
    """Unstage any accidentally staged .conex/ paths.

    PORT v1 _unstage_secret_configs: covers a force-added .conex directory.
    """
    try:
        result = _run_git(repo_dir, "diff", "--cached", "--name-only", "-z", check=False)
    except GitError:
        return
    if not result.stdout:
        return
    secret = [
        p for p in result.stdout.strip("\0").split("\0")
        if p and _is_conex_relpath(p)
    ]
    for batch in _chunked_paths(secret):
        try:
            _run_git(repo_dir, "reset", "-q", "HEAD", "--", *batch, check=False)
        except GitError:
            pass


def ensure_repo(root: Path) -> bool:
    """Ensure root is inside a git repo; init if absent.

    Sets user.name/email fallback ONLY on fresh init (never clobbers an
    existing repo's identity — v1 behavior).  Ensures .gitignore contains
    .conex/.

    Returns True when the repo is usable; raises GitError on failure.
    """
    if not _git_available():
        raise GitError("git binary not found; install git and try again")

    root.mkdir(parents=True, exist_ok=True)
    try:
        result = _run_git(root, "rev-parse", "--git-dir", check=False)
    except GitError:
        result = None  # type: ignore[assignment]

    if result is not None and result.returncode == 0:
        # Already inside a repo — touch only .gitignore, never identity.
        _ensure_gitignore_has_conex(root)
        return True

    # Fresh init.
    print("Initializing git repository...", file=sys.stderr)
    _run_git(root, "init")
    _run_git(root, "config", "user.name", "conex")
    _run_git(root, "config", "user.email", "conex@localhost")
    _ensure_gitignore_has_conex(root)
    return True


def commit_user_changes(root: Path) -> bool:
    """Stage tracked modifications and commit them before the export.

    PORT v1 commit_local_changes semantics:
    - Only stages changes to tracked files (git add -u).
    - Then unstages any .conex/ paths (covers a force-added .conex).
    - Skips on a fresh repo with no commits.
    - Returns True iff a commit was created.

    Raises GitError on subprocess failures.
    """
    if not _git_available():
        raise GitError("git binary not found")

    if not _has_commits(root):
        return False

    _run_git(root, "add", "-u", ".")
    _unstage_conex_paths(root)

    result = _run_git(root, "diff", "--cached", "--quiet", check=False)
    if result.returncode == 0:
        return False  # nothing staged

    _run_git(root, "commit", "-m", "Local changes before export")
    return True


def commit_export(root: Path, result: Any, message: str) -> bool:
    """Stage exactly the build's written + deleted paths and commit.

    Invariant I8: stages ONLY result.written (additions/updates) and
    result.deleted (removals). Never uses git add -A or -u.  Paths are
    chunked to respect argv limits.  .conex/ paths are unstaged after
    staging (belt-and-suspenders).

    Returns True iff a commit was created (empty delta → False).
    Raises GitError on subprocess failure.
    """
    if not _git_available():
        raise GitError("git binary not found")

    written: list[Path] = result.written
    deleted: list[Path] = result.deleted

    # Stage written paths (additions and updates).
    written_strs = [str(p) for p in written if not _is_conex_relpath(str(p))]
    for batch in _chunked_paths(written_strs):
        _run_git(root, "add", "--", *batch)

    # Stage deleted paths (git add -- on a missing file records the deletion).
    deleted_strs = [str(p) for p in deleted if not _is_conex_relpath(str(p))]
    for batch in _chunked_paths(deleted_strs):
        _run_git(root, "add", "--", *batch)

    _unstage_conex_paths(root)

    # Commit only if something is staged.
    check_result = _run_git(root, "diff", "--cached", "--quiet", check=False)
    if check_result.returncode == 0:
        return False  # empty delta

    _run_git(root, "commit", "-m", message)
    return True
