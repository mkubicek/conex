"""Tree building and path resolution, ported from Go reader's cache.go."""

from __future__ import annotations

from confluence_export.types import Page, PageNode


def build_tree(pages: list[Page]) -> list[PageNode]:
    """Convert a flat list of pages into a tree using parent pointers."""
    node_map: dict[str, PageNode] = {}
    for page in pages:
        node_map[page.id] = PageNode(page=page)

    roots: list[PageNode] = []
    archived_roots: list[PageNode] = []
    for node in node_map.values():
        if not node.page.parent_id:
            if node.page.status == "archived":
                archived_roots.append(node)
            else:
                roots.append(node)
            continue
        parent = node_map.get(node.page.parent_id)
        if parent:
            parent.children.append(node)
        else:
            if node.page.status == "archived":
                archived_roots.append(node)
            else:
                roots.append(node)

    # Group archived root pages under a synthetic _archived node
    if archived_roots:
        archived_page = Page(id="__archived__", title="_archived", status="folder")
        archived_node = PageNode(page=archived_page, children=archived_roots)
        roots.append(archived_node)

    # Sort children by position at every level
    _sort_tree(roots)
    return roots


def _sort_tree(nodes: list[PageNode]) -> None:
    nodes.sort(key=lambda n: n.page.position)
    for node in nodes:
        _sort_tree(node.children)


def find_node_by_id(roots: list[PageNode], page_id: str) -> PageNode | None:
    """Search the tree for a node by page ID."""
    for root in roots:
        if root.page.id == page_id:
            return root
        found = find_node_by_id(root.children, page_id)
        if found:
            return found
    return None


def find_node_by_path(roots: list[PageNode], path: str) -> PageNode | None:
    """Resolve a slash-separated path to a node. Case-insensitive matching."""
    path = path.strip("/")
    if not path:
        return None
    parts = path.split("/")
    return _find_by_parts(roots, parts)


def _find_by_parts(nodes: list[PageNode], parts: list[str]) -> PageNode | None:
    if not parts or not nodes:
        return None
    target = parts[0].lower()
    for node in nodes:
        if node.page.title.lower() == target:
            if len(parts) == 1:
                return node
            return _find_by_parts(node.children, parts[1:])
    return None


def find_pages(pages: list[Page], query: str) -> list[Page]:
    """Search pages by title substring (case-insensitive)."""
    q = query.lower()
    return [p for p in pages if q in p.title.lower()]


def page_path(pages: list[Page], page_id: str) -> str:
    """Build the full slash-separated path from root to the given page."""
    index = {p.id: p for p in pages}
    parts: list[str] = []
    current = page_id
    while current:
        page = index.get(current)
        if not page:
            break
        parts.insert(0, page.title)
        current = page.parent_id
    return "/" + "/".join(parts)


def collect_subtree(node: PageNode) -> list[PageNode]:
    """Collect node and all descendants in pre-order."""
    result = [node]
    for child in node.children:
        result.extend(collect_subtree(child))
    return result


def format_tree(roots: list[PageNode], prefix: str = "") -> str:
    """Format tree as ASCII art, like the Go reader's printTree."""
    lines: list[str] = []
    for i, node in enumerate(roots):
        is_last = i == len(roots) - 1
        connector = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
        child_prefix = prefix + ("    " if is_last else "\u2502   ")
        lines.append(f"{prefix}{connector}{node.page.title}")
        if node.children:
            lines.append(format_tree(node.children, child_prefix))
    return "\n".join(lines)
