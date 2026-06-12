"""Confluence Cloud REST API v2 adapter (also handles GATEWAY_V2).

Endpoints (doc-verified):
  /wiki/api/v2/spaces?keys=<key>
  /wiki/api/v2/spaces/{id}/pages?body-format=storage   (cursor pagination)
  /wiki/api/v2/spaces/{id}/folders                     (cursor pagination)
  /wiki/api/v2/pages/{id}/attachments                  (cursor pagination)
  /wiki/api/v2/pages/{id}?body-format=storage          (per-page body fetch)
  /wiki/rest/api/user?accountId=<id>                   (user lookup, v1 path)

Pagination: all list endpoints use cursor-based pagination via _links.next.
The shared _paginate helper guards against malformed envelopes (null results
or null _links) with `or []` / `(... or {}).get("next")` coalescing.

Download URL strategy (PORT confluence_export/media._download_one):
  Prefer: <api_base_url>/wiki/rest/api/content/{page_id}/child/attachment/{att_id}/download
  Fallback: Attachment.download_url resolved against api_base_url
            (prefix /wiki when not already present)

GATEWAY_V2 uses the same v2 surface but addressed via the gateway base URL
(https://api.atlassian.com/ex/confluence/{cloudId}), so no special-casing
is needed beyond cfg.api_base_url already pointing to the right host.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import requests

from conex.config import ResolvedConfig
from conex.errors import ApiError
from conex.http import Http
from conex.models import Attachment, Folder, Page, PageVersion, Space


class CloudV2API:
    """Adapter for CLOUD_V2 and GATEWAY_V2 dialects.

    Invariants:
    - returns_archived == True: v2 listings include both current and archived
      pages by default (no status filter needed).
    - Every method returns conex.models objects; no raw dict escapes.
    - Pagination guards: results-or-[] and (_links or {}).get("next") prevent
      crashes on malformed API envelopes.
    """

    returns_archived: bool = True

    def __init__(self, cfg: ResolvedConfig) -> None:
        self._cfg = cfg
        self._http = Http(auth_headers=cfg.auth_headers)
        self._base = cfg.api_base_url.rstrip("/")

    # -- pagination helpers --------------------------------------------------

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all cursor-paginated results from path.

        Guards against malformed envelopes: a None results or _links field
        must not crash; coerce with `or`.
        """
        all_results: list[dict] = []
        current_url = self._base + path
        current_params: dict | None = params

        while True:
            data: dict = self._http.get_json(current_url, current_params)
            all_results.extend(data.get("results") or [])

            next_link = (data.get("_links") or {}).get("next")
            if not next_link:
                break

            parsed = urlparse(next_link)
            # next_link is site-relative (e.g. /wiki/api/v2/...?cursor=...)
            current_url = self._base + parsed.path
            current_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        return all_results

    # -- interface methods ---------------------------------------------------

    def get_space(self, key: str) -> Space:
        """Return the Space matching key.

        Raises ApiError(status=404) when no space is found.
        """
        data = self._http.get_json(
            self._base + "/wiki/api/v2/spaces",
            {"keys": key, "limit": "1"},
        )
        results = (data.get("results") or [])
        if not results:
            raise ApiError(
                f"Space with key {key!r} not found",
                status=404,
                url=self._base + "/wiki/api/v2/spaces",
            )
        return _space_from_v2(results[0])

    def get_pages(
        self,
        space_id: str,
        space_key: str,
        include_archived: bool,
    ) -> list[Page]:
        """Return all pages in space_id, bodies included inline.

        include_archived is accepted but ignored: v2 listings always include
        both current and archived pages.
        """
        path = f"/wiki/api/v2/spaces/{space_id}/pages"
        rows = self._paginate(path, {"limit": "250", "body-format": "storage"})
        return [_page_from_v2(r) for r in rows]

    def get_page_body(self, page_id: str) -> str:
        """Fetch the storage-format XHTML body for one page."""
        data = self._http.get_json(
            self._base + f"/wiki/api/v2/pages/{page_id}",
            {"body-format": "storage"},
        )
        body = (data.get("body") or {})
        storage = (body.get("storage") or {})
        return storage.get("value") or ""

    def get_folders(self, space_id: str) -> list[Folder]:
        """Return all folders in the space."""
        path = f"/wiki/api/v2/spaces/{space_id}/folders"
        rows = self._paginate(path, {"limit": "250"})
        return [_folder_from_v2(r) for r in rows]

    def get_attachments(self, page_id: str) -> list[Attachment]:
        """Return all attachments for page_id."""
        path = f"/wiki/api/v2/pages/{page_id}/attachments"
        rows = self._paginate(path, {"limit": "250"})
        return [_attachment_from_v2(r, page_id) for r in rows]

    def get_user_display_name(self, account_id: str) -> str:
        """Return the display name for account_id, or "" on any failure.

        Uses the v1 /wiki/rest/api/user endpoint (the v2 equivalent is a
        bulk POST; the v1 path is the canonical single-account lookup).
        """
        try:
            data = self._http.get_json(
                self._base + "/wiki/rest/api/user",
                {"accountId": account_id},
            )
            return str(data.get("displayName") or data.get("publicName") or "")
        except Exception:
            return ""

    def download(self, url: str) -> requests.Response:
        """Stream-download url; the caller must close the response."""
        return self._http.get_stream(url)

    # -- URL builder used by pull.py -----------------------------------------

    def attachment_download_url(self, att: Attachment) -> str:
        """Build an absolute download URL for att.

        Prefer the REST attachment-download endpoint (works on site AND
        gateway); fall back to att.download_url resolved against api_base_url.
        """
        if att.page_id and att.id:
            return (
                self._base
                + f"/wiki/rest/api/content/{att.page_id}"
                f"/child/attachment/{att.id}/download"
            )
        # Fallback: resolve download_url against api_base_url
        dl = att.download_url
        if not dl:
            return ""
        if dl.startswith("http://") or dl.startswith("https://"):
            return dl
        if not dl.startswith("/wiki"):
            dl = "/wiki" + dl
        return self._base + dl


# -- model factories ---------------------------------------------------------


def _version_from_v2(data: dict | None) -> PageVersion:
    if not data:
        return PageVersion()
    author = (data.get("author") or {})
    return PageVersion(
        number=data.get("number") or 0,
        created_at=data.get("createdAt") or "",
        message=data.get("message") or "",
        author_id=str(author.get("accountId") or ""),
    )


def _space_from_v2(data: dict) -> Space:
    links = data.get("_links") or {}
    homepage = data.get("homepageId") or ""
    return Space(
        id=str(data.get("id") or ""),
        key=data.get("key") or "",
        name=data.get("name") or "",
        homepage_id=str(homepage),
    )


def _page_from_v2(data: dict) -> Page:
    body = (data.get("body") or {})
    storage = (body.get("storage") or {})
    parent_type = data.get("parentType") or ""
    links = data.get("_links") or {}
    web_ui = links.get("webui") or ""
    # Construct absolute web_url from webui when a base is present
    # (the adapter has no site_url context here, so store webui as-is;
    # callers that need absolute URLs use cfg.site_url).
    return Page(
        id=str(data.get("id") or ""),
        title=data.get("title") or "",
        space_id=str(data.get("spaceId") or ""),
        parent_id=str(data.get("parentId") or ""),
        parent_type=parent_type,
        position=data.get("position") or 0,
        status=data.get("status") or "current",
        body_storage=storage.get("value") or "",
        version=_version_from_v2(data.get("version")),
        web_url=web_ui,
    )


def _folder_from_v2(data: dict) -> Folder:
    return Folder(
        id=str(data.get("id") or ""),
        title=data.get("title") or "",
        parent_id=str(data.get("parentId") or ""),
        position=data.get("position") or 0,
    )


def _attachment_from_v2(data: dict, page_id: str) -> Attachment:
    links = data.get("_links") or {}
    download_url = links.get("download") or ""
    return Attachment(
        id=str(data.get("id") or ""),
        title=data.get("title") or "",
        media_type=data.get("mediaType") or "",
        file_size=data.get("fileSize") or 0,
        page_id=page_id,
        download_url=download_url,
        version=_version_from_v2(data.get("version")),
    )
