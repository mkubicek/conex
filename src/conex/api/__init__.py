"""Confluence API protocol and factory.

The ConfluenceAPI Protocol defines the interface both dialect adapters must
satisfy. make_api dispatches on cfg.dialect and returns the appropriate adapter.

Contract:
- Adapters return MODELS ONLY (conex.models); no raw dicts escape this layer.
- download() accepts only absolute URLs; the adapter builds them.
- get_folders(space_id, pages) discovers folders from the page set (there is no
  "list folders in a space" endpoint); returns [] for COOKIE_V1.
- get_pages() includes body_storage when the dialect supports it in-listing
  (v2 body-format=storage); otherwise body_storage == "" and pull fetches
  them individually via get_page_body().
- returns_archived reflects what the API actually returns, not what the caller
  requested: CLOUD_V2 and GATEWAY_V2 always include archived pages in listings;
  COOKIE_V1 is current-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import requests

from conex.config import Dialect, ResolvedConfig
from conex.models import Attachment, Folder, Page, Space

if TYPE_CHECKING:
    pass


class ConfluenceAPI(Protocol):
    """Protocol all dialect adapters must satisfy.

    Every method returns conex.models objects only; raw API dicts never escape
    the adapter boundary.
    """

    returns_archived: bool
    """False for COOKIE_V1 (current-only listing); True for v2 dialects."""

    def get_space(self, key: str) -> Space:
        """Return the Space for the given space key.

        Raises ApiError(status=404) when no matching space is found.
        """
        ...

    def get_pages(
        self,
        space_id: str,
        space_key: str,
        include_archived: bool,
    ) -> list[Page]:
        """Return all pages in the space.

        For v2 dialects, body_storage is populated inline (body-format=storage).
        For COOKIE_V1, body_storage is "" — callers must invoke get_page_body.
        include_archived is ignored for COOKIE_V1 (always current-only).
        """
        ...

    def get_page_body(self, page_id: str) -> str:
        """Return the storage-format XHTML body for one page.

        Used by pull for dialects that do not include bodies in-listing.
        """
        ...

    def get_folders(self, space_id: str, pages: list[Page]) -> list[Folder]:
        """Return the folders that appear as ancestors of *pages*.

        The Confluence v2 API has no "list folders in a space" endpoint, so
        folders are discovered from the page set: any page with
        ``parent_type == "folder"`` has a folder parent, fetched via
        ``GET /folders/{id}`` and recursed (a folder's parent may itself be a
        folder).  Returns [] for COOKIE_V1 (v1 REST has no folder concept).
        """
        ...

    def get_attachments(self, page_id: str) -> list[Attachment]:
        """Return all attachments for the given page."""
        ...

    def get_user_display_name(self, account_id: str) -> str:
        """Return the display name for an Atlassian account, or "" if unknown."""
        ...

    def download(self, url: str) -> requests.Response:
        """Stream-download the resource at url; caller must close the response.

        url must be absolute. The adapter builds absolute download URLs;
        pull never constructs them.
        """
        ...

    def attachment_download_url(self, att: Attachment) -> str:
        """Return an absolute download URL for att.

        Prefer the REST attachment-download endpoint (works on site AND
        gateway — PORT v1 media._download_one's strategy); fall back to
        att.download_url resolved against api_base_url.  Returns "" when
        no viable URL can be constructed; pull skips the download and warns.
        Used by pull.py — pull never builds URLs itself.
        """
        ...


def make_api(cfg: ResolvedConfig) -> ConfluenceAPI:
    """Construct the appropriate dialect adapter for cfg.

    Dispatches on cfg.dialect:
    - CLOUD_V2   -> CloudV2API (site URL, api-token/PAT auth)
    - GATEWAY_V2 -> CloudV2API (gateway URL, same v2 surface)
    - COOKIE_V1  -> CookieV1API (legacy REST, browser cookies)
    """
    if cfg.dialect in (Dialect.CLOUD_V2, Dialect.GATEWAY_V2):
        from conex.api.v2 import CloudV2API

        return CloudV2API(cfg)
    if cfg.dialect is Dialect.COOKIE_V1:
        from conex.api.v1 import CookieV1API

        return CookieV1API(cfg)
    raise ValueError(f"Unknown dialect: {cfg.dialect!r}")  # pragma: no cover
