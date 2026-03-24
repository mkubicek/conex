"""Compare an existing export directory against current Confluence API state."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from confluence_export.tree import page_path
from confluence_export.types import Page


@dataclass
class ExportedPage:
    page_id: str
    version: int
    title: str
    path: str
    file_path: Path
    space_key: str = ""


@dataclass
class DiffResult:
    new: list[Page] = field(default_factory=list)
    deleted: list[ExportedPage] = field(default_factory=list)
    modified: list[tuple[ExportedPage, Page]] = field(default_factory=list)
    unchanged_count: int = 0


def _parse_frontmatter(file_path: Path) -> dict | None:
    """Read only the YAML frontmatter block between --- delimiters."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if not text.startswith("---"):
        return None

    end = text.find("\n---", 3)
    if end == -1:
        return None

    yaml_block = text[4:end]
    try:
        data = yaml.safe_load(yaml_block)
        if isinstance(data, dict):
            return data
    except yaml.YAMLError:
        pass
    return None


def scan_export_dir(export_dir: Path, space_key: str) -> dict[str, ExportedPage]:
    """Scan .md files in export_dir, return dict keyed by page_id for matching space_key."""
    result: dict[str, ExportedPage] = {}
    skipped_spaces: set[str] = set()

    for md_file in export_dir.rglob("*.md"):
        fm = _parse_frontmatter(md_file)
        if not fm or "page_id" not in fm:
            continue

        file_space = fm.get("space_key", "")
        if file_space.upper() != space_key.upper():
            skipped_spaces.add(file_space)
            continue

        page_id = str(fm["page_id"])
        result[page_id] = ExportedPage(
            page_id=page_id,
            version=int(fm.get("version", 0)),
            title=fm.get("title", ""),
            path=fm.get("path", ""),
            file_path=md_file,
            space_key=file_space,
        )

    if skipped_spaces:
        print(
            f"Warning: skipped files from other space(s): {', '.join(sorted(skipped_spaces))}",
            file=sys.stderr,
        )

    return result


def compute_diff(
    exported: dict[str, ExportedPage], api_pages: list[Page]
) -> DiffResult:
    """Compare exported pages against API pages by page_id + version number."""
    api_by_id = {p.id: p for p in api_pages}
    result = DiffResult()

    for page_id, exp in exported.items():
        api_page = api_by_id.get(page_id)
        if api_page is None:
            result.deleted.append(exp)
        elif api_page.version.number != exp.version:
            result.modified.append((exp, api_page))
        else:
            result.unchanged_count += 1

    for page_id, api_page in api_by_id.items():
        if page_id not in exported:
            result.new.append(api_page)

    return result


def format_diff(result: DiffResult, all_pages: list[Page]) -> str:
    """Format diff result as human-readable text."""
    lines: list[str] = []

    if result.modified:
        lines.append(f"Modified ({len(result.modified)}):")
        for exp, api_page in sorted(result.modified, key=lambda t: t[0].path):
            path = page_path(all_pages, api_page.id)
            lines.append(f"  {path}  (v{exp.version} -> v{api_page.version.number})")

    if result.new:
        lines.append(f"New ({len(result.new)}):")
        for page in sorted(result.new, key=lambda p: page_path(all_pages, p.id)):
            path = page_path(all_pages, page.id)
            lines.append(f"  {path}")

    if result.deleted:
        lines.append(f"Deleted ({len(result.deleted)}):")
        for exp in sorted(result.deleted, key=lambda e: e.path):
            lines.append(f"  {exp.path}")

    lines.append(f"Unchanged: {result.unchanged_count} page(s)")

    return "\n".join(lines)
