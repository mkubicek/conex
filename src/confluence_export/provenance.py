"""Explicit archived-preservation provenance model.

This module exists to END a treadmill. The export tool preserves a prior
``--include-archived`` export's ``_archived/`` subtree across later runs that do
NOT see archived pages, while still pruning genuinely-stale content. That logic
used to live in two near-parallel branches in ``exporter.py`` that decided
"is this an archived page?" by string-matching the on-disk directory name
(``_archived`` / ``_archived-*``). Dirname-as-provenance is fragile: a real live
page literally titled ``_archived`` collides with it, which spawned a series of
point fixes (M1, M1b, RF-A, RF-A-coll, ...).

The fix is to drive every decision off a REAL runtime provenance signal instead
of a dirname, and to express each hard-won invariant as its own NAMED, pure,
unit-testable predicate. A regression in any single edge case now surfaces as one
failing predicate test rather than a buried end-to-end failure.

The one signal that collapses the whole subsystem is ``cs.include_archived``
(``cache.py``: ``include_archived or client.returns_archived_pages``, persisted on
the cache so it is correct even on a ``--cached`` hit):

    cache_sees_archived is True  -> the EXACT archived page ids and their planned
                                    on-disk targets are known: preserve page-exact
                                    dirs (Guard ``exact_archived_dirs``).
    cache_sees_archived is False -> the archived id set is UNKNOWABLE this run
                                    (cookie_v1 current-only, or a legacy cache):
                                    fall back to preserving prior on-disk
                                    ``_archived*`` roots (Guard ``recursive_archived_dirs``).

The ``_archived`` directory NAME survives in ``recursive_archived_dirs`` as a
directory-existence filter -- a prior on-disk export carries no in-memory
provenance, so "is there a prior archived export at this path" can only be
answered from the disk. For a root that a LIVE page claims this run (its segment
is in ``live_root_segments``), the dirname does NOT decide live-vs-archived:
per-entry ``status`` / ``path`` frontmatter does, so the live page's own content
stays prunable. For a root that NO live page owns on a blind run, the
archived-named directory's existence is, by necessity, the only signal we have
(accepted residual: a stray folder literally named ``_archived`` would be kept --
the alternative, deleting it, risks erasing a real prior archived export, so we
fail safe toward preserve). Only the synthetic root name and its NUMERIC collision
suffixes (``_archived``, ``_archived-2``, ...) match -- the exact set
``plan_layout`` emits -- so a live page named ``_archived-notes`` is not mistaken
for one.

DATA-SAFETY DECISION (deliberate, see also the cli prune gate in ``cli.py``):
a full export that returns ZERO pages is NEVER pruned to empty. An empty result
-- whether a genuinely emptied space or an auth/transient failure that returned an
empty set -- preserves the previously-committed export. This module therefore has
NO "prune the emptied space" predicate; pruning only ever happens when real pages
were written or explicit page-owned dirs must be protected/restored.

Edge cases covered (see tests/test_provenance.py for the per-predicate tests):
  M1 / M1b    full export omitting archived preserves prior _archived/ exactly.
  M1-exact    a page moved OUT of an archived parent is preserved per-page (not
              recursively), so the moved-out copy is still prunable.
  RF-A        current-only refresh preserves a prior on-disk _archived/ root.
  RF-A-coll   a live page titled "_archived" does NOT shadow / get preserved as
              the archived root; a real archived page underneath still is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from confluence_export.diff import ExportedPage

# The synthetic archived root segment (see tree.py: the "__archived__" node is
# named "_archived", and the collision-free layout suffixes clashes as
# "_archived-2", "_archived-3", ...). Used ONLY as a directory-existence filter.
ARCHIVED_ROOT_NAME = "_archived"


def _is_archived_root_segment(name: str) -> bool:
    """True if a top-level on-disk dir name is the synthetic archived root or one
    of its NUMERIC collision suffixes (``_archived``, ``_archived-2``,
    ``_archived-3``, ...). ``plan_layout`` only ever emits numeric ``-{n}``
    suffixes for the ``__archived__`` node, so a live page with a non-numeric name
    like ``_archived-notes`` is NOT mistaken for an archived root and stays
    prunable. Used only as a directory-existence narrowing filter."""
    if name == ARCHIVED_ROOT_NAME:
        return True
    prefix = ARCHIVED_ROOT_NAME + "-"
    return name.startswith(prefix) and name[len(prefix):].isdigit()


def _dedup(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            out.append(path)
    return out


@dataclass(frozen=True)
class RunProvenance:
    """Immutable snapshot of the REAL provenance signals for one export run.

    Built once in ``export_space``; every preservation predicate reads ONLY this
    snapshot -- never the live client, never a directory name as a truth source.
    """

    # is_full_export(path_filter, no_children): a partial export never prunes, so
    # it never needs to protect anything from a prune.
    is_full: bool
    # cs.include_archived: the authoritative-visibility bit. True iff this run's
    # cache could see archived pages (so the archived id set is trustworthy).
    cache_sees_archived: bool
    # Real archived page ids (collect_subtree of the synthetic __archived__ node),
    # empty when none are visible.
    archived_ids: frozenset[str]
    # Page ids written this run (the live roots actually exported).
    in_scope_ids: frozenset[str]
    # self._plan: page_id -> collision-free on-disk target (planned over the full
    # tree, so archived targets stay byte-exact).
    plan: dict[str, PurePosixPath]
    # M2 snapshot: page_id -> the page's on-disk dirs BEFORE reconcile moved them.
    pre_reconcile_dirs: dict[str, list[Path]]
    # The single shared disk scan (the M2 pre-reconcile scan), flattened. Reused
    # here so the blind fallback needs no extra scan of its own.
    on_disk_entries: tuple[ExportedPage, ...]
    output_dir: Path


def preservation_in_scope(p: RunProvenance) -> bool:
    """[M1 / M1b] Preservation is only relevant on a full export that did NOT
    write the archived pages itself. A partial export never prunes (so nothing to
    protect); an ``--include-archived`` run writes the archived pages (so their
    files are in ``written_files`` and need no preservation)."""
    return p.is_full and not bool(p.archived_ids & p.in_scope_ids)


def archived_set_is_knowable(p: RunProvenance) -> bool:
    """[the design seam] The exact archived id set is trustworthy when EITHER the
    cache authoritatively covered archived pages (so "no archived exist" is itself
    trustworthy), OR we are actually holding archived pages in the in-memory tree
    (their exact on-disk targets are in the plan). The blind on-disk fallback is
    only needed when we have neither signal -- a current-only/legacy cache that
    returned zero archived pages yet a prior archived export may sit on disk.

    This is the real signal the old M1 (archived-node-present) / RF-A
    (dirname-match) split was reconstructing badly."""
    return p.cache_sees_archived or bool(p.archived_ids)


def exact_archived_dirs(p: RunProvenance) -> list[Path]:
    """[M1, M1-exact, ARCH-ONLY] Page-EXACT dirs for archived pages NOT in scope
    this run, taken from the plan (byte-identical to where the content lives) plus
    their pre-reconcile old paths. NOT recursive: a page that moved OUT of an
    archived parent (M1-exact) keeps only its own dir protected, so the moved-out
    copy is still pruned."""
    in_scope_dirs = {
        p.output_dir.joinpath(*p.plan[pid].parts).resolve()
        for pid in p.in_scope_ids
        if pid in p.plan
    }
    out: list[Path] = []
    for aid in p.archived_ids:
        if aid in p.in_scope_ids:  # unarchived -> now live -> not preserved
            continue
        if aid in p.plan:
            out.append(p.output_dir.joinpath(*p.plan[aid].parts))
        out.extend(
            d
            for d in p.pre_reconcile_dirs.get(aid, [])
            if d.resolve() not in in_scope_dirs
        )
    return _dedup(out)


def recursive_archived_dirs(p: RunProvenance) -> list[Path]:
    """[RF-A, RF-A-coll] cache_sees_archived is False -> the archived id set is
    UNKNOWABLE, so we cannot do page-exact protection. Preserve, recursively, any
    prior on-disk ``_archived*`` root that this run does not own as a live page.

    The directory NAME only narrows which unowned roots to inspect. The
    live-vs-archived decision is:
      * a whole ``_archived*`` root is preserved when its name is NOT a live page's
        planned root segment; otherwise
      * the root belongs to a live page that claimed the ``_archived`` name, so we
        fall back to per-entry frontmatter provenance and preserve only the
        genuinely-archived page dirs underneath (status == "archived", or a legacy
        entry whose recorded path is not under the live root), leaving the live
        page's own content prunable.
    """
    live_root_segments = {
        t.parts[0]
        for pid, t in p.plan.items()
        if pid != "__archived__" and t.parts
    }

    # Per-entry rescue, ONLY for roots a live page claims this run: decide which
    # page dirs under an otherwise-live-owned _archived root are genuinely archived
    # (by frontmatter provenance, never by the dir name). Roots no live page owns
    # are preserved whole by the directory-existence branch below, so they need no
    # per-entry accounting here.
    per_root_archived: dict[str, list[Path]] = {}
    for entry in p.on_disk_entries:
        if not entry.file_path.is_relative_to(p.output_dir):
            continue
        rel_parts = entry.file_path.relative_to(p.output_dir).parts
        if not rel_parts or not _is_archived_root_segment(rel_parts[0]):
            continue
        root_name = rel_parts[0]
        if root_name not in live_root_segments:
            continue
        root_path = "/" + root_name
        # This entry is the LIVE page's own content -> leave it prunable. An entry
        # with NO recorded path (legacy / hand-edited export, no status) is treated
        # as live content too: without frontmatter provenance we cannot call it
        # archived, and preserving it would re-protect the whole live-claimed root,
        # defeating the "live content under a claimed _archived name stays prunable"
        # guarantee.
        if entry.status != "archived" and (
            not entry.path
            or entry.path == root_path
            or entry.path.startswith(root_path + "/")
        ):
            continue
        per_root_archived.setdefault(root_name, []).append(entry.file_path.parent)

    dirs: list[Path] = []
    if p.output_dir.exists() and p.output_dir.is_dir():
        for child in sorted(p.output_dir.iterdir()):
            if not (child.is_dir() and _is_archived_root_segment(child.name)):
                continue
            if child.name in live_root_segments:
                # A live page owns this root name: preserve only the genuinely
                # archived page dirs found underneath, not the whole root.
                dirs.extend(per_root_archived.get(child.name, []))
            else:
                # No live page owns this root: it is entirely a prior archived
                # export -> preserve it recursively.
                dirs.append(child)
    return _dedup(dirs)
