"""Page and folder tree → collision-free on-disk path plan.

This module has no I/O. :func:`plan_layout` walks the merged page+folder tree
once and returns, for every page id and folder id, the target directory and
markdown file path relative to the export output directory.

Design contract (invariants callers may rely on):
- **I-alloc**: a single allocated *segment* drives both the directory leaf and
  the markdown stem for every page.  The two can never desync.
- **I-casefold**: collision detection uses NFC-casefold so siblings whose
  sanitized titles differ only in case or Unicode canonical form get distinct,
  stable paths rather than silently overwriting each other on a normalizing
  filesystem (APFS, HFS+).
- **I-position**: allocation visits siblings in ``(position, id)`` order so
  the disambiguation suffix is assigned to the same sibling on every run,
  regardless of API/cache ordering.
- **I-archived**: archived pages live under a synthetic ``_archived/`` root
  that participates in normal collision allocation at the space level.  A live
  page whose parent is archived (or absent from the export) surfaces as a
  space-level root (PR3 behaviour).
- **I-folders**: folders are internal nodes only — they have a directory
  segment but no ``.md`` file.  Pages with ``parent_type == "folder"`` nest
  under their folder node.  A folder with an unknown parent surfaces as a
  space-level root.  This diverges deliberately from v1, which ignored
  folders entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from conex.models import Folder, Page, Space
from conex.paths import nfc_casefold, sanitize_filename, truncate_with_suffix


# ---------------------------------------------------------------------------
# Internal tree node
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    """Internal tree node for a page or folder.

    ``page`` is set for page nodes; ``folder`` is set for folder nodes.
    Exactly one of the two is non-None.
    """

    page: Page | None = None
    folder: Folder | None = None
    children: list[_Node] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        if self.page is not None:
            return self.page.id
        assert self.folder is not None
        return self.folder.id

    @property
    def title(self) -> str:
        if self.page is not None:
            return self.page.title
        assert self.folder is not None
        return self.folder.title

    @property
    def position(self) -> int:
        if self.page is not None:
            return self.page.position
        assert self.folder is not None
        return self.folder.position

    @property
    def is_page(self) -> bool:
        return self.page is not None

    @property
    def is_folder(self) -> bool:
        return self.folder is not None


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutPlan:
    """The collision-free path plan for an entire space export.

    Attributes:
        dirs:        page_id → page DIR relpath (PurePosixPath).
        files:       page_id → ``.md`` file relpath.
        order:       page ids in depth-first tree order (folders excluded).
        folder_dirs: folder_id → DIR relpath.
        subtree_dir: resolved planned dir for the subtree root node, or None
                     when no ``subtree`` was requested.  Build uses this to
                     scope pruning to the subtree.
    """

    dirs: dict[str, PurePosixPath]
    files: dict[str, PurePosixPath]
    order: list[str]
    folder_dirs: dict[str, PurePosixPath]
    subtree_dir: PurePosixPath | None


# ---------------------------------------------------------------------------
# Allocation helpers
# ---------------------------------------------------------------------------


def _fold(segment: str) -> str:
    """Collision key: NFC-casefold.

    Two siblings whose sanitized titles are NFC-equivalent or case-equivalent
    collide here and get a disambiguating suffix, instead of mapping to one
    path and silently overwriting each other on a normalizing filesystem.
    """
    return nfc_casefold(segment)


def _allocate_segment(title: str, node_id: str, taken: dict[str, str]) -> str:
    """Allocate a collision-free path segment within one parent's namespace.

    ``taken`` maps ``_fold(segment) → node_id`` of the claimant.  The first
    sibling (in allocation order) to want a name keeps the bare sanitized form;
    any later sibling colliding under the NFC+casefold fold gets a numeric
    ``-2``/``-3``/… suffix, with length reserved so the suffixed name never
    exceeds the 100-char cap.
    """
    base = sanitize_filename(title)
    if _fold(base) not in taken:
        taken[_fold(base)] = node_id
        return base

    n = 2
    while True:
        candidate = truncate_with_suffix(base, f"-{n}")
        if _fold(candidate) not in taken:
            taken[_fold(candidate)] = node_id
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


def _build_tree(
    pages: list[Page],
    folders: list[Folder],
) -> list[_Node]:
    """Build the merged page+folder tree from flat lists.

    Algorithm:
    1. Create one _Node per page and per folder, keyed by id.
    2. Attach each node to its parent across the merged id-space.  A folder
       may be the parent of a page (``page.parent_type == "folder"``) or of
       another folder.
    3. Archived pages are collected under a synthetic ``__archived__`` root
       node.  Live pages whose parent is archived or absent surface as roots
       (PR3).
    4. Sort children by ``(position, id)`` at every level.
    """
    # Build id → node maps
    page_nodes: dict[str, _Node] = {p.id: _Node(page=p) for p in pages if p.id}
    folder_nodes: dict[str, _Node] = {f.id: _Node(folder=f) for f in folders if f.id}
    all_nodes: dict[str, _Node] = {**folder_nodes, **page_nodes}

    roots: list[_Node] = []
    archived_roots: list[_Node] = []

    # --- Attach folders to their parents ---
    for fnode in folder_nodes.values():
        assert fnode.folder is not None
        parent_id = fnode.folder.parent_id
        parent = all_nodes.get(parent_id) if parent_id else None
        if parent is not None:
            parent.children.append(fnode)
        else:
            # Unknown parent (or no parent) → surface as space root
            roots.append(fnode)

    # --- Attach pages to their parents ---
    for pnode in page_nodes.values():
        assert pnode.page is not None
        p = pnode.page

        if p.status == "archived":
            # Check if the archived page has an archived parent in the set.
            # If so, nest under it; otherwise it becomes an archived root.
            parent = all_nodes.get(p.parent_id) if p.parent_id else None
            if parent and parent.is_page and parent.page is not None and parent.page.status == "archived":
                parent.children.append(pnode)
            else:
                archived_roots.append(pnode)
            continue

        # Live page
        if not p.parent_id:
            roots.append(pnode)
            continue

        parent = all_nodes.get(p.parent_id)
        if parent is None:
            # PR3: parent absent from export → surface as root
            roots.append(pnode)
            continue

        if parent.is_page:
            assert parent.page is not None
            if parent.page.status == "archived":
                # PR3: live page with archived parent → surface as root
                roots.append(pnode)
            else:
                parent.children.append(pnode)
        elif parent.is_folder:
            # Folder parent is always fine (folders are never archived)
            parent.children.append(pnode)

    # --- Synthetic _archived root ---
    if archived_roots:
        archived_page = Page(id="__archived__", title="_archived", status="folder")
        archived_node = _Node(page=archived_page, children=archived_roots)
        roots.append(archived_node)

    # --- Sort children by (position, id) at every level ---
    _sort_tree(roots)
    return roots


def _sort_tree(nodes: list[_Node]) -> None:
    """Sort children by (position, id) in place, recursively."""
    nodes.sort(key=lambda n: (n.position, n.node_id))
    for node in nodes:
        _sort_tree(node.children)


# ---------------------------------------------------------------------------
# Walk + allocate
# ---------------------------------------------------------------------------


def _walk(
    nodes: list[_Node],
    parent_dir: PurePosixPath,
    dirs: dict[str, PurePosixPath],
    files: dict[str, PurePosixPath],
    order: list[str],
    folder_dirs: dict[str, PurePosixPath],
    node_dirs: dict[str, PurePosixPath],
) -> None:
    """Allocate one sibling group's segments, then recurse into each child group.

    Allocation visits siblings in ``(position, id)`` order so the disambiguation
    suffix is assigned to the same sibling on every run (I-position).

    Folders get a directory segment; pages get both a directory and a ``.md`` file.
    The synthetic ``__archived__`` node is treated as a page node for path
    allocation purposes (it maps to the ``_archived/`` segment) but its entry is
    NOT added to ``files`` (no ``.md``).

    ``node_dirs`` records the allocated directory for EVERY node by ``node_id``
    — pages, folders, and the synthetic ``__archived__`` root alike.  It is the
    authoritative "where did this node land" map used to resolve a subtree
    root's planned dir, including the synthetic root that is deliberately
    absent from ``dirs``/``folder_dirs``.
    """
    taken: dict[str, str] = {}

    def _alloc_key(node: _Node) -> tuple[int, int, str]:
        # Synthetic __archived__ node always allocated first (position-0 sentinel)
        if node.is_page and node.page is not None and node.page.id == "__archived__":
            return (0, 0, "")
        return (1, node.position, node.node_id)

    for node in sorted(nodes, key=_alloc_key):
        segment = _allocate_segment(node.title, node.node_id, taken)
        target_dir = parent_dir / segment
        node_dirs[node.node_id] = target_dir

        if node.is_folder:
            folder_dirs[node.node_id] = target_dir
        else:
            assert node.page is not None
            # The synthetic __archived__ root participates in collision
            # allocation (so its _archived/ segment is reserved) but must
            # NOT appear in dirs, files, or order — it has no body blob
            # and is not a real page.  Its allocated dir still lives in
            # node_dirs so a subtree rooted at it resolves correctly.
            if node.page.id != "__archived__":
                dirs[node.page.id] = target_dir
                files[node.page.id] = target_dir / f"{segment}.md"
                order.append(node.page.id)

        if node.children:
            _walk(node.children, target_dir, dirs, files, order, folder_dirs, node_dirs)


# ---------------------------------------------------------------------------
# find_node_by_path  (PORT v1 tree.py — first-match-wins, case-insensitive)
# ---------------------------------------------------------------------------


def _find_by_parts(nodes: list[_Node], parts: list[str]) -> _Node | None:
    """Recursively resolve a sequence of raw-title segments into a node.

    Matching is case-insensitive on the raw title.  First-match-wins among
    same-titled siblings — accepted v1 behaviour; do not "fix" ambiguity.
    """
    if not parts or not nodes:
        return None
    target = parts[0].lower()
    for node in nodes:
        if node.title.lower() == target:
            if len(parts) == 1:
                return node
            return _find_by_parts(node.children, parts[1:])
    return None


def _find_node_by_path(roots: list[_Node], path: str) -> _Node | None:
    """Resolve a slash-separated raw-title path to a node.

    Path segments match node titles case-insensitively.  First-match-wins
    among same-titled siblings at each level.  An empty or root-only path
    returns None.
    """
    path = path.strip("/")
    if not path:
        return None
    parts = path.split("/")
    return _find_by_parts(roots, parts)


# ---------------------------------------------------------------------------
# Subtree collection
# ---------------------------------------------------------------------------


def _collect_subtree_page_ids(node: _Node) -> list[str]:
    """Collect all page ids reachable from (and including) node, pre-order."""
    result: list[str] = []
    if node.is_page and node.page is not None:
        result.append(node.page.id)
    for child in node.children:
        result.extend(_collect_subtree_page_ids(child))
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan_layout(
    space: Space,
    pages: list[Page],
    folders: list[Folder],
    *,
    subtree: str | None = None,
    no_children: bool = False,
) -> LayoutPlan:
    """Compute the collision-free on-disk layout for an entire space export.

    Returns a :class:`LayoutPlan` mapping every page id to its target directory
    and ``.md`` path (both relative to the export root), plus the depth-first
    page traversal order and folder directory mappings.

    Arguments:
        space:       The Confluence space (provides the root directory name).
        pages:       All pages to lay out.
        folders:     All folders to lay out (merged into the tree as internal nodes).
        subtree:     Slash-separated raw-title path to restrict the plan to a
                     subtree rooted at the named node (case-insensitive, first-
                     match-wins).  None means the full space.
        no_children: When True and ``subtree`` is set, include only the single
                     named node, not its descendants.

    Contract:
        - The space root dir is ``sanitize_filename(space.name)`` (or
          ``"untitled"`` for an empty name).
        - Each page's directory leaf equals the ``.md`` stem; the two can never
          desync (I-alloc).
        - Collision-free allocation is NFC-casefold aware (I-casefold).
        - Archived pages nest under a synthetic ``_archived/`` root; live pages
          with an absent or archived parent surface at the space root (PR3).
        - Folders are internal nodes only (no ``.md``); ``folder_dirs`` maps
          folder ids to their allocated directory paths.
    """
    space_root = PurePosixPath(sanitize_filename(space.name) if space.name else "untitled")

    # Build and allocate the FULL tree first (collision allocation must be
    # global across the whole tree, not just the subtree).
    roots = _build_tree(pages, folders)

    dirs: dict[str, PurePosixPath] = {}
    files: dict[str, PurePosixPath] = {}
    order: list[str] = []
    folder_dirs: dict[str, PurePosixPath] = {}
    node_dirs: dict[str, PurePosixPath] = {}

    _walk(roots, space_root, dirs, files, order, folder_dirs, node_dirs)

    if subtree is None:
        return LayoutPlan(
            dirs=dirs,
            files=files,
            order=order,
            folder_dirs=folder_dirs,
            subtree_dir=None,
        )

    # --- Subtree restriction ---
    subtree_node = _find_node_by_path(roots, subtree)
    if subtree_node is None:
        # Subtree not found — return an empty plan.
        return LayoutPlan(
            dirs={},
            files={},
            order=[],
            folder_dirs={},
            subtree_dir=None,
        )

    # Determine the resolved planned dir for the subtree root.  Use node_dirs,
    # which records EVERY node's allocated dir — including the synthetic
    # ``__archived__`` root, which is absent from ``dirs``/``folder_dirs``.
    # Resolving it from ``dirs`` alone would yield None for an ``_archived``
    # subtree, silently disabling build's prune-scope guard and deleting every
    # out-of-subtree live page.
    subtree_dir = node_dirs.get(subtree_node.node_id)

    if no_children:
        # Only include the root node itself (if it's a page).
        restricted_ids: set[str] = set()
        if subtree_node.is_page and subtree_node.page is not None:
            restricted_ids.add(subtree_node.page.id)
    else:
        # Include the root node and all descendants.
        all_sub_ids = _collect_subtree_page_ids(subtree_node)
        restricted_ids = set(all_sub_ids)

    # Build restricted plan — also restrict folder_dirs to folders reachable
    # within the subtree.
    restricted_dirs = {pid: d for pid, d in dirs.items() if pid in restricted_ids}
    restricted_files = {pid: f for pid, f in files.items() if pid in restricted_ids}
    restricted_order = [pid for pid in order if pid in restricted_ids]

    # Collect folder ids within the subtree.
    def _collect_folder_ids(node: _Node) -> list[str]:
        result: list[str] = []
        if node.is_folder and node.folder is not None:
            result.append(node.folder.id)
        for child in node.children:
            result.extend(_collect_folder_ids(child))
        return result

    sub_folder_ids = set(_collect_folder_ids(subtree_node))
    restricted_folder_dirs = {fid: d for fid, d in folder_dirs.items() if fid in sub_folder_ids}

    return LayoutPlan(
        dirs=restricted_dirs,
        files=restricted_files,
        order=restricted_order,
        folder_dirs=restricted_folder_dirs,
        subtree_dir=subtree_dir,
    )
