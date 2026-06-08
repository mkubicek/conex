"""Typed protection model for the git stale-prune and restore.

Why this module exists — it ends a treadmill. The export tool must protect
certain on-disk dirs from the git stale-prune (archived pages not written this
run, pages skipped on a transient failure, a blind-run ``_archived`` subtree)
while still pruning genuine upstream deletions. That policy used to live as THREE
interchangeable ``list[Path]`` fields routed by a hand-written ``+`` in cli.py,
plus a ``resolve()``/``absolute()`` dual-form set comparison DUPLICATED across the
prune and the restore in git.py, plus inline imperative media/move-window
arithmetic. Each of those was a place the next point-fix could grow.

This module welds each decision into exactly one shape:

  * SCOPE IS A TYPE. ``PageExactProtection`` (the page's own files + ``.media``,
    NOT a child page — M1-exact) and ``SubtreeProtection`` (the dir and everything
    beneath — skipped pages + blind ``_archived``) are distinct frozen wrappers,
    so a page-exact value physically cannot occupy the subtree slot. No untyped
    ``list[Path]`` slot is left to mis-route.
  * The dual-form match lives once, in ``ProtectedDir``, and is shared verbatim by
    prune AND restore so they cannot drift. Its match methods take the PRECOMPUTED
    ``(file_resolved, file_lexical)`` pair — see the security note on
    ``owns_exactly``.
  * media-keep (M1a / RF-C) and move-window (M2 / RF-B) are NAMED pure predicates
    (``media_file_is_preserved`` / ``move_window_dirs``) with their own unit tests,
    so a regression fails one small predicate test, not a buried end-to-end path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from confluence_export.media import MEDIA_DIR_NAME


@dataclass(frozen=True)
class PageExactProtection:
    """One page dir protected EXACTLY: its own files and its ``.media`` subtree,
    but NOT a nested child page. Scope of archived pages whose precise on-disk
    target is known (M1-exact: a page moved out of an archived parent keeps only
    its own dir protected, so the moved-out copy is still reconciled)."""

    path: Path


@dataclass(frozen=True)
class SubtreeProtection:
    """One dir protected RECURSIVELY: the dir and everything beneath it. Scope of a
    page SKIPPED this run (transient failure — we cannot know its descendants' true
    upstream state) and a wholesale-omitted ``_archived`` subtree on a blind run."""

    path: Path


def prune_media_owner_set(
    prune_media_dirs: list[Path], output_dir: Path
) -> frozenset[Path]:
    """Fold each prune-media path (a ``.media`` dir or its owner page dir) to the
    RESOLVED owner dir, dropping ``output_dir`` itself. This is the override set
    that AUTHORIZES pruning otherwise-preserved media on a ``--no-media`` run
    (RF-C). Lifted verbatim from the old git.py owner-set comprehension."""
    out_resolved = output_dir.resolve()
    return frozenset(
        (p.parent.resolve() if p.name == MEDIA_DIR_NAME else p.resolve())
        for p in (prune_media_dirs or [])
        if p.resolve() != out_resolved
    )


@dataclass(frozen=True)
class ProtectionSet:
    """The single typed bundle ``commit_export`` receives, replacing the exporter's
    interchangeable protected-path lists.

    ``page_exact`` and ``subtrees`` hold DISTINCT wrapper types in DISTINCT fields,
    so scope can never be mis-routed (there is no shared ``list[Path]`` slot).
    ``prune_media_owners`` is NOT a protection: it is the opposite-polarity
    override that FORCES media pruning, carried alongside as a plain frozenset of
    resolved owner dirs and never matched as protection. Frozen with hashable
    fields, so it is safe as a default argument and supports exact structural
    equality in tests."""

    page_exact: tuple[PageExactProtection, ...] = ()
    subtrees: tuple[SubtreeProtection, ...] = ()
    prune_media_owners: frozenset[Path] = frozenset()

    @classmethod
    def from_exporter(
        cls,
        *,
        preserved_page_paths: list[Path],
        preserved_paths: list[Path],
        skipped_paths: list[Path],
        prune_media_dirs: list[Path],
        output_dir: Path,
    ) -> "ProtectionSet":
        """The ONE place that routes scope. The ``preserved_paths + skipped_paths``
        union that used to be a hand-written ``+`` in cli.py moves here, to the
        single producer that knows skipped pages are protected recursively and
        archived-exact pages page-exactly. Keeping the two scope inputs in separate
        params makes the M1-exact-vs-recursive routing impossible to swap."""
        return cls(
            page_exact=tuple(PageExactProtection(p) for p in preserved_page_paths),
            subtrees=tuple(
                SubtreeProtection(p) for p in (*preserved_paths, *skipped_paths)
            ),
            prune_media_owners=prune_media_owner_set(prune_media_dirs, output_dir),
        )


def _path_is_owned_by(path: Path, page_dir: Path) -> bool:
    """Whether ``path`` is owned page-EXACTLY by ``page_dir``: a file directly in
    ``page_dir`` (its own .md/.html), or a file under ``page_dir``'s ``.media``
    subtree. A nested CHILD page is NOT owned — that is the M1-exact distinction.
    Single-dir core of the old git.py ``_is_page_owned_path`` set helper."""
    if path.parent == page_dir:
        return True
    try:
        rel = path.relative_to(page_dir)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] == MEDIA_DIR_NAME


@dataclass(frozen=True)
class ProtectedDir:
    """One protected dir compiled to BOTH the symlink-FOLLOWING (``resolved``) and
    the lexical NON-following (``lexical``) form, so the dual-form OR is written
    once and shared by the prune and the restore. A form is ``None`` when it equals
    ``output_dir`` (self-excluded, so the whole tree is never protected).

    SECURITY — the match methods take the PRECOMPUTED ``(file_resolved,
    file_lexical)`` pair the git loop already derives (``full_lexical =
    (output_dir / rel_path).absolute()``; ``full = full_lexical.resolve()``). They
    MUST NOT be "simplified" to take a single path and re-derive both forms: on a
    protected dir that is a symlink to an outside target, re-deriving the lexical
    form from the resolved path makes the lexical defense silently go dark — and
    every existing symlink test still passes, masking the hole. Both forms are
    always stored and always queried separately; they are NEVER unioned into one
    canonical set (that union would make a symlink compare equal to its target)."""

    resolved: Path | None
    lexical: Path | None

    def owns_exactly(self, file_resolved: Path, file_lexical: Path) -> bool:
        """Page-EXACT match: the file is owned by this dir under EITHER form."""
        return (
            self.resolved is not None
            and _path_is_owned_by(file_resolved, self.resolved)
        ) or (
            self.lexical is not None
            and _path_is_owned_by(file_lexical, self.lexical)
        )

    def contains_subtree(self, file_resolved: Path, file_lexical: Path) -> bool:
        """RECURSIVE match: the file lives under this dir under EITHER form."""
        return (
            self.resolved is not None
            and file_resolved.is_relative_to(self.resolved)
        ) or (
            self.lexical is not None
            and file_lexical.is_relative_to(self.lexical)
        )


def build_protected(
    output_dir: Path,
    protection: ProtectionSet,
    fold: Callable[[str], str] | None = None,
) -> tuple[list[ProtectedDir], list[ProtectedDir]]:
    """Compile a ``ProtectionSet`` into ``(page_exact_dirs, subtree_dirs)`` of
    ``ProtectedDir``, each carrying both path forms with ``output_dir`` self-
    excluded per-form. Built ONCE and shared by ``_remove_stale_files`` and
    ``_restore_protected_deletions`` so the two can never drift.

    Each stored path form is run through ``fold`` — the SAME case/NFC fold the
    prune applies to its staleness key — so a protected dir whose on-disk case or
    Unicode form differs from this run's planned name still matches. Without it, a
    case-only title change on a case-insensitive filesystem makes the protected
    page compare unequal to its committed (other-case) path and the prune deletes
    it (#44). ``fold`` is applied PER FORM, so the resolved/lexical dual remains two
    independent symlink-safe values (never unioned).

    CONTRACT: this is half of a "compiled once, shared, cannot drift" pair — the
    matchers it returns are queried by ``_remove_stale_files`` /
    ``_restore_protected_deletions``, which MUST fold the queried file forms with
    the SAME function or matching silently drifts and a protected dir is pruned
    (#44). Production (``commit_export``) probes the FS once and threads one fold to
    all three, so they always agree. ``fold`` defaults to identity (a no-op) — NOT
    the prune's fold (that is ``nfc`` on a case-sensitive FS, which still folds
    NFD→NFC, and ``nfc_casefold`` on a case-insensitive one). The identity default
    is for unit tests over fold-invariant/ASCII inputs only; any direct caller that
    pairs this with a consumer MUST pass that consumer's exact fold."""
    f = fold or (lambda s: s)
    out_resolved = Path(f(str(output_dir.resolve())))
    out_absolute = Path(f(str(output_dir.absolute())))

    def compile_dir(p: Path) -> ProtectedDir:
        resolved = Path(f(str(p.resolve())))
        lexical = Path(f(str(p.absolute())))
        return ProtectedDir(
            resolved=(resolved if resolved != out_resolved else None),
            lexical=(lexical if lexical != out_absolute else None),
        )

    def active(d: ProtectedDir) -> bool:
        return d.resolved is not None or d.lexical is not None

    page_dirs = [
        d for d in (compile_dir(pr.path) for pr in protection.page_exact) if active(d)
    ]
    sub_dirs = [
        d for d in (compile_dir(s.path) for s in protection.subtrees) if active(d)
    ]
    return page_dirs, sub_dirs


def media_file_is_preserved(
    owner: Path,
    *,
    written_page_dirs: frozenset[Path],
    written_media_dirs: frozenset[Path],
    prune_media_owners: frozenset[Path],
    page_exact: list[ProtectedDir],
    subtrees: list[ProtectedDir],
) -> bool:
    """[M1a / RF-C] On a ``--no-media`` run, whether to KEEP a committed ``.media``
    file that is absent from this run's written_files. Keep iff its ``owner`` page
    still produced output this run AND its media was not itself re-written AND it is
    not force-pruned; OR the owner is page-exact protected; OR the owner lives under
    a recursively-protected subtree.

    RESOLVED-ONLY by construction: ``owner`` is already a resolved path and the old
    inline check (``owner in protected`` / ``is_relative_to`` over the resolved
    subtree set) had no lexical variant, so this queries ``d.resolved`` only — never
    ``d.lexical`` — preserving that asymmetry exactly."""
    keep_by_write = (
        owner in written_page_dirs
        and owner not in written_media_dirs
        and owner not in prune_media_owners
    )
    return (
        keep_by_write
        or any(owner == d.resolved for d in page_exact)
        or any(
            d.resolved is not None and owner.is_relative_to(d.resolved)
            for d in subtrees
        )
    )


def move_window_dirs(
    page_dir: Path, page_id: str, pre_reconcile_dirs: dict[str, list[Path]]
) -> list[Path]:
    """[M2 / RF-B] The dirs to protect RECURSIVELY when a page is skipped this run:
    its CURRENT dir plus every pre-reconcile (moved-from) dir, so a transient
    failure cannot let the prune drop the last-good committed copy and the moved-out
    old path is restored from HEAD. Pure: replaces the five identical
    ``append(page_dir); extend(pre_reconcile.get(id, []))`` pairs in the exporter
    walk with one named producer."""
    return [page_dir, *pre_reconcile_dirs.get(page_id, [])]


def _media_owner_dir(output_dir: Path, rel_path: str) -> Path | None:
    """The resolved owner page dir of a tracked ``.media`` file, or None if the
    path is not under a ``.media`` dir. Moved verbatim from git.py."""
    parts = Path(rel_path).parts
    if MEDIA_DIR_NAME not in parts:
        return None
    idx = parts.index(MEDIA_DIR_NAME)
    return output_dir.resolve().joinpath(*parts[:idx]).resolve()


def page_dirs_from_written_files(
    output_dir: Path, written_files: list[Path]
) -> frozenset[Path]:
    """Page directories that successfully produced output in this export. Moved
    verbatim from git.py (return narrowed to frozenset for the predicate)."""
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
    return frozenset(page_dirs)


def media_owner_dirs_from_written_files(
    output_dir: Path, written_files: list[Path]
) -> frozenset[Path]:
    """Page directories whose current media files were explicitly written/kept.
    Moved verbatim from git.py (return narrowed to frozenset for the predicate)."""
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
    return frozenset(owners)
