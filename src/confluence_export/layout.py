"""Pure layout planning: assign a collision-free on-disk path to every page.

This module has no I/O. ``plan_layout`` walks the page tree once and returns,
for every page id, the page's target directory relative to the export output
directory. The decisive property is that a *single* allocated segment is used
for BOTH the directory leaf and the markdown filename (the exporter reads
``target_dir.name``), so the two can never desync — issue #11's silent overwrite
came from two independent ``sanitize_filename`` calls. Disambiguation is per
parent and casefold-aware, so siblings whose titles collapse to the same name on
a case-insensitive filesystem still get distinct, stable paths.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from confluence_export.converter import MAX_FILENAME_LEN, sanitize_filename
from confluence_export.types import PageNode


def _truncate_with_suffix(segment: str, suffix: str) -> str:
    """Append ``suffix`` to ``segment`` without exceeding ``MAX_FILENAME_LEN``.

    The base is truncated to ``MAX_FILENAME_LEN - len(suffix)`` first so the
    combined result still fits, mirroring ``sanitize_filename``'s own cap.
    """
    avail = MAX_FILENAME_LEN - len(suffix)
    truncated = segment[:avail].rstrip("-")
    if not truncated:
        # Defensive: an empty/all-dash base would otherwise yield a leading-dash
        # segment like "-2". sanitize_filename never produces this today, but
        # don't depend on that.
        truncated = "untitled"
    return f"{truncated}{suffix}"


def _allocate_segment(title: str, page_id: str, taken: dict[str, str]) -> str:
    """Allocate a collision-free path segment within one parent's namespace.

    ``taken`` maps ``casefold(segment) -> page_id`` of the claimant. The first
    sibling (in allocation order) to want a name keeps the bare sanitized form;
    any later sibling colliding under casefold gets a numeric ``-2``/``-3``/...
    suffix, with length reserved so the suffixed name never exceeds the cap.
    """
    base = sanitize_filename(title)
    if base.casefold() not in taken:
        taken[base.casefold()] = page_id
        return base

    n = 2
    while True:
        candidate = _truncate_with_suffix(base, f"-{n}")
        if candidate.casefold() not in taken:
            taken[candidate.casefold()] = page_id
            return candidate
        n += 1


def _walk(nodes: list[PageNode], parent_dir: PurePosixPath, plan: dict[str, PurePosixPath]) -> None:
    """Allocate one sibling group, then recurse into each child group.

    Siblings are visited in ``(position, id)`` order so the disambiguating suffix
    is handed to the same sibling on every run regardless of API/cache ordering.
    This order is local to allocation — it does not change tree traversal or
    ``conex tree`` output, which keep ``build_tree``'s position ordering.
    """
    taken: dict[str, str] = {}
    for node in sorted(nodes, key=lambda n: (n.page.position, n.page.id)):
        segment = _allocate_segment(node.page.title, node.page.id, taken)
        target_dir = parent_dir / segment
        plan[node.page.id] = target_dir
        if node.children:
            _walk(node.children, target_dir, plan)


def plan_layout(roots: list[PageNode]) -> dict[str, PurePosixPath]:
    """Compute the collision-free on-disk layout for an entire page tree.

    Returns a mapping of ``page_id -> target_dir`` (the page's own directory,
    relative to the output directory; root pages sit directly under it).
    Allocation is per parent, so a page's segment depends only on its siblings.
    The exporter consumes ``target_dir.name`` (the leaf) for both the directory
    and the markdown stem; the reconciler (issue #17) compares the full
    ``target_dir`` against the page's current on-disk path.
    """
    plan: dict[str, PurePosixPath] = {}
    _walk(roots, PurePosixPath(), plan)
    return plan
