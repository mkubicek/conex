"""Confluence legacy REST API v1 adapter (cookie/browser-session auth).

Endpoints (doc-verified):
  /wiki/rest/api/space/{key}                                 (single space)
  /wiki/rest/api/content?spaceKey=&type=page&status=current  (page listing)
    expand=body.storage,version,ancestors,space,history,extensions
  /wiki/rest/api/content/{id}/child/attachment               (attachments)
    expand=version,metadata,extensions,history
  /wiki/rest/api/user?accountId=<id>                        (user lookup)

Pagination: v1 ALSO follows _links.next (PORT _paginate_offset from
confluence_export/client.py — do NOT implement start/limit arithmetic).
The shared _paginate helper guards against malformed envelopes (null results
or null _links) with `or []` / `(... or {}).get("next")` coalescing.

v1 quirks:
- Numeric ids: id fields arrive as integers; _null_means_default + str() coerce.
- Body lives at body.storage.value (not inline at top level).
- ancestors[-1] is the immediate parent; ancestor type is always "page".
- status=current listing only; include_archived triggers a SECOND call with
  status=archived but ONLY when the caller sets include_archived=True.
  returns_archived == False: the adapter cannot guarantee archived coverage.
- No folder concept: get_folders() always returns [].
- Download URL strategy: same as v2 — prefer the REST endpoint
  /wiki/rest/api/content/{page_id}/child/attachment/{att_id}/download;
  fall back to att.download_url prefixed with /wiki when missing.

COOKIE_V1 parent_type from ancestors is always "page" (v1 REST ancestors
are all pages; folder parents are invisible). Document this in callers.
"""

from __future__ import annotations

import sys
from urllib.parse import parse_qs, quote, urlparse

import requests

from conex.config import ResolvedConfig
from conex.http import Http
from conex.models import Attachment, Folder, Page, PageVersion, Space


class CookieV1API:
    """Adapter for the COOKIE_V1 dialect (legacy REST + browser session cookies).

    Invariants:
    - returns_archived == False: the v1 content endpoint is current-only by
      default; archived pages are only listed when include_archived=True.
    - get_folders() always returns [] — v1 REST has no folder concept; a
      folder-parented page appears as a root in the caller's tree.
    - parent_type from ancestors is always "page" (v1 REST ancestors are all
      pages; folder parents are not visible through this API).
    - Pagination follows _links.next (same guard as v2).
    - Every method returns conex.models objects; no raw dict escapes.
    """

    returns_archived: bool = False

    def __init__(self, cfg: ResolvedConfig) -> None:
        self._cfg = cfg
        from urllib.parse import urlparse
        cookie_host = urlparse(cfg.site_url).hostname or ""
        self._http = Http(auth_headers=cfg.auth_headers, cookie_host=cookie_host)
        self._base = cfg.api_base_url.rstrip("/")

    # -- pagination helpers --------------------------------------------------

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all results following _links.next pagination.

        PORT of confluence_export/client._paginate_offset: v1 also uses
        _links.next for pagination (NOT manual start/limit arithmetic).
        Guards against malformed envelopes with `or` coalescing.
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
            current_url = self._base + parsed.path
            current_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        return all_results

    # -- interface methods ---------------------------------------------------

    def get_space(self, key: str) -> Space:
        """Return the Space matching key.

        Raises ApiError (status=404) propagated from Http when not found.
        """
        data = self._http.get_json(
            self._base + f"/wiki/rest/api/space/{quote(key, safe='')}",
        )
        return _space_from_v1(data)

    def get_pages(
        self,
        space_id: str,
        space_key: str,
        include_archived: bool,
    ) -> list[Page]:
        """Return all pages in the space.

        Always fetches current pages. When include_archived=True a second
        call fetches archived pages (v1 status is single-valued so two
        requests are needed). body_storage is populated from body.storage.value.
        """
        statuses = ["current", "archived"] if include_archived else ["current"]
        pages: list[Page] = []
        for status in statuses:
            rows = self._paginate(
                "/wiki/rest/api/content",
                {
                    "spaceKey": space_key,
                    "type": "page",
                    "status": status,
                    "expand": "body.storage,version,ancestors,space,history,extensions",
                    "limit": "250",
                },
            )
            pages.extend(_page_from_v1(r) for r in rows)
        return pages

    def get_page_body(self, page_id: str) -> str:
        """Fetch the storage-format body for one page via a per-page request."""
        data = self._http.get_json(
            self._base + f"/wiki/rest/api/content/{quote(page_id, safe='')}",
            {"expand": "body.storage"},
        )
        body = (data.get("body") or {})
        storage = (body.get("storage") or {})
        return storage.get("value") or ""

    def get_folders(self, space_id: str, pages: list[Page]) -> list[Folder]:
        """Return [] — v1 REST has no folder concept; warn loudly.

        The legacy cookie/v1 API exposes no folders, so any folder-parented
        page collapses to the space root (the hierarchy is silently flattened).
        Emit a loud stderr warning so the user knows to use an API token for
        the full hierarchy.  *pages* is accepted for protocol parity and unused.
        """
        print(
            "conex: warning: cookie/v1 auth cannot list folders — "
            "folder-parented pages will appear at the space root "
            "(use an API token / v2 dialect for the full hierarchy)",
            file=sys.stderr,
        )
        return []

    def get_attachments(self, page_id: str) -> list[Attachment]:
        """Return all attachments for page_id."""
        rows = self._paginate(
            f"/wiki/rest/api/content/{quote(page_id, safe='')}/child/attachment",
            {"expand": "version,metadata,extensions,history", "limit": "250"},
        )
        return [_attachment_from_v1(r, page_id) for r in rows]

    def get_user_display_name(self, account_id: str) -> str:
        """Return the display name for account_id, or "" on any failure."""
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

        Prefer the REST attachment-download endpoint; fall back to
        att.download_url resolved against api_base_url.
        """
        if att.page_id and att.id:
            return (
                self._base
                + f"/wiki/rest/api/content/{att.page_id}"
                f"/child/attachment/{att.id}/download"
            )
        dl = att.download_url
        if not dl:
            return ""
        if dl.startswith("http://") or dl.startswith("https://"):
            return dl
        if not dl.startswith("/wiki"):
            dl = "/wiki" + dl
        return self._base + dl


# -- model factories ---------------------------------------------------------


def _account_id(user: dict | None) -> str:
    """Extract account/user id from a v1 user dict."""
    if not user:
        return ""
    return str(
        user.get("accountId")
        or user.get("account_id")
        or user.get("userKey")
        or user.get("username")
        or ""
    )


def _version_from_v1(data: dict | None) -> PageVersion:
    if not data:
        return PageVersion()
    author = data.get("by") or {}
    return PageVersion(
        number=data.get("number") or 0,
        created_at=data.get("createdAt") or data.get("when") or "",
        message=data.get("message") or "",
        author_id=_account_id(author),
    )


def _space_from_v1(data: dict) -> Space:
    links = data.get("_links") or {}
    homepage = data.get("homepage") or {}
    homepage_id = str(
        data.get("homepageId")
        or homepage.get("id")
        or ""
    )
    return Space(
        id=str(data.get("id") or ""),
        key=data.get("key") or "",
        name=data.get("name") or "",
        homepage_id=homepage_id,
    )


def _page_from_v1(data: dict) -> Page:
    """Map a v1 content record to a Page model.

    parent_type is always "page" for v1 ancestors (v1 REST ancestors are all
    pages; folder parents are invisible through this API).
    """
    links = data.get("_links") or {}
    body = data.get("body") or {}
    storage = body.get("storage") or {} if body else {}
    space = data.get("space") or {}
    ancestors = data.get("ancestors") or []
    parent = (ancestors[-1] if ancestors else {}) or {}
    extensions = data.get("extensions") or {}
    history = data.get("history") or {}
    return Page(
        id=str(data.get("id") or ""),
        title=data.get("title") or "",
        space_id=str(space.get("id") or ""),
        parent_id=str(parent.get("id") or ""),
        # v1 REST ancestors are always pages; folder parents invisible
        parent_type="page" if parent.get("id") else "",
        position=extensions.get("position") or 0,
        status=data.get("status") or "current",
        body_storage=(storage.get("value") or "") if storage else "",
        version=_version_from_v1(data.get("version")),
        web_url=links.get("webui") or "",
    )


def _attachment_from_v1(data: dict, page_id: str) -> Attachment:
    links = data.get("_links") or {}
    metadata = data.get("metadata") or {}
    extensions = data.get("extensions") or {}
    return Attachment(
        id=str(data.get("id") or ""),
        title=data.get("title") or "",
        media_type=(
            data.get("mediaType")
            or metadata.get("mediaType")
            or extensions.get("mediaType")
            or ""
        ),
        file_size=data.get("fileSize") or extensions.get("fileSize") or 0,
        page_id=page_id,
        download_url=links.get("download") or data.get("downloadLink") or "",
        version=_version_from_v1(data.get("version")),
    )
