"""Git versioning for export directories."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


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

    # Check if anything was staged
    result = _run_git(output_dir, "diff", "--cached", "--quiet", check=False)
    if result is None or result.returncode == 0:
        return False

    return _run_git(output_dir, "commit", "-m", "Local changes before Confluence export") is not None


def commit_export(output_dir: Path, written_files: list[Path], space_key: str) -> bool:
    """Stage exporter-written files and remove stale tracked files.

    Stages written_files, then removes any tracked files under output_dir that
    are not in written_files (handles upstream deletions, renames, and moves).
    Returns True if a commit was made.
    """
    # Stage only the files the exporter wrote
    paths = [str(f) for f in written_files]
    if _run_git(output_dir, "add", "--", *paths) is None:
        return False

    # Remove stale tracked files (deletions/renames/moves upstream)
    _remove_stale_files(output_dir, written_files)

    # Check if anything was staged
    result = _run_git(output_dir, "diff", "--cached", "--quiet", check=False)
    if result is None or result.returncode == 0:
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"Export Confluence space {space_key} ({timestamp})"
    return _run_git(output_dir, "commit", "-m", msg) is not None


def _remove_stale_files(output_dir: Path, written_files: list[Path]) -> None:
    """Remove tracked files that are no longer part of the export."""
    if not _has_commits(output_dir):
        return

    result = _run_git(output_dir, "ls-files", "-z", ".", check=False)
    if result is None or not result.stdout.strip("\0"):
        return

    written_resolved = {f.resolve() for f in written_files}
    stale = []
    for rel_path in result.stdout.strip("\0").split("\0"):
        # Preserve user workspace directories across re-exports
        parts = Path(rel_path).parts
        if ".workspace" in parts:
            continue
        full = (output_dir / rel_path).resolve()
        if full not in written_resolved:
            stale.append(rel_path)

    if stale:
        _run_git(output_dir, "rm", "--quiet", "--", *stale)
