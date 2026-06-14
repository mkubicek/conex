"""conex.convert — storage XHTML → Markdown conversion.

Public API::

    CONVERTER_VERSION: int    # bump to invalidate incremental skip
    MediaRefs                 # per-page attachment-name resolver
    ConvertContext            # conversion context dataclass
    convert_page(body, ctx)   # storage XHTML → markdown body
    build_frontmatter(...)    # YAML frontmatter string

``convert_page`` runs the 8-pass pipeline in ``render.py`` and assembles the
final markdown string with the YAML frontmatter block prepended.

``MediaRefs`` wraps an ``AttachmentNamePlan`` (from ``conex.paths``) and
implements by-id and by-title resolution matching the v1 ``for_reference``
semantics exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import yaml

from conex.models import Attachment, Page, Space
from conex.paths import AttachmentNamePlan, plan_attachment_names

# ---------------------------------------------------------------------------
# Public constant
# ---------------------------------------------------------------------------

CONVERTER_VERSION: int = 1
"""Bump this to invalidate all incremental-skip fingerprints on next build."""


# ---------------------------------------------------------------------------
# MediaRefs
# ---------------------------------------------------------------------------


class MediaRefs:
    """Per-page attachment-name resolver.

    Built by build.py from ONE AttachmentNamePlan per page (the SAME plan
    that names the files on disk), so links can never desync from filenames.

    Storage XML references attachments by TITLE/filename, not id.  Resolution
    order (PORT v1 ``AttachmentNamePlan.for_reference`` semantics):
    1. by attachment id (``ri:content-id`` / ``ri:contentId`` etc.)
    2. exact title match
    3. NFC-casefold title fallback
    4. fresh safe_attachment_name sanitisation (safe for unknown titles)
    """

    def __init__(self, plan: AttachmentNamePlan) -> None:
        self._plan = plan

    @classmethod
    def from_attachments(cls, attachments: list[Attachment]) -> "MediaRefs":
        """Build a MediaRefs from a list of Attachment models."""
        return cls(plan_attachment_names(attachments))

    def filename_for_id(self, att_id: str) -> str | None:
        """Return the local filename for the given attachment id, or None."""
        return self._plan.by_id.get(att_id)

    def filename_for_title(self, title: str) -> str | None:
        """Return the local filename for the given title.

        Tries exact title, then NFC-casefold, then falls back to a fresh
        sanitisation (never returns None — safe for any title string).
        """
        return self._plan.for_reference(title, None)


# ---------------------------------------------------------------------------
# ConvertContext
# ---------------------------------------------------------------------------


@dataclass
class ConvertContext:
    """All per-page context needed by the conversion pipeline.

    ``media_available`` contains filenames that build.py confirmed are
    present-and-owned THIS run for THIS page.  It is never a raw os.listdir
    of .media/.

    ``media_enabled`` is False on ``--no-media`` runs; when False,
    attachment references always degrade to "missing attachment" notes.
    """

    page: Page
    space: Space
    site_url: str
    attachments: list[Attachment]
    media: MediaRefs
    rendered_drawio: dict[str, str]
    resolve_user: Callable[[str], str]
    media_enabled: bool = True
    media_available: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# convert_page
# ---------------------------------------------------------------------------


def convert_page(body_storage: str, ctx: ConvertContext) -> str:
    """Convert storage-format XHTML to a markdown string (body only).

    The returned string does NOT include YAML frontmatter; callers that need
    a complete .md file should prepend ``build_frontmatter(...)`` output.

    The pipeline is the 8-pass render.preprocess_storage_xhtml + pass 8
    (markdownify + whitespace normalisation + single-H1 rule).
    """
    from conex.convert.render import preprocess_to_soup, _pass_markdownify

    # Hand the live soup straight to markdownify — no serialize + re-parse round
    # trip (the dominant per-page CPU cost at 10k+ page scale).
    soup = preprocess_to_soup(body_storage, ctx)
    return _pass_markdownify(soup, ctx.page.title)


# ---------------------------------------------------------------------------
# build_frontmatter
# ---------------------------------------------------------------------------


def build_frontmatter(
    page: Page,
    space: Space,
    human_path: str,
    site_url: str,
    attachments: list[Attachment] | None = None,
) -> str:
    """Build the YAML frontmatter block for a page's markdown file.

    Shape (v1 parity):
    - title, page_id, space_key, path, url, last_modified, version
    - status: archived — only when page.status == "archived"
    - attachments: [{name, type, size}, …] — only when the page has attachments

    Returns a string of the form ``---\\n<yaml>---\\n\\n``.
    """
    url = ""
    if site_url and page.web_url:
        if page.web_url.startswith("http"):
            url = page.web_url
        else:
            # v2 stores the API `_links.webui` path (e.g. "/spaces/SP/pages/123"),
            # which is relative to the Confluence app root at /wiki — v1 emitted
            # f"{base}/wiki{webui}".  Prepend /wiki for a clickable link.
            path = page.web_url if page.web_url.startswith("/") else "/" + page.web_url
            if not path.startswith("/wiki/") and path != "/wiki":
                path = "/wiki" + path
            url = f"{site_url.rstrip('/')}{path}"

    meta: dict = {
        "title": page.title,
        "page_id": page.id,
        "space_key": space.key,
        "path": human_path,
        "url": url,
        "last_modified": page.version.created_at,
        "version": page.version.number,
    }
    if page.status == "archived":
        meta["status"] = "archived"

    if attachments:
        meta["attachments"] = [
            {"name": a.title, "type": a.media_type, "size": a.file_size}
            for a in attachments
        ]

    yaml_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_str}---\n\n"
