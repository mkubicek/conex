"""Reconcile the on-disk export layout with the planned layout (issue #17).

Runs before the write walk on full exports. When a page is reparented or renamed
in Confluence its computed path changes, and its old exported directory would
become an orphan.

Design (issue #17, **Option B — "detect and warn"**, a deliberate product call;
see ``PR24-PR25-REFLECTION.md`` for why auto-carry was abandoned):

A full export REGENERATES every page's markdown from the API at the page's new
path, and git records a rename from a plain delete+add (no ``git mv`` is needed
for ``git log --follow``). So page markdown is **disposable** — it is dropped at
the old path, never moved. ``.media`` is disposable too: it re-downloads at the
new path (and git's rename detection links the dropped + re-added attachments by
content similarity), so it is also just dropped at the old path.

The only content that cannot be recomputed is the user's ``.workspace`` (prep
files conex never generates and never commits). conex **deliberately does NOT
relocate it.** Carrying a tree-derived sidecar across a move forces a perpetual
filesystem/git-index/plan reconciliation that proved to be an unbounded
edge-case stream for a rare event. Instead, on a move a non-empty ``.workspace``
is left untouched at the old path and a one-line note tells the user where the
page went, so they can move their prep files if they still want them. An empty
auto-created ``.workspace`` carries no data and is simply removed.

Pipeline (no holding area, no relocation, no quarantine, no git-index patching):
  0. Heal legacy duplicates (two ``.md`` with one ``page_id``): keep the
     canonical copy (at-target preferred, else highest version); drop the stale
     copy's markdown + ``.media``; warn if it holds a non-empty ``.workspace``.
  1. For each moved page: drop the stale markdown (rewritten fresh at the new
     path) and the disposable ``.media``; warn-and-leave a non-empty
     ``.workspace``, remove an empty one.
  2. Prune directories left empty by the moves/deletes (``rmdir`` only, so it can
     never delete user content).

State is derived entirely from frontmatter via
:func:`diff.scan_export_dir_grouped`; reconcile holds no transient on-disk state,
so re-running it is safe. Note the recovery model: reconcile drops a moved page's
old markdown *before* the write walk regenerates it at the new path, so a crash
between the two leaves the page absent on disk for that run. It is restored on the
next full export because the **write walk re-fetches it from the API** — not
because reconcile "heals" it (the dropped page has no frontmatter left to scan).
Markdown and ``.media`` are recomputable from Confluence; only the user's
``.workspace`` is never touched, so nothing irreplaceable is at risk in that window.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path, PurePosixPath

from confluence_export.diff import _NON_PAGE_DIRS, ExportedPage, scan_export_dir_grouped
from confluence_export.media import MEDIA_DIR_NAME, WORKSPACE_DIR_NAME


def _abs_target(output_dir: Path, target_dir: PurePosixPath) -> Path:
    return output_dir.joinpath(*target_dir.parts).resolve()


def _rel_str(path: Path, output_dir: Path) -> str:
    try:
        return str(path.relative_to(output_dir))
    except ValueError:
        return str(path)


def _same_dir(a: Path, b: Path) -> bool:
    """True when a and b are the same directory, treating a case-only difference
    on a case-insensitive filesystem as the same (so a case-only title change is
    a no-op, not a churn-every-run "move")."""
    if a == b:
        return True
    try:
        return a.exists() and b.exists() and a.samefile(b)
    except OSError:
        return False


def _choose_canonical(entries: list[ExportedPage], target_abs: Path) -> ExportedPage:
    """Pick the copy to keep. A copy already sitting at the plan target wins (so
    a stale lower-version duplicate elsewhere does not displace the live page and
    destroy its content); otherwise the highest-version, path-ordered copy."""
    return min(
        entries,
        key=lambda e: (
            e.file_path.parent.resolve() != target_abs,  # at-target first
            -e.version,                                   # then highest version
            str(e.file_path),                             # then deterministic path
        ),
    )


def _rmdir_if_empty(d: Path) -> None:
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass


def _remove_artifacts(md_path: Path) -> None:
    """Drop a page's disposable artifacts at a stale path: its markdown (and debug
    ``.html``) and its ``.media`` directory. Markdown is rewritten and ``.media``
    is re-downloaded fresh at the new path; in a git dir the staged delete + add
    is recorded as a rename by git's content-similarity detection."""
    for art in (md_path, md_path.with_suffix(".html")):
        try:
            if art.exists():
                art.unlink()
        except OSError:
            pass
    media = md_path.parent / MEDIA_DIR_NAME
    if media.is_dir():
        shutil.rmtree(media, ignore_errors=True)


def _warn_or_clear_workspace(page_dir: Path, note: str) -> None:
    """Handle a moved/duplicate page's ``.workspace`` at a stale path. A non-empty
    one is the user's own prep work: conex does NOT relocate it (see module
    docstring) — it is left in place and the user is told (``note``) where the
    page moved. An empty auto-created one carries no data and is removed so the
    old shell can be pruned."""
    ws = page_dir / WORKSPACE_DIR_NAME
    if not ws.is_dir():
        return
    if any(ws.iterdir()):
        print(note, file=sys.stderr)
    else:
        _rmdir_if_empty(ws)


def _execute_deletes(
    delete_copies: list[ExportedPage], output_dir: Path, canonical_dirs: set[Path]
) -> None:
    """Narrow-delete non-canonical duplicate copies (legacy collision/orphan
    state): remove the stale markdown and (unless it shares the canonical copy's
    directory) its ``.media``. A non-empty ``.workspace`` is left in place with a
    warning rather than destroying prep work."""
    for e in delete_copies:
        page_dir = e.file_path.parent
        try:
            if e.file_path.exists():
                e.file_path.unlink()
        except OSError:
            # Match every other delete in this module (all swallow OSError): a
            # read-only/locked stale copy must not abort the whole reconcile and
            # strand the move/prune phases that run after it.
            pass
        if page_dir.resolve() in canonical_dirs:
            continue  # shared with the canonical copy — never touch its media/workspace
        media = page_dir / MEDIA_DIR_NAME
        if media.is_dir():
            shutil.rmtree(media, ignore_errors=True)
        _warn_or_clear_workspace(
            page_dir,
            f"  Warning: orphaned workspace left at "
            f"'{_rel_str(page_dir, output_dir)}'; move it manually",
        )


def _vacate_moved_page(
    canonical: ExportedPage,
    target_abs: Path,
    output_dir: Path,
    *,
    media_will_redownload: bool,
) -> None:
    """Clear a moved page's old directory: drop the disposable markdown and
    ``.media`` (both regenerate at the new path, and git's rename detection
    follows them), and handle the irreplaceable ``.workspace`` per Option B —
    a non-empty one is left in place with a note pointing at the new path.

    ``media_will_redownload`` is False on a ``--no-media`` run: the old ``.media``
    is still dropped (it cannot be cleanly carried), but the page then has no
    attachments on disk until a full export with media restores them, so the drop
    is announced instead of silent."""
    src_dir = canonical.file_path.parent
    title = canonical.title or src_dir.name
    media = src_dir / MEDIA_DIR_NAME
    if not media_will_redownload and media.is_dir() and any(media.iterdir()):
        print(
            f'  Note: cached attachments for "{title}" at '
            f"'{_rel_str(media, output_dir)}' were not carried to the new path; "
            "re-run a full export with media to restore them.",
            file=sys.stderr,
        )
    note = (
        f'  Note: "{title}" moved to '
        f"'{_rel_str(target_abs, output_dir)}'; your prep files at "
        f"'{_rel_str(src_dir / WORKSPACE_DIR_NAME, output_dir)}' do not move "
        "automatically — relocate them if you still need them."
    )
    _remove_artifacts(canonical.file_path)
    _warn_or_clear_workspace(src_dir, note)


def _prune_empty_dirs(output_dir: Path, candidates: list[Path]) -> None:
    """Remove directories left empty by moves/deletes, bottom-up. Uses ``rmdir``
    only (which refuses non-empty directories), so it can never delete user
    content."""
    seen: set[Path] = set()
    for d in candidates:
        cur = d
        while cur != output_dir and output_dir in cur.parents:
            seen.add(cur)
            cur = cur.parent
    for d in sorted(seen, key=lambda p: len(p.parts), reverse=True):
        _rmdir_if_empty(d)


def _heal_folder_workspaces(output_dir: Path, move_sources: list[Path]) -> None:
    """Handle a legacy ``.workspace`` stranded in a FOLDER directory after its
    child pages moved out. Older exports created a ``.workspace`` under every
    node, including folders; a folder emits no frontmatter so it generates no
    move of its own. An empty one is legacy cruft and is removed; a non-empty one
    holds real user files and is left in place with a warning."""
    seen: set[Path] = set()
    for src in move_sources:
        cur = src.parent
        while cur != output_dir and output_dir in cur.parents:
            seen.add(cur)
            cur = cur.parent
    for d in sorted(seen, key=lambda p: len(p.parts), reverse=True):
        ws = d / WORKSPACE_DIR_NAME
        if not ws.is_dir():
            continue
        # A real page is markdown that is NOT inside a sidecar/internal dir.
        # Prune those dirs DURING traversal (P2) rather than rglob-ing the whole
        # subtree (incl. .media attachment trees) and filtering after: any .md
        # surviving the prune is a real page, so a stray .md under a sidecar dir
        # can't make an empty folder workspace look occupied and wrongly spare it.
        has_page = False
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x not in _NON_PAGE_DIRS]
            if any(f.endswith(".md") for f in filenames):
                has_page = True
                break
        if has_page:
            continue
        if not any(ws.iterdir()):
            try:
                ws.rmdir()
                d.rmdir()
            except OSError:
                pass
        else:
            print(
                f"  Warning: user .workspace left at '{_rel_str(ws, output_dir)}' "
                "(its folder was renamed/reparented); move it manually",
                file=sys.stderr,
            )


def reconcile(
    plan: dict[str, PurePosixPath],
    output_dir: Path,
    space_key: str,
    *,
    media_will_redownload: bool = True,
) -> None:
    """Bring the on-disk layout in line with the plan before the write walk:
    heal legacy duplicates, drop each moved page's stale disposable artifacts so
    the writer can regenerate them at the new path, warn about (without moving)
    any user ``.workspace``, and prune emptied shells.

    No git interaction and no relocation: page markdown and ``.media`` are
    rewritten / re-downloaded at the new path by the write walk, and git's own
    rename detection makes history follow the plain delete + add.
    ``media_will_redownload`` (False under ``--no-media``) only controls whether a
    dropped ``.media`` is announced — see :func:`_vacate_moved_page`.

    Known limitation: identity is keyed solely on frontmatter ``page_id``. Two
    ``.md`` carrying the *same* ``page_id`` are treated as duplicates of one page
    and the non-canonical copy's markdown/``.media`` are healed away — the intended
    recovery for legacy #11/#17 duplicates. A same-``page_id`` collision from
    hand-edited frontmatter or an in-tree copy of a page directory would therefore
    drop that copy; a genuinely live page self-heals because the write walk
    regenerates every in-plan page right after this runs, and a user ``.workspace``
    is never deleted, so nothing irreplaceable is at risk."""
    output_dir = output_dir.resolve()
    grouped = scan_export_dir_grouped(output_dir, space_key)

    # Collect duplicates + moves.
    delete_copies: list[ExportedPage] = []
    canonical_dirs: set[Path] = set()
    moves: list[tuple[Path, Path, ExportedPage]] = []  # (src_dir, target_abs, canonical)

    for page_id, entries in grouped.items():
        target_dir = plan.get(page_id)
        if target_dir is None:
            # Page absent from the plan (upstream deletion, or filtered/archived
            # out of this run): leave ALL on-disk copies untouched.
            continue
        target_abs = _abs_target(output_dir, target_dir)
        canonical = _choose_canonical(entries, target_abs)
        src_dir = canonical.file_path.parent.resolve()
        canonical_dirs.add(src_dir)
        delete_copies.extend(e for e in entries if e is not canonical)
        if not _same_dir(src_dir, target_abs):
            moves.append((src_dir, target_abs, canonical))

    _execute_deletes(delete_copies, output_dir, canonical_dirs)

    for _src_dir, target_abs, canonical in moves:
        _vacate_moved_page(
            canonical, target_abs, output_dir,
            media_will_redownload=media_will_redownload,
        )

    move_sources = [src for src, _, _ in moves]
    _prune_empty_dirs(output_dir, move_sources + [e.file_path.parent for e in delete_copies])
    _heal_folder_workspaces(output_dir, move_sources)
