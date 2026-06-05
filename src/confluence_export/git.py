"""Git versioning for export directories."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from confluence_export.media import MEDIA_DIR_NAME, WORKSPACE_DIR_NAME
from confluence_export.paths import nfc, nfc_casefold

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
    protected_dirs: list[Path] | None = None,
    protected_subtree_dirs: list[Path] | None = None,
    prune_media_dirs: list[Path] | None = None,
    preserve_media: bool = False,
) -> bool:
    """Stage exporter-written files and remove stale tracked files.

    Stages written_files, then (only on a full export that actually wrote pages)
    removes any tracked files under output_dir that are not in written_files
    (handles upstream deletions, renames, and moves). A full export that wrote
    nothing prunes nothing — see the Decision-1 note at the prune below. Returns
    True if a commit was made.

    ``is_full`` must be False for filtered / single-subtree / no-children
    exports: those write only part of the tree, so written_files covers only
    that subset. Pruning then would ``git rm`` the entire rest of the repo. A
    partial export is therefore strictly add-only.

    ``protected_dirs`` are protected page-EXACTLY: the page's own files (and its
    ``.media``) are spared from the stale prune, but a nested CHILD page is not.
    That is the right scope for archived pages whose precise on-disk target is
    known (M1-exact: a page moved OUT of an archived parent keeps only its own
    dir protected, so the moved-out copy is still reconciled).

    ``protected_subtree_dirs`` are protected RECURSIVELY (the dir and everything
    beneath it). That is the right scope for (a) a page SKIPPED this run because
    of a transient failure (body fetch / conversion raised) — we cannot know the
    true upstream state of its descendants, so we preserve the whole subtree and
    heal on the next successful export — and (b) a whole subtree intentionally
    omitted from this export, such as an existing ``_archived`` tree when archived
    pages were not requested.

    A moved page is handled as a plain delete + add: the reconciler drops the old
    path's markdown/.media and the write walk regenerates them at the new path,
    so the stale-file prune removes the old path and git's own rename detection
    makes history follow — no sidecar relocation or index patching needed (issue
    #17, Option B). The user's ``.workspace`` is never moved; it is preserved by
    the prune skip below and stays where the user put it.

    ``preserve_media`` must be True on a ``--no-media`` run (M1): that run writes
    no attachments, so written_files carries no ``.media`` paths and the prune
    would otherwise ``git rm`` every committed attachment even though they are
    still valid on disk. The committed media is left untouched; a later full
    export with media reconciles genuine attachment deletions.
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
        # DATA-SAFETY DECISION 1: prune ONLY when this run actually wrote live
        # pages. A full export that wrote nothing has no authority to reconcile
        # deletions — a transient/auth-failed empty response, or a v2 space that
        # returned its archived set but zero current pages, must NEVER git-rm a
        # whole committed export down to (near-)empty. We keep the prior export
        # and heal on the next successful run; archived-preservation sets alone
        # can never trigger a prune of live pages. (See the cli.py prune gate.)
        if written_files:
            _remove_stale_files(
                output_dir, written_files, protected_dirs or [],
                protected_subtree_dirs=protected_subtree_dirs or [],
                prune_media_dirs=prune_media_dirs or [],
                preserve_media=preserve_media,
            )
        # Restore any tracked file reconcile deleted from disk under a protected
        # dir, so the worktree matches the copy we kept in HEAD and the next run's
        # commit_local_changes can't stage the deletion (RF-B/M2). Always safe —
        # it only re-adds protected files, never deletes — so it runs even on a
        # write-less run (e.g. all pages skipped this run).
        _restore_protected_deletions(
            output_dir,
            protected_dirs or [],
            protected_subtree_dirs=protected_subtree_dirs or [],
        )

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
    protected_dirs: list[Path] | None = None,
    *,
    protected_subtree_dirs: list[Path] | None = None,
    prune_media_dirs: list[Path] | None = None,
    preserve_media: bool = False,
) -> None:
    """Remove tracked files that are no longer part of the export."""
    if not _has_commits(output_dir):
        return

    # protected_dirs: page-EXACT protection (the page's own files + .media, not a
    # nested child page) for archived pages whose precise target is known.
    # protected_subtree_dirs: RECURSIVE protection for skipped pages (transient
    # failure — preserve descendants too) and wholesale-omitted subtrees. Both
    # exclude their files from the stale prune; never treat output_dir itself as
    # protected.
    out_resolved = output_dir.resolve()
    out_absolute = output_dir.absolute()
    protected = {
        p.resolve() for p in (protected_dirs or [])
        if p.resolve() != out_resolved
    }
    protected_lexical = {
        p.absolute() for p in (protected_dirs or [])
        if p.absolute() != out_absolute
    }
    protected_subtrees = {
        p.resolve() for p in (protected_subtree_dirs or [])
        if p.resolve() != out_resolved
    }
    protected_subtrees_lexical = {
        p.absolute() for p in (protected_subtree_dirs or [])
        if p.absolute() != out_absolute
    }
    prune_media_owners = {
        p.parent.resolve() if p.name == MEDIA_DIR_NAME else p.resolve()
        for p in (prune_media_dirs or [])
        if p.resolve() != out_resolved
    }
    written_page_dirs = (
        _page_dirs_from_written_files(output_dir, written_files)
        if preserve_media else set()
    )
    written_media_dirs = (
        _media_owner_dirs_from_written_files(output_dir, written_files)
        if preserve_media else set()
    )

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
    # One shared fold definition with layout's collision keys (Q6).
    fold = nfc_casefold if _fs_is_case_insensitive(output_dir) else nfc
    written_keys = {fold(str(f.resolve())) for f in written_files}

    stale = []
    for rel_path in result.stdout.strip("\0").split("\0"):
        # Preserve user workspace directories across re-exports. On a move conex
        # leaves a committed .workspace tracked at its old path (it is never
        # relocated — issue #17, Option B); the user moves it themselves.
        parts = Path(rel_path).parts
        if WORKSPACE_DIR_NAME in parts:
            continue
        # M1: a --no-media run wrote no attachments; never prune committed
        # media just because it is absent from this run's written_files — BUT
        # only media that is still ON DISK. Media reconcile already deleted (a
        # moved page's old .media) must still be pruned, or it is left tracked
        # but missing and the export finishes dirty (RF-C).
        if preserve_media and MEDIA_DIR_NAME in parts and (output_dir / rel_path).is_file():
            owner = _media_owner_dir(output_dir, rel_path)
            if owner is not None and (
                (
                    owner in written_page_dirs
                    and owner not in written_media_dirs
                    and owner not in prune_media_owners
                )
                or owner in protected
                or any(owner.is_relative_to(root) for root in protected_subtrees)
            ):
                continue
        if _is_secret_config_relpath(rel_path):
            continue
        full_lexical = (output_dir / rel_path).absolute()
        full = full_lexical.resolve()
        if protected and _is_page_owned_path(full, protected):
            continue
        if protected_lexical and _is_page_owned_path(full_lexical, protected_lexical):
            continue
        if protected_subtrees and any(full.is_relative_to(d) for d in protected_subtrees):
            continue
        if protected_subtrees_lexical and any(
            full_lexical.is_relative_to(d) for d in protected_subtrees_lexical
        ):
            continue
        if fold(str(full)) not in written_keys:
            stale.append(rel_path)

    if stale:
        for rel_path in stale:
            path = output_dir / rel_path
            if MEDIA_DIR_NAME in Path(rel_path).parts and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
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


def _page_dirs_from_written_files(output_dir: Path, written_files: list[Path]) -> set[Path]:
    """Page directories that successfully produced output in this export."""
    out = output_dir.resolve()
    page_dirs: set[Path] = set()
    for path in written_files:
        try:
            rel = path.resolve().relative_to(out)
        except ValueError:
            continue
        parts = rel.parts
        if MEDIA_DIR_NAME in parts:
            idx = parts.index(MEDIA_DIR_NAME)
            page_dirs.add(out.joinpath(*parts[:idx]).resolve())
        elif path.suffix in {".md", ".html"}:
            page_dirs.add(path.resolve().parent)
    return page_dirs


def _media_owner_dirs_from_written_files(output_dir: Path, written_files: list[Path]) -> set[Path]:
    """Page directories whose current media files were explicitly written/kept."""
    out = output_dir.resolve()
    owners: set[Path] = set()
    for path in written_files:
        try:
            rel = path.resolve().relative_to(out)
        except ValueError:
            continue
        if MEDIA_DIR_NAME not in rel.parts:
            continue
        idx = rel.parts.index(MEDIA_DIR_NAME)
        owners.add(out.joinpath(*rel.parts[:idx]).resolve())
    return owners


def _media_owner_dir(output_dir: Path, rel_path: str) -> Path | None:
    parts = Path(rel_path).parts
    if MEDIA_DIR_NAME not in parts:
        return None
    idx = parts.index(MEDIA_DIR_NAME)
    return output_dir.resolve().joinpath(*parts[:idx]).resolve()


def _is_page_owned_path(path: Path, page_dirs: set[Path]) -> bool:
    """Whether ``path`` belongs to one protected page dir, not its child pages."""
    for page_dir in page_dirs:
        if path.parent == page_dir:
            return True
        try:
            rel = path.relative_to(page_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == MEDIA_DIR_NAME:
            return True
    return False


def _restore_protected_deletions(
    output_dir: Path,
    protected_dirs: list[Path],
    *,
    protected_subtree_dirs: list[Path] | None = None,
) -> None:
    """Restore tracked-but-deleted files under a protected dir from HEAD.

    When a moved page is skipped (transient body/convert failure), reconcile has
    already removed its old path from disk and the stale-prune keeps it tracked
    (protected) so the last-good committed copy stays in HEAD. That leaves the
    working tree with a tracked deletion, which the NEXT run's commit_local_changes
    (``git add -u``) would stage — quietly dropping the copy M2 protected. Restore
    those files so the worktree matches HEAD. Only restores genuinely-deleted
    files (``ls-files --deleted``), so a present (possibly user-edited) file is
    never reverted."""
    out = output_dir.resolve()
    out_absolute = output_dir.absolute()
    protected = {p.resolve() for p in protected_dirs if p.resolve() != out}
    protected_lexical = {
        p.absolute() for p in protected_dirs
        if p.absolute() != out_absolute
    }
    protected_subtrees = {
        p.resolve() for p in (protected_subtree_dirs or [])
        if p.resolve() != out
    }
    protected_subtrees_lexical = {
        p.absolute() for p in (protected_subtree_dirs or [])
        if p.absolute() != out_absolute
    }
    if (
        not protected
        and not protected_lexical
        and not protected_subtrees
        and not protected_subtrees_lexical
    ):
        return
    result = _run_git(output_dir, "ls-files", "--deleted", "-z", check=False)
    if result is None or not result.stdout.strip("\0"):
        return
    to_restore = []
    symlink_ancestors: dict[Path, list[str]] = {}
    for rel_path in result.stdout.strip("\0").split("\0"):
        if not rel_path:
            continue
        if _is_secret_config_relpath(rel_path):
            continue
        full_lexical = (output_dir / rel_path).absolute()
        full = full_lexical.resolve()
        if (
            _is_page_owned_path(full, protected)
            or _is_page_owned_path(full_lexical, protected_lexical)
            or any(full.is_relative_to(d) for d in protected_subtrees)
            or any(full_lexical.is_relative_to(d) for d in protected_subtrees_lexical)
        ):
            ancestor = _symlink_ancestor(output_dir, rel_path)
            if ancestor is not None:
                symlink_ancestors.setdefault(ancestor, []).append(rel_path)
            to_restore.append(rel_path)
    blocked: set[str] = set()
    for ancestor, rel_paths in symlink_ancestors.items():
        if not ancestor.is_symlink():
            continue
        try:
            ancestor.unlink()
        except OSError:
            blocked.update(rel_paths)
    if blocked:
        to_restore = [path for path in to_restore if path not in blocked]
    for batch in _chunked_paths(to_restore):
        _run_git(output_dir, "checkout", "HEAD", "--", *batch, check=False)


def _symlink_ancestor(output_dir: Path, rel_path: str) -> Path | None:
    root = output_dir.absolute()
    path = (output_dir / rel_path).absolute()
    try:
        rel = path.relative_to(root)
    except ValueError:
        return root
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return current
    return None

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
