"""build.py — snapshot + blobs + prev state → output tree + new state.

This is the heart of conex v2.  It is a deterministic, crash-safe function
that materialises the Confluence page tree onto the filesystem from
pre-fetched, immutable blob data.

Algorithm (single linear pass):

Step 1 — Layout
    Call plan_layout() over snapshot pages and folders to produce the
    collision-free path plan (page dirs, .md files, order).  The subtree
    and no_children options scope the plan; they are NOT content inputs.

Step 2 — Fingerprint
    For each page in plan.order compute a SHA-256 over exactly these inputs
    (in this order):
      version.number, CONVERTER_VERSION, include_html, media, render_drawio,
      sorted((att_id, att_version, planned_media_name)),
      body_blob_digest,
      sorted(derived png digests actually used for this page)
    subtree/no_children must NOT enter the fingerprint.

Step 3 — Skip
    A page is skipped (and its prev PageState carried forward verbatim) when
    ALL of:
      - prev state exists for the page id
      - prev dir == planned dir AND prev file == planned file
      - prev fingerprint == computed fingerprint
      - the .md file exists on disk
    Skipped pages count into BuildResult.skipped.

Step 4 — Move
    A page has moved when its id appears in prev but the planned dir differs
    from the prev dir.  Protocol:
      1. Write all new artifacts for the page first (step 5).
      2. Carry .workspace: os.rename the non-empty old .workspace/ to the
         new dir.  If the target already has a .workspace/, rename to
         .workspace-from-<old-dir-leaf> instead (warn).  On EXDEV,
         copytree+rmtree.  Idempotent: old .workspace absent or already
         at the target = move done silently.
      3. Delete old recorded artifacts (.md, .html, old .media/ files).
         Old-media cleanup uses per-file set-difference (CANDIDATES − KEEP):
         CANDIDATES = prev PageState.attachments file paths; KEEP = all paths
         written this run (casefolded).  No whole-dir deletes.
      4. rmdir emptied parent dirs bottom-up (never remove non-empty).
      5. Record (old_dir, new_dir) in moved.
    Crash-mid-move: next build re-derives the same target; idempotent.

Step 5 — Write
    Render markdown from the body blob via conex.convert (+ frontmatter).
    Write via .conex/tmp + os.replace (I4, I6).
    --include-html writes the raw storage body alongside; path recorded in
    PageState.html.
    Build ONE AttachmentNamePlan per page (drives both disk names and
    ctx.media); ctx.media_available tracks filenames confirmed this run.
    Materialise .media/ from blobs with mtime = attachment version.created_at
    -> epoch.  On parse failure leave mtime unset.  (DELIBERATE DIVERGENCE:
    v1 copy2 preserved source mtime; v2 stamps the attachment's version time.)
    opts.media == False: do not materialise, do not delete existing media,
    carry prev attachment states.
    snapshot.attachments_complete is False (in-place): never delete an existing
    .media/ file; only add newly fetched attachments.
    snapshot.attachments_complete is False + page MOVES (strategy b): any
    prev-recorded media file absent from the partial listing is os.renamed from
    the old .media/ dir into the new .media/ dir before the deferred cleanup
    runs.  This preserves bytes and ensures state/disk consistency (the state
    records the file at the new location, and it exists there).  On EXDEV,
    falls back to shutil.copy2 + unlink.  On a case-insensitive FS where old
    and new .media/ dirs are the same inode (case-only rename), the rename is
    skipped (files are already in place).
    drawio preview-first: use the .png sibling when its version.created_at
    TIMESTAMP >= xml attachment's; else batch-render misses ONCE per build
    via drawio.find_drawio_pairs + drawio.render_batch (mocked in tests).

Step 6 — Prune
    For each prev page id NOT in plan:
    - I2 zero-pages guard: if plan is empty and prev was non-empty → skip ALL
      pruning AND blob GC, warn, return prev state unchanged.
    - I3 archived preservation: skip pruning if prev status == "archived" and
      not snapshot.include_archived.
    - subtree scope: when opts.subtree is set, only prune pages whose prev
      dir is inside the resolved subtree_dir from layout.
    - Delete page recorded artifacts (.md, .html, .media/); warn + leave any
      non-empty .workspace/.  rmdir emptied parents bottom-up.
    - Prune folder dirs from prev.folders not in new plan (rmdir iff empty).

Step 7 — State
    Build ExportState from:
      skipped carry-forwards + written pages + I2/I3 survivors.
      folders from plan.
      converter_version = CONVERTER_VERSION.
    Save atomically ONCE at the end (I6).

Step 8 — Blob GC
    keep = all body_blobs ∪ attachment_blobs ∪ derived_blobs values from the
    current snapshot ∪ every blob digest in the NEW state (incl. carry-over
    attachment states).
    blobs.gc(keep) runs LAST, only on non-guarded runs.

Crash-safety argument:
    Every file lands via os.replace from .conex/tmp (I4).  State is written
    once, last (I6).  A crash at any point leaves the previous state.json
    intact; the next run re-derives the same target layout deterministically
    and converges.  The blob store is append-only until GC, which runs after a
    successful state save.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from conex.convert import (
    CONVERTER_VERSION,
    ConvertContext,
    MediaRefs,
    build_frontmatter,
    convert_page,
)
from conex.errors import StateError
from conex.layout import plan_layout
from conex.models import Attachment, Page
from conex.paths import plan_attachment_names, safe_attachment_name
from conex.store.blobs import BlobStore
from conex.store.state import (
    AttachmentState,
    ExportState,
    PageState,
    Snapshot,
)


# ---------------------------------------------------------------------------
# Public option/result types
# ---------------------------------------------------------------------------


@dataclass
class BuildOptions:
    """Options controlling a single build run.

    Attributes:
        include_html:   Write the raw storage body alongside the .md file.
        media:          Materialise attachment files into .media/.
        render_drawio:  Attempt batch drawio rendering for diagram attachments.
        author_lookup:  Allow live author lookups through the API.
        subtree:        Restrict to the named subtree (slash-separated titles).
        no_children:    When subtree is set, include only the root node.
        site_url:       Configured site URL, used to build the frontmatter `url`
                        and resolve attachment/page links.  "" leaves url empty.
    """

    include_html: bool = False
    media: bool = True
    render_drawio: bool = True
    author_lookup: bool = True
    subtree: str | None = None
    no_children: bool = False
    site_url: str = ""


@dataclass
class BuildResult:
    """Summary of what a build run did to the output tree.

    Attributes:
        written:  Absolute paths written or updated this run.
        deleted:  Paths removed this run (for git staging).
        skipped:  Count of unchanged pages.
        moved:    (old_dir_relpath, new_dir_relpath) pairs.
        warnings: Human-readable warning strings.
    """

    written: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    skipped: int = 0
    moved: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def _fingerprint(
    page: Page,
    body_blob_digest: str,
    attachments: list[Attachment],
    name_plan: "AttachmentNamePlan",  # type: ignore[name-defined]
    derived_png_digests: list[str],
    opts: BuildOptions,
) -> str:
    """Compute the build fingerprint for a single page.

    Inputs (in this exact order):
      version.number, CONVERTER_VERSION, include_html, media, render_drawio,
      site_url, sorted((att_id, att_version, planned_media_name)),
      body_blob_digest,
      sorted(derived png digests actually used)

    site_url is a CONTENT input (it builds the frontmatter ``url:``), so a
    changed site URL must invalidate the skip and rewrite the page.
    subtree and no_children are scope, not content — they must NOT appear here.
    """
    h = hashlib.sha256()

    def _add(value: object) -> None:
        h.update(repr(value).encode())

    _add(page.version.number)
    _add(CONVERTER_VERSION)
    _add(opts.include_html)
    _add(opts.media)
    _add(opts.render_drawio)
    _add(opts.site_url)

    att_tuples = []
    for att in attachments:
        planned_name = name_plan.by_id.get(att.id, "")
        att_tuples.append((att.id, att.version.number, planned_name))
    att_tuples.sort()
    _add(att_tuples)

    _add(body_blob_digest)
    _add(sorted(derived_png_digests))

    return h.hexdigest()


# ---------------------------------------------------------------------------
# Mtime helper
# ---------------------------------------------------------------------------


def _parse_mtime(created_at: str) -> float | None:
    """Parse an ISO 8601 timestamp to a POSIX epoch float.

    Returns None on any parse failure (deliberate: leave mtime unset).
    Python 3.11+ handles trailing 'Z' natively.
    """
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Directory cleanup helpers
# ---------------------------------------------------------------------------


def _rmdir_empty_parents(path: Path, stop_at: Path) -> None:
    """Remove *path* and any empty ancestor dirs up to (but not including) stop_at."""
    current = path
    while current != stop_at and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _guarded_delete_file(
    p: Path, keep: set[str], result: BuildResult
) -> None:
    """Unlink *p* only when its casefold is not in *keep*; record in result.deleted.

    The *keep* set contains the casefolded absolute paths of every file written,
    materialised, renamed-in, or carried during this build run.  Any path in
    *keep* must never be deleted, regardless of which deletion call site triggers.
    """
    if str(p).casefold() in keep:
        return
    if p.exists() and p.is_file():
        p.unlink()
        result.deleted.append(p)


def _guarded_delete_dir_tree(
    d: Path, keep: set[str], result: BuildResult
) -> None:
    """Remove files under *d* that are not in *keep*; rmdir iff the tree becomes empty.

    Iterates per-file through the KEEP guard before deleting anything.  The
    directory itself (and all ancestor dirs) are removed only when completely
    empty after the file deletions.  This replaces the old ``_delete_dir_tree``
    which called ``shutil.rmtree`` unconditionally (DEFECT C root cause).
    """
    if not d.exists():
        return
    for child in sorted(d.rglob("*")):
        if child.is_file():
            _guarded_delete_file(child, keep, result)
    # Remove the now-possibly-empty directory and any empty ancestors.
    # _rmdir_empty_parents stops at the first non-empty dir.


def _delete_artifact(p: Path, result: BuildResult) -> None:
    """Unlink a regular file if it exists; record in result.deleted.

    INTERNAL USE ONLY — call this only from the stale-media inline cleanup
    path (inside the write loop, before the global KEEP set is built) where
    the caller has already verified the file is safe to delete.  All other
    deletion sites must use _guarded_delete_file.
    """
    if p.exists() and p.is_file():
        p.unlink()
        result.deleted.append(p)


# ---------------------------------------------------------------------------
# Workspace carry helpers
# ---------------------------------------------------------------------------


def _carry_workspace(old_dir: Path, new_dir: Path, result: BuildResult) -> None:
    """Carry the .workspace subdirectory from old_dir to new_dir.

    Protocol:
    - old .workspace absent or empty → nothing to do (idempotent).
    - target .workspace absent → os.rename (or copytree+rmtree on EXDEV).
    - target .workspace present → rename to .workspace-from-<old-dir-leaf>, warn.
    """
    old_ws = old_dir / ".workspace"
    if not old_ws.exists():
        return
    if not any(old_ws.iterdir()):
        return  # empty — nothing to carry

    new_ws = new_dir / ".workspace"

    if new_ws.exists():
        leaf = old_dir.name
        collision_name = f".workspace-from-{leaf}"
        target = new_dir / collision_name
        msg = (
            f"workspace collision during move: both {old_ws} and {new_ws} exist; "
            f"renaming incoming to {target}"
        )
        result.warnings.append(msg)
        warnings.warn(msg, stacklevel=5)
        try:
            os.rename(old_ws, target)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                shutil.copytree(old_ws, target)
                shutil.rmtree(old_ws, ignore_errors=True)
            else:
                raise
        return

    try:
        os.rename(old_ws, new_ws)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            shutil.copytree(old_ws, new_ws)
            shutil.rmtree(old_ws, ignore_errors=True)
        else:
            raise


# ---------------------------------------------------------------------------
# drawio helpers (imported lazily to avoid requiring the module at import time)
# ---------------------------------------------------------------------------


def _pair_xml(pair: object) -> "Attachment":
    """Extract the xml attachment from a DrawioPair or (xml, png) tuple."""
    if hasattr(pair, "xml"):
        return pair.xml  # type: ignore[union-attr]
    return pair[0]  # type: ignore[index]


def _pair_png(pair: object) -> "Attachment | None":
    """Extract the png attachment from a DrawioPair or (xml, png) tuple."""
    if hasattr(pair, "png"):
        return pair.png  # type: ignore[union-attr]
    return pair[1]  # type: ignore[index]


def _run_drawio_render(
    snapshot: Snapshot,
    blobs: BlobStore,
    pages: list[Page],
    opts: BuildOptions,
) -> dict[str, str]:
    """Batch-render drawio XML attachments whose previews are stale or absent.

    Returns a dict mapping xml attachment name → rendered png blob digest.
    If drawio.py is unavailable or render_drawio is False, returns {}.
    Only renders pages whose PNG preview is STALE (xml newer than png).
    """
    if not opts.render_drawio:
        return {}
    try:
        from conex import drawio as _drawio  # type: ignore[import]
    except ImportError:
        return {}

    all_atts: list[Attachment] = []
    for page in pages:
        all_atts.extend(snapshot.attachments.get(page.id, []))

    pairs = _drawio.find_drawio_pairs(all_atts)
    if not pairs:
        return {}

    # Build xml_blobs dict only for pairs that need rendering (stale preview).
    xml_blobs: dict[str, str] = {}
    for pair in pairs:
        xml_att = _pair_xml(pair)
        png_att = _pair_png(pair)

        # Preview-first: if png exists and is fresh, skip rendering.
        if png_att is not None:
            xml_ts = _parse_mtime(xml_att.version.created_at) or 0.0
            png_ts = _parse_mtime(png_att.version.created_at) or 0.0
            if png_ts >= xml_ts:
                continue  # Preview is fresh — no render needed.

        att_key = f"{xml_att.id}@{xml_att.version.number}"
        digest = snapshot.attachment_blobs.get(att_key)
        if digest:
            # Key by CONTENT digest, not title: two pages can own diagrams with
            # the same filename but different content — a title-keyed map would
            # collapse them and embed one page's render into the other (BL-3).
            xml_blobs[digest] = digest

    if not xml_blobs:
        return {}

    try:
        return _drawio.render_batch(xml_blobs, blobs)
    except Exception as exc:
        warnings.warn(f"drawio render failed: {exc}", stacklevel=3)
        return {}


def _dir_in_subtree(dir_str: str, subtree_prefix: str, no_children: bool) -> bool:
    """True if a recorded dir falls within the in-scope subtree for pruning.

    With ``no_children`` only the subtree ROOT itself is in scope; otherwise the
    root and all its descendants are.  Path-boundary aware: a prefix-colliding
    sibling (``Root-One-2`` vs ``Root-One``) is never wrongly matched.
    """
    if no_children:
        return dir_str == subtree_prefix
    return dir_str == subtree_prefix or dir_str.startswith(subtree_prefix + "/")


def _space_mismatch(prev: "ExportState", snapshot: "Snapshot") -> bool:
    """True if ``prev`` describes a DIFFERENT space than ``snapshot``.

    Compared by id first (stable across renames), then by key.  An empty
    identity on either side is treated as 'unknown' — never a mismatch — so
    legacy or hand-written state that never recorded a space is not falsely
    rejected.  A real mismatch means the output directory holds another space's
    export; reconciling would delete it as the complement of ownership.
    """
    pid, sid = prev.space_id.strip(), snapshot.space.id.strip()
    if pid and sid:
        return pid != sid
    pk, sk = prev.space_key.strip(), snapshot.space.key.strip()
    if pk and sk:
        return pk != sk
    return False


def _assert_writable_dir(root_real: Path, dir_abs: Path) -> None:
    """Refuse to write into a directory reached through a symlink (S1 posture).

    conex never creates symlinks in an export tree, so a symlinked page/media
    directory — or any symlinked ancestor that resolves outside the root — is a
    sign of a planted redirect that would carry conex writes (and, worse, the
    reconciliation's deletes) outside the export.  Mirrors v1's refusal of
    symlinked page/media dirs, generalized to the multi-segment path via a
    realpath containment check.  Raises before any mkdir/replace, so a tampered
    tree aborts the build rather than being written or deleted through.
    """
    if dir_abs.is_symlink():
        raise StateError(f"refusing to write through symlinked path: {dir_abs}")
    try:
        resolved = dir_abs.resolve()
    except OSError:
        return
    try:
        resolved.relative_to(root_real)
    except ValueError as exc:
        raise StateError(
            f"refusing to write outside the export root via a symlinked "
            f"ancestor: {dir_abs} resolves to {resolved}"
        ) from exc


def _page_owned_paths(root: Path, ps: "PageState") -> list[Path]:
    """Absolute paths a single PageState records as conex-owned output."""
    out: list[Path] = []
    if ps.file:
        out.append(root / ps.file)
    if ps.html:
        out.append(root / ps.html)
    media = root / ps.dir / ".media"
    for att in ps.attachments.values():
        if att.file:
            out.append(media / att.file)
    for fname in ps.rendered_media:
        if fname:
            out.append(media / fname)
    return out


def _reconcile_deletions(
    root: Path,
    prev: "ExportState",
    new_pages: dict[str, "PageState"],
    new_folders: dict[str, str],
    result: "BuildResult",
) -> None:
    """Delete every artifact the PREV state owned that the NEW state no longer
    owns — deletion as the complement of ownership.

    This single set difference replaces the former move-cleanup + prune passes.
    Because EVERY desired path is materialized before this runs, the cases that
    were special and bug-prone collapse:
    - **Move**: the page's old paths leave the desired set (its new paths are in
      it) → deleted; no file is shuffled.
    - **Title-swap / N-page rotation**: a path another page now owns stays in
      the desired set → never deleted; each page already materialized its own
      content there from the blob store.
    - **Prune / reparent-out / archived / subtree-scope**: a surviving page is
      carried into ``new_pages`` (so its paths are desired); a genuinely-removed
      page is not, so its recorded artifacts are deleted.

    Invariants are structural: ``.workspace`` and any unrecorded user file are
    never in the prev-owned set, so they cannot be deleted; dirs are removed only
    when empty.
    """
    desired: set[str] = {
        str(p).casefold()
        for ps in new_pages.values()
        for p in _page_owned_paths(root, ps)
    }

    dirs_to_prune: set[Path] = set()
    for pid, ps in prev.pages.items():
        # Delete recorded prev artifacts the new state no longer owns.
        for p in _page_owned_paths(root, ps):
            _guarded_delete_file(p, desired, result)

        page_dir = root / ps.dir
        if pid not in new_pages:
            # Genuinely pruned: also clear UNRECORDED media (e.g. derived drawio
            # PNGs) under the old .media/, guarded so a swap partner's media at
            # the same path survives.  Leave a non-empty .workspace + warn.
            media_dir = page_dir / ".media"
            if media_dir.exists():
                _guarded_delete_dir_tree(media_dir, desired, result)
            ws = page_dir / ".workspace"
            if ws.exists() and any(ws.iterdir()):
                msg = (
                    f"prune: non-empty .workspace left at {ws} "
                    f"(page {pid!r} removed)"
                )
                result.warnings.append(msg)
                warnings.warn(msg, stacklevel=2)

        # The old dir of a moved page, or a pruned page's dir, may now be empty.
        if pid not in new_pages or new_pages[pid].dir != ps.dir:
            dirs_to_prune.add(page_dir / ".media")
            dirs_to_prune.add(page_dir)

    # rmdir emptied dirs deepest-first (never removes a non-empty dir).
    for d in sorted(dirs_to_prune, key=lambda p: len(p.parts), reverse=True):
        _rmdir_empty_parents(d, root)

    # Folder dirs the new state no longer owns (out-of-subtree folders were
    # carried into new_folders, so a scoped run never touches them): rmdir iff
    # empty; a non-empty folder dir is user content → leave + warn.
    for fid, fdir in (prev.folders or {}).items():
        if fid in new_folders:
            continue
        fdir_abs = root / fdir
        if fdir_abs.exists():
            try:
                fdir_abs.rmdir()
            except OSError:
                msg = f"prune: folder dir {fdir_abs} is non-empty; leaving"
                result.warnings.append(msg)
                warnings.warn(msg, stacklevel=2)


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------


def build(
    root: Path,
    snapshot: Snapshot,
    blobs: BlobStore,
    prev: ExportState | None,
    opts: BuildOptions,
    api: object = None,
) -> tuple[BuildResult, ExportState]:
    """Materialise the Confluence export tree from snapshot + blobs.

    Contract:
    - Deterministic: the same inputs always produce the same output tree.
    - Crash-safe: every file lands via os.replace; state saves last (I6).
    - I1: only deletes paths recorded in state under a page id.
    - I2: empty plan + non-empty prev → skip all pruning; warn; return prev.
    - I3: prev archived pages not in snapshot.include_archived are preserved.
    - I4: all temp files under .conex/tmp/.
    - I6: state written atomically, once, at end of successful run.
    - I7: all paths through sanitization + resolve_within before FS ops.

    Parameters:
        root:     Export root directory.
        snapshot: The current fetch snapshot (body_storage is "" on pages).
        blobs:    The blob store.
        prev:     Previous ExportState (None on first run).
        opts:     Build options.
        api:      Optional live ConfluenceAPI for author lookups.

    Returns:
        (BuildResult, ExportState)
    """
    result = BuildResult()
    conex_dir = root / ".conex"
    tmp_dir = conex_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    root_real = root.resolve()

    # -----------------------------------------------------------------------
    # Step 1 — Layout
    # -----------------------------------------------------------------------
    plan = plan_layout(
        snapshot.space,
        snapshot.pages,
        snapshot.folders,
        subtree=opts.subtree,
        no_children=opts.no_children,
    )

    # -----------------------------------------------------------------------
    # Step 2 — Fingerprints + drawio (batch, once per build)
    # -----------------------------------------------------------------------

    # Build resolver for author display names.
    # api=None (offline) → read from snapshot.users only.
    _user_cache: dict[str, str] = dict(snapshot.users)
    _api = api  # may be None

    def resolve_user(account_id: str) -> str:
        if not account_id:
            return ""
        if account_id in _user_cache:
            return _user_cache[account_id]
        if _api is not None and opts.author_lookup:
            try:
                name = _api.get_user_display_name(account_id)  # type: ignore[union-attr]
                _user_cache[account_id] = name
                return name
            except Exception:
                pass
        return ""

    # Collect pages in plan order.
    plan_pages = [p for p in snapshot.pages if p.id in plan.dirs]

    # Run drawio render once for all pages.
    drawio_results: dict[str, str] = {}
    if opts.render_drawio and plan_pages:
        drawio_results = _run_drawio_render(snapshot, blobs, plan_pages, opts)

    # All freshly-rendered PNG digests produced this run — they are not yet in
    # snapshot.derived_blobs so they must be added to the GC keep set explicitly.
    freshly_rendered_digests: set[str] = set(drawio_results.values())

    # Compute per-page name plans and fingerprints.
    name_plans: dict[str, object] = {}  # page_id -> AttachmentNamePlan
    fingerprints: dict[str, str] = {}   # page_id -> digest

    for page in plan_pages:
        atts = snapshot.attachments.get(page.id, [])
        np = plan_attachment_names(atts)
        name_plans[page.id] = np

        body_digest = snapshot.body_blobs.get(page.id, "")

        # Collect derived png digests that are ACTUALLY used for this page.
        used_png_digests: list[str] = []
        for xml_att in atts:
            if not xml_att.title.lower().endswith(".drawio") and not xml_att.title.lower().endswith(".xml"):
                continue
            att_key = f"{xml_att.id}@{xml_att.version.number}"
            xml_digest = snapshot.attachment_blobs.get(att_key)
            if xml_digest:
                derived_key = f"drawio-png:v{_get_drawio_render_version()}:{xml_digest}"
                d = snapshot.derived_blobs.get(derived_key) or drawio_results.get(xml_digest)
                if d:
                    used_png_digests.append(d)

        fingerprints[page.id] = _fingerprint(
            page, body_digest, atts, np, used_png_digests, opts
        )

    # -----------------------------------------------------------------------
    # Step 3 — Skip check (determine which pages need writing)
    # -----------------------------------------------------------------------
    skip_ids: set[str] = set()
    write_ids: set[str] = set()

    for page in plan_pages:
        pid = page.id
        planned_dir = str(plan.dirs[pid])
        planned_file = str(plan.files[pid])

        if prev is not None and pid in prev.pages:
            ps = prev.pages[pid]
            if (
                ps.dir == planned_dir
                and ps.file == planned_file
                and ps.fingerprint == fingerprints[pid]
                and (root / ps.file).exists()
            ):
                skip_ids.add(pid)
                result.skipped += 1
                continue

        write_ids.add(pid)

    # -----------------------------------------------------------------------
    # Steps 4 & 5 — Move + Write (in plan order for deterministic output)
    # -----------------------------------------------------------------------
    new_page_states: dict[str, PageState] = {}

    # Carry skipped states forward.
    for page in plan_pages:
        if page.id in skip_ids and prev is not None:
            new_page_states[page.id] = prev.pages[page.id]

    for page in plan_pages:
        pid = page.id
        if pid not in write_ids:
            continue

        planned_dir_rel = plan.dirs[pid]
        planned_file_rel = plan.files[pid]
        planned_dir_abs = root / str(planned_dir_rel)
        planned_file_abs = root / str(planned_file_rel)

        # S1: refuse a symlinked/escaping page dir before the FIRST write into
        # it (the media phase below may mkdir(parents=True) through it).
        _assert_writable_dir(root_real, planned_dir_abs)

        is_move = (
            prev is not None
            and pid in prev.pages
            and prev.pages[pid].dir != str(planned_dir_rel)
        )

        # --- Step 5: Write new artifacts ---
        atts = snapshot.attachments.get(pid, [])
        np = name_plans[pid]

        # drawio preview-first resolution per attachment.
        rendered_drawio: dict[str, str] = {}
        try:
            from conex import drawio as _drawio  # type: ignore[import]
            pairs = _drawio.find_drawio_pairs(atts)
        except ImportError:
            pairs = []

        for pair in pairs:
            xml_att = _pair_xml(pair)
            png_att = _pair_png(pair)
            xml_digest = snapshot.attachment_blobs.get(f"{xml_att.id}@{xml_att.version.number}")
            use_preview = False
            if png_att is not None:
                # Use preview when png.created_at >= xml.created_at (timestamps).
                xml_ts = _parse_mtime(xml_att.version.created_at) or 0.0
                png_ts = _parse_mtime(png_att.version.created_at) or 0.0
                use_preview = png_ts >= xml_ts

            if use_preview and png_att is not None:
                png_key = f"{png_att.id}@{png_att.version.number}"
                png_digest = snapshot.attachment_blobs.get(png_key)
                if png_digest:
                    rendered_drawio[xml_att.title] = np.by_id.get(png_att.id, png_att.title)
            elif xml_digest and xml_digest in drawio_results:
                # Batch-rendered result (keyed by content digest, BL-3): derive a
                # collision-checked filename for the rendered PNG.  It is not an
                # attachment, so use the SANITIZED xml stem + ".png" (BL-4: the
                # raw title may contain path separators / '..'), appending
                # "-rendered" to avoid colliding with a real attachment name.
                stem = xml_att.title.rsplit(".", 1)[0]
                rendered_png_filename = safe_attachment_name(f"{stem}.png")
                if any(
                    rendered_png_filename == np.by_id.get(a.id, a.title)
                    for a in atts
                    if a.id != getattr(_pair_png(pair) if pair else None, "id", None)
                ):
                    rendered_png_filename = safe_attachment_name(f"{stem}-rendered.png")
                rendered_drawio[xml_att.title] = rendered_png_filename

        # Determine media availability.
        # rendered_png_digests_to_materialize: maps filename → blob digest for
        # batch-rendered PNGs that need materialisation during the media phase.
        rendered_png_digests_to_materialize: dict[str, str] = {}
        for pair in pairs:
            xml_att = _pair_xml(pair)
            xml_digest = snapshot.attachment_blobs.get(f"{xml_att.id}@{xml_att.version.number}")
            if xml_digest and xml_digest in drawio_results and xml_att.title in rendered_drawio:
                fname = rendered_drawio[xml_att.title]
                rendered_png_digests_to_materialize[fname] = drawio_results[xml_digest]

        media_available: set[str] = set()
        att_states: dict[str, AttachmentState] = {}

        if not opts.media:
            # Carry prev attachment states.
            if prev is not None and pid in prev.pages:
                att_states = dict(prev.pages[pid].attachments)
        else:
            # Ensure the media dir exists.  S1: a .media symlink can be planted
            # independently of a clean page dir, so check it too.
            media_dir = planned_dir_abs / ".media"
            _assert_writable_dir(root_real, media_dir)

            for att in atts:
                att_key = f"{att.id}@{att.version.number}"
                att_digest = snapshot.attachment_blobs.get(att_key, "")
                planned_name = np.by_id.get(att.id, "")
                if not planned_name:
                    planned_name = att.title

                if att_digest and planned_name:
                    dest = media_dir / planned_name
                    mtime = _parse_mtime(att.version.created_at)
                    media_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        blobs.materialize(att_digest, dest, mtime=mtime)
                        media_available.add(planned_name)
                        result.written.append(dest)
                    except Exception as exc:
                        msg = f"failed to materialise attachment {att.title!r}: {exc}"
                        result.warnings.append(msg)
                        warnings.warn(msg, stacklevel=2)
                        att_states[att.id] = AttachmentState(
                            version=att.version.number,
                            file=planned_name,
                            blob=att_digest,
                            size=att.file_size,
                        )
                        continue

                    att_states[att.id] = AttachmentState(
                        version=att.version.number,
                        file=planned_name,
                        blob=att_digest,
                        size=att.file_size,
                    )
                else:
                    # Download failed or no blob.
                    att_states[att.id] = AttachmentState(
                        version=att.version.number,
                        file=planned_name,
                        blob=att_digest,
                        size=att.file_size,
                    )

            # Materialise batch-rendered drawio PNGs that are not regular
            # attachments.  The rendered blob must reach disk and enter
            # media_available so convert can emit an <img> reference, and its
            # digest must be tracked so GC cannot delete it on the same build.
            for png_filename, rendered_digest in rendered_png_digests_to_materialize.items():
                dest = media_dir / png_filename
                media_dir.mkdir(parents=True, exist_ok=True)
                try:
                    blobs.materialize(rendered_digest, dest)
                    media_available.add(png_filename)
                    result.written.append(dest)
                except Exception as exc:
                    msg = f"failed to materialise rendered drawio PNG {png_filename!r}: {exc}"
                    result.warnings.append(msg)
                    warnings.warn(msg, stacklevel=2)

            # Handle attachments_complete=False: carry prev media, never delete.
            if not snapshot.attachments_complete and prev is not None and pid in prev.pages:
                for prev_att_id, prev_att_state in prev.pages[pid].attachments.items():
                    if prev_att_id not in att_states:
                        att_states[prev_att_id] = prev_att_state
                        if prev_att_state.file:
                            media_available.add(prev_att_state.file)
            elif (
                snapshot.attachments_complete
                and not is_move
                and prev is not None
                and pid in prev.pages
            ):
                # On an IN-PLACE complete rewrite, remove stale .media files that
                # are no longer in the current attachment set (I1: conex only
                # deletes what it recorded in state).  Drive deletions off prev
                # PageState, never off a raw listdir.
                #
                # Skipped for a MOVE: here media_dir is the NEW dir but the prev
                # records the OLD-dir files, so unlinking media_dir/<old name>
                # would delete a swap partner's media at the new location (BL-
                # INLINE-STALE-NEWDIR).  A move's old-dir stale media is handled
                # by the deferred, _keep-guarded per-file cleanup instead.
                current_files = {s.file for s in att_states.values() if s.file}
                # Also keep rendered drawio PNG filenames.
                current_files.update(rendered_png_digests_to_materialize.keys())
                for prev_att_state in prev.pages[pid].attachments.values():
                    if prev_att_state.file and prev_att_state.file not in current_files:
                        stale = media_dir / prev_att_state.file
                        if stale.exists() and stale.is_file():
                            stale.unlink()
                            result.deleted.append(stale)

        # Build conversion context.  site_url comes from opts (threaded from the
        # resolved config) so frontmatter emits a real page URL; "" leaves it
        # empty (e.g. offline --cached with no configured site).
        media_refs = MediaRefs(np)
        ctx = ConvertContext(
            page=page,
            space=snapshot.space,
            site_url=opts.site_url,
            attachments=atts,
            media=media_refs,
            rendered_drawio=rendered_drawio,
            resolve_user=resolve_user,
            media_enabled=opts.media,
            media_available=media_available,
        )

        # Read body from blob.
        body_digest = snapshot.body_blobs.get(pid, "")
        if body_digest and blobs.has(body_digest):
            body_storage = blobs.read_bytes(body_digest).decode("utf-8", errors="replace")
        else:
            body_storage = ""

        # Render markdown.  (planned_dir_abs was symlink-checked at the top of
        # this iteration, before the media phase's first write into it.)
        planned_dir_abs.mkdir(parents=True, exist_ok=True)
        human_path = str(planned_dir_rel)
        frontmatter = build_frontmatter(
            page, snapshot.space, human_path, ctx.site_url, attachments=atts
        )
        md_body = convert_page(body_storage, ctx)
        md_content = frontmatter + md_body

        # Write .md via tmp + os.replace (I4).
        md_tmp = tmp_dir / f"page-{pid}.md.tmp"
        md_tmp.write_text(md_content, encoding="utf-8")
        os.replace(md_tmp, planned_file_abs)
        result.written.append(planned_file_abs)

        # Write --include-html artifact.
        html_rel = ""
        if opts.include_html:
            html_path_abs = planned_dir_abs / f"{planned_file_abs.stem}.html"
            html_rel = str(planned_dir_rel / f"{planned_file_abs.stem}.html")
            html_tmp = tmp_dir / f"page-{pid}.html.tmp"
            html_tmp.write_text(body_storage, encoding="utf-8")
            os.replace(html_tmp, html_path_abs)
            result.written.append(html_path_abs)

        # --- Step 4: Move .workspace after new artifacts are landed ---
        if is_move:
            assert prev is not None
            old_dir_rel = prev.pages[pid].dir
            old_dir_abs = root / old_dir_rel

            # Carry .workspace immediately (safe: .workspace is never in
            # result.written, so no collision with deferred deletion).
            if old_dir_abs.exists():
                _carry_workspace(old_dir_abs, planned_dir_abs, result)

            # Media carry-on-move rescue (DEFECT B + DEFECT D unified):
            # When a page MOVES and a prev-recorded media file was NOT
            # re-materialized at the new dir this run — regardless of WHY
            # (attachments_complete=False OR media=False OR the new-version blob
            # is missing) — carry the attachment's LAST-GOOD content to the new
            # .media/ dir by materializing from the immutable blob store.
            #
            # Materializing from the content-addressed store (rather than moving
            # the old on-disk file) makes this:
            #   - independent per page: an N-page title rotation that shares an
            #     attachment filename is safe — there is no cross-page file
            #     shuffle, so one page overwriting another's old dir cannot lose
            #     bytes (the source of truth is the blob, not the disk file);
            #   - crash-safe: materialize stages via .conex/tmp + os.replace, and
            #     the prev blob survives any crash (it is referenced by prev
            #     state, which is saved only on success).
            #
            # Gate: carry only attachment IDs still in att_states after the write
            # phase (explicitly-dropped attachments are left as deferred-cleanup
            # candidates), skip a renamed attachment whose current name was
            # already materialized (BL-1), and record new_file in result.written
            # so it enters _keep before the deferred old-media cleanup runs.
            if prev is not None and pid in prev.pages:
                new_media_for_move = planned_dir_abs / ".media"
                _assert_writable_dir(root_real, new_media_for_move)
                written_cf = {str(p).casefold() for p in result.written}
                for prev_att_id, prev_att_state in prev.pages[pid].attachments.items():
                    # Only carry attachments still tracked this run.  Attachments
                    # absent from att_states were explicitly dropped — leave them
                    # as candidates for the deferred cleanup.
                    if prev_att_id not in att_states:
                        continue
                    # Skip ONLY when the attachment was renamed this run AND its
                    # current-named file was actually materialized at the new dir
                    # (the old name is then an orphan for deferred cleanup — BL-1).
                    current_name = att_states[prev_att_id].file
                    if current_name != prev_att_state.file and current_name in media_available:
                        continue
                    fname = prev_att_state.file
                    if not fname:
                        continue
                    new_file = new_media_for_move / fname
                    if str(new_file).casefold() in written_cf:
                        continue  # already materialized at the new dir this run
                    # Carry the attachment's LAST-GOOD content to the new dir by
                    # materializing from the immutable, content-addressed blob
                    # store — the prev AttachmentState records the digest, and the
                    # prior run's GC keeps it (it is referenced by prev state).
                    # This is independent per page (no cross-page file shuffle, so
                    # an N-page title rotation sharing a filename is safe) and
                    # crash-safe (materialize stages via .conex/tmp + os.replace).
                    # A page whose prev download had failed has no blob → nothing
                    # to carry → skip; the on-disk file is never the source of
                    # truth, so a swap partner overwriting it cannot lose bytes.
                    prev_blob = prev_att_state.blob
                    if not (prev_blob and blobs.has(prev_blob)):
                        continue
                    new_media_for_move.mkdir(parents=True, exist_ok=True)
                    blobs.materialize(prev_blob, new_file)
                    # Record the LAST-GOOD attachment state — the content now
                    # actually on disk — rather than the missing new digest that
                    # the failed materialize recorded.  This keeps state in sync
                    # with disk, makes the Step-8 GC keep prev_blob (it is now a
                    # new-state .blob value), and lets a FURTHER move while the new
                    # version is still missing rescue it again.  Otherwise GC would
                    # reclaim the last-good blob and the next move would lose the
                    # bytes entirely (multi-run data loss).
                    att_states[prev_att_id] = prev_att_state
                    result.written.append(new_file)
                    media_available.add(fname)
                    written_cf.add(str(new_file).casefold())

            # Old-artifact deletion is no longer deferred here: the page's new
            # paths are now in the new state and its old paths are not, so the
            # single reconciliation pass (below) deletes the old paths as the
            # complement of ownership — title-swap/rotation-safe by construction.
            result.moved.append((old_dir_rel, str(planned_dir_rel)))

        # Build PageState.  rendered_media records the batch-rendered drawio PNGs
        # (not attachments, so they have no id) so they are part of the page's
        # owned-path set and the reconciliation deletes the old copy on a move.
        new_page_states[pid] = PageState(
            dir=str(planned_dir_rel),
            file=str(planned_file_rel),
            html=html_rel,
            title=page.title,
            version=page.version.number,
            status=page.status,
            fingerprint=fingerprints[pid],
            attachments=att_states,
            rendered_media=sorted(rendered_png_digests_to_materialize.keys()),
        )

    # -----------------------------------------------------------------------
    # Step 6 — Reconcile the filesystem to the new state
    # -----------------------------------------------------------------------

    # Space-identity guard — the output directory must hold THIS space's export.
    # Without it, exporting space B into a directory that holds space A's export
    # makes every page of A absent from B's snapshot, and the reconciliation
    # below deletes them as the complement of ownership (a quiet, easy-to-trigger
    # data-loss path: `conex export B -o dir_of_A`).  Abort before any deletion.
    if prev is not None and prev.pages and _space_mismatch(prev, snapshot):
        raise StateError(
            f"output directory holds an export of space "
            f"{(prev.space_key or prev.space_id)!r} but this run targets space "
            f"{(snapshot.space.key or snapshot.space.id)!r}; refusing to "
            f"reconcile across spaces (it would delete the existing export). "
            f"Use a different output directory."
        )

    # Guards that ABORT the reconciliation entirely (return prev unchanged, no
    # deletions, no GC) — these are the only ways a reconcile is skipped.
    #
    # I2 zero-pages guard — FULL-SPACE only.  An empty plan on a full export
    # (auth failure, empty listing) must not nuke the export.  An empty plan from
    # a --path run whose subtree RESOLVED is a legitimately-empty subtree (its
    # only pages were deleted upstream) is NOT guarded — it falls through to the
    # scoped reconciliation, which deletes the in-scope pages while preserving
    # everything out of scope (BL-I2-SUBTREE-GLOBAL).
    if not plan.dirs and opts.subtree is None and prev is not None and prev.pages:
        msg = (
            "build: plan is empty but previous state has pages; "
            "skipping all pruning to avoid data loss (I2)"
        )
        result.warnings.append(msg)
        warnings.warn(msg, stacklevel=2)
        return result, prev

    # Defense-in-depth: a subtree was requested but its planned dir could not be
    # resolved (a truly-missing/typo'd --path).  Without a subtree_dir the
    # reconciliation cannot scope itself, so skip it rather than risk deleting
    # out-of-scope pages (the CLI path-not-found check normally catches this
    # before build is reached).
    if opts.subtree is not None and plan.subtree_dir is None:
        msg = (
            "build: subtree requested but its planned directory could not be "
            "resolved; skipping all pruning to avoid deleting out-of-scope pages"
        )
        result.warnings.append(msg)
        warnings.warn(msg, stacklevel=2)
        return result, prev

    snapshot_page_ids = {p.id for p in snapshot.pages}

    if prev is not None:
        for pid, ps in prev.pages.items():
            if pid in plan.dirs:
                continue  # will be in new state

            # I3: preserve archived pages when snapshot.include_archived is False.
            if ps.status == "archived" and not snapshot.include_archived:
                new_page_states[pid] = ps
                continue

            # A page still present in the snapshot is LIVE — it merely left the
            # plan because it was reparented out of the exported --path subtree,
            # not deleted upstream.  Carry it forward; only pages genuinely gone
            # from the snapshot are pruned.  (On a full export every snapshot page
            # is in the plan, so this only affects scoped runs.)
            if pid in snapshot_page_ids:
                new_page_states[pid] = ps
                continue

            # Subtree scope: only prune pages inside the subtree.  --no-children
            # narrows scope to the root alone so descendants are preserved (BL-2:
            # a scope-narrowing flag must never delete on-disk descendants).
            if opts.subtree is not None and plan.subtree_dir is not None:
                if not _dir_in_subtree(ps.dir, str(plan.subtree_dir), opts.no_children):
                    new_page_states[pid] = ps
                    continue

            # else: genuinely removed upstream and in scope → NOT carried into
            # new_page_states, so the reconciliation below deletes it.

    # Folders recorded in the new state: the plan's folders, plus — on a scoped
    # subtree run — any prev folders OUTSIDE the subtree carried forward verbatim
    # (a --path export restricts plan.folder_dirs to the subtree; without this
    # the rest of the space's folder records would be dropped from state).
    new_folders: dict[str, str] = {}
    if opts.subtree is not None and prev is not None and plan.subtree_dir is not None:
        for fid, fdir in (prev.folders or {}).items():
            if not _dir_in_subtree(fdir, str(plan.subtree_dir), opts.no_children):
                new_folders[fid] = fdir
    new_folders.update({fid: str(d) for fid, d in plan.folder_dirs.items()})

    # Delete everything prev owned that the new state no longer owns (the
    # complement of ownership) — ONE set difference covering move-cleanup AND
    # prune; title-swaps and N-page rotations are safe by construction.
    if prev is not None:
        _reconcile_deletions(root, prev, new_page_states, new_folders, result)

    # -----------------------------------------------------------------------
    # Step 7 — Build and save ExportState (I6: once, at the end)
    # -----------------------------------------------------------------------
    import datetime as _dt

    # new_page_states (skipped + written + carried survivors) and new_folders
    # were finalized above, before the reconciliation used them as the authority
    # for what to keep.
    new_state = ExportState(
        schema_version=1,
        space_key=snapshot.space.key,
        space_id=snapshot.space.id,
        updated_at=_dt.datetime.now(tz=timezone.utc).isoformat(),
        converter_version=CONVERTER_VERSION,
        pages=new_page_states,
        folders=new_folders,
    )

    from conex.store.state import StateStore

    StateStore(root).save(new_state)

    # -----------------------------------------------------------------------
    # Step 8 — Blob GC (the abort guards above return before reaching here)
    # -----------------------------------------------------------------------
    keep: set[str] = set()
    keep.update(snapshot.body_blobs.values())
    keep.update(snapshot.attachment_blobs.values())
    keep.update(snapshot.derived_blobs.values())
    # Freshly-rendered drawio PNGs are not yet in snapshot.derived_blobs;
    # include them explicitly so GC does not delete them on the same build.
    keep.update(freshly_rendered_digests)
    for ps in new_state.pages.values():
        for att_state in ps.attachments.values():
            if att_state.blob:
                keep.add(att_state.blob)
    blobs.gc(keep)

    return result, new_state


# ---------------------------------------------------------------------------
# Internal helper (deferred import of drawio render version)
# ---------------------------------------------------------------------------


def _get_drawio_render_version() -> int:
    try:
        from conex import drawio as _drawio  # type: ignore[import]
        return _drawio.DRAWIO_RENDER_VERSION
    except (ImportError, AttributeError):
        return 1
