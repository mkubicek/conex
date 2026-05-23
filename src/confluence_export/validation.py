"""Markdown export validation and diagnostic formatting helpers."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from confluence_export.types import ExportDiagnostic, Page

_DRAWIO_SENTINEL_RE = re.compile(r"\\?\[drawio:[^\]]+\\?\]")
_INTERNAL_PLACEHOLDER_RES = [
    re.compile(r"__CONFLUENCE_EXPORT_[A-Z0-9_]+__"),
    re.compile(r"CONFLUENCE_EXPORT_INTERNAL"),
    re.compile(r"@@CONFLUENCE_EXPORT_[^@]+@@"),
]
_AUTOLINK_MEDIA_REF_RE = re.compile(r"<(\.media/[^>\n]+)>")
_HTML_MEDIA_REF_RE = re.compile(r"""(?:src|href)=["'](\.media/[^"']+)["']""")
_REQUIRED_FRONTMATTER = {"title", "page_id", "space_key", "path"}


def validate_markdown(
    markdown: str,
    md_path: Path,
    page: Page,
    *,
    validate_media_refs: bool = True,
    generated_media_paths: set[Path] | None = None,
) -> list[ExportDiagnostic]:
    """Validate one exported markdown page and return structured diagnostics."""
    diagnostics: list[ExportDiagnostic] = []

    def add(code: str, message: str) -> None:
        diagnostics.append(
            ExportDiagnostic(
                severity="error",
                page_id=page.id,
                page_title=page.title,
                code=code,
                message=message,
                path=md_path,
            )
        )

    frontmatter, body = _split_frontmatter(markdown)
    if frontmatter is None:
        add("invalid_frontmatter", "missing or invalid YAML frontmatter")
    else:
        try:
            parsed = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            add("invalid_frontmatter", f"frontmatter is not valid YAML: {exc}")
            parsed = {}
        if not isinstance(parsed, dict):
            add("invalid_frontmatter", "frontmatter must be a mapping")
        else:
            missing = sorted(k for k in _REQUIRED_FRONTMATTER if not parsed.get(k))
            if missing:
                add(
                    "invalid_frontmatter",
                    f"frontmatter missing required field(s): {', '.join(missing)}",
                )

    content = body if frontmatter is not None else markdown
    if page.status != "folder" and not content.strip():
        add("empty_markdown", "non-folder page produced empty markdown")

    searchable_markdown = _strip_code_for_placeholder_checks(markdown)
    if _DRAWIO_SENTINEL_RE.search(searchable_markdown):
        add(
            "drawio_sentinel_leaked",
            "unresolved internal draw.io sentinel leaked into markdown",
        )

    for pattern in _INTERNAL_PLACEHOLDER_RES:
        if pattern.search(searchable_markdown):
            add(
                "internal_placeholder_leaked",
                "unresolved internal implementation placeholder leaked into markdown",
            )
            break

    generated_media_paths = generated_media_paths or set()
    for path in generated_media_paths:
        if not path.exists():
            add(
                "missing_generated_media",
                f"referenced generated media is missing: {path.name}",
            )

    if validate_media_refs:
        for ref in sorted(_extract_media_refs(markdown)):
            ref_path = (md_path.parent / ref).resolve()
            if not ref_path.exists():
                add(
                    "missing_media",
                    f"markdown references missing media {ref}",
                )

    return diagnostics


def _strip_code_for_placeholder_checks(markdown: str) -> str:
    """Remove markdown code spans/blocks before implementation placeholder checks."""
    without_fences = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    without_fences = re.sub(r"~~~.*?~~~", "", without_fences, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", without_fences)


def _split_frontmatter(markdown: str) -> tuple[str | None, str]:
    if not markdown.startswith("---\n"):
        return None, markdown

    end = markdown.find("\n---", 4)
    if end == -1:
        return None, markdown

    frontmatter = markdown[4:end]
    body = markdown[end + 4 :].strip()
    return frontmatter, body


def _extract_media_refs(markdown: str) -> set[str]:
    refs: set[str] = set()
    refs.update(_extract_markdown_media_refs(markdown))
    for match in _AUTOLINK_MEDIA_REF_RE.finditer(markdown):
        refs.add(_clean_media_ref(match.group(1)))
    for match in _HTML_MEDIA_REF_RE.finditer(markdown):
        refs.add(_clean_media_ref(match.group(1)))
    return refs


def _extract_markdown_media_refs(markdown: str) -> set[str]:
    refs: set[str] = set()
    i = 0
    while i < len(markdown):
        link_start = markdown.find("](", i)
        if link_start == -1:
            break
        ref_start = link_start + 2
        if not markdown.startswith(".media/", ref_start):
            i = ref_start
            continue

        ref_end = _find_markdown_link_end(markdown, ref_start)
        if ref_end == -1:
            i = ref_start
            continue
        refs.add(_clean_media_ref(markdown[ref_start:ref_end]))
        i = ref_end + 1
    return refs


def _find_markdown_link_end(markdown: str, start: int) -> int:
    depth = 0
    escaped = False
    for pos in range(start, len(markdown)):
        char = markdown[pos]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            if depth == 0:
                return pos
            depth -= 1
    return -1


def _clean_media_ref(ref: str) -> str:
    ref = ref.strip().strip("<>").strip()
    if re.search(r"\s+['\"][^'\"]*['\"]$", ref):
        ref = ref.rsplit(" ", 1)[0]
    return ref.split("#", 1)[0].split("?", 1)[0]
