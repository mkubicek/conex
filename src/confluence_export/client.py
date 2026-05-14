"""Confluence Cloud REST API v2 client with pagination and retry logic."""

from __future__ import annotations

import sys
import time
from urllib.parse import parse_qs, quote, urlparse

import requests
from requests.auth import HTTPBasicAuth

from confluence_export.config import Config
from confluence_export.types import Attachment, Page, Space, Version


class AuthenticationError(Exception):
    """Raised when the server returns 401 or 403."""

    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}")


class ConfluenceClient:
    """Thin wrapper around Confluence Cloud REST API v2.

    Thread-safe for concurrent .get() calls. urllib3's default connection pool
    size is 10, which accommodates the 8-worker thread pools used by callers.
    """

    def __init__(self, config: Config, verbose: bool = False):
        self.base_url = config.base_url
        self.verbose = verbose

        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.session.timeout = 30
        self.api_flavor = "v2"
        self._space_key_by_id: dict[str, str] = {}

        if config.api_token:
            if config.use_bearer:
                # PAT-only: use Bearer token auth
                self.session.headers["Authorization"] = f"Bearer {config.api_token}"
                self._log("Using Bearer token auth (PAT)")
            else:
                # Email + API token: use Basic Auth
                self.session.auth = HTTPBasicAuth(config.email, config.api_token)
                self._log(f"Using Basic Auth with email: {config.email}")
        else:
            self._log("No credentials configured — browser token required")

    # -- low-level helpers ---------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[debug] {msg}", file=sys.stderr)

    def set_bearer_token(self, token: str) -> None:
        """Replace current credentials with a Bearer token."""
        self.session.auth = None
        self.session.cookies.clear()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.api_flavor = "v2"

    def set_cookies(self, cookie_string: str) -> None:
        """Replace current credentials with browser session cookies."""
        self.session.auth = None
        self.session.headers.pop("Authorization", None)
        self.session.cookies.clear()
        for pair in cookie_string.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.session.cookies.set(name.strip(), value.strip())
        self.api_flavor = "cookie_v1"

    def verify_auth(self) -> None:
        """Verify current credentials with a minimal request."""
        if self.api_flavor == "cookie_v1":
            self._get("/wiki/rest/api/space", {"limit": "1"})
        else:
            self._get("/wiki/api/v2/spaces", {"limit": "1"})

    def _get(self, path: str, params: dict | None = None, max_retries: int = 3) -> dict:
        """GET with retry + rate-limit handling."""
        url = self.base_url + path
        for attempt in range(max_retries):
            try:
                self._log(f"GET {url} params={params}")
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code
                if status in (401, 403):
                    raise AuthenticationError(status, url) from exc
                if status == 429:
                    retry_after = int(exc.response.headers.get("Retry-After", 60))
                    self._log(f"Rate limited, waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue
                if status >= 500 and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    self._log(f"Server error {status}, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise
            except requests.exceptions.ConnectionError:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    self._log(f"Connection error, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Max retries exceeded for {url}")

    def _get_raw(self, path: str) -> requests.Response:
        """GET returning raw response (for file downloads)."""
        url = self.base_url + path
        self._log(f"GET (raw) {url}")
        resp = self.session.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        return resp

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages of results using cursor-based pagination."""
        all_results: list[dict] = []
        current_path = path
        current_params = dict(params) if params else {}

        while True:
            data = self._get(current_path, current_params)
            all_results.extend(data.get("results", []))

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break

            # next_link is a relative URL like /wiki/api/v2/...?cursor=...
            parsed = urlparse(next_link)
            current_path = parsed.path
            current_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        return all_results

    def _paginate_offset(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages of results using v1 start/limit pagination."""
        all_results: list[dict] = []
        current_path = path
        current_params = dict(params) if params else {}

        while True:
            data = self._get(current_path, current_params)
            all_results.extend(data.get("results", []))

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break

            parsed = urlparse(next_link)
            current_path = parsed.path
            current_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        return all_results

    @staticmethod
    def _account_id(user: dict | None) -> str:
        if not user:
            return ""
        return str(
            user.get("accountId")
            or user.get("account_id")
            or user.get("userKey")
            or user.get("username")
            or ""
        )

    @classmethod
    def _version_from_v1(cls, data: dict | None) -> Version:
        if not data:
            return Version()
        return Version(
            created_at=data.get("createdAt", "") or data.get("when", ""),
            message=data.get("message", ""),
            number=data.get("number", 0),
            minor_edit=data.get("minorEdit", False),
            author_id=cls._account_id(data.get("by")),
        )

    def _remember_space(self, space: Space) -> Space:
        if space.id and space.key:
            self._space_key_by_id[space.id] = space.key
        return space

    def _space_key_for_v1(self, space_id: str) -> str:
        if space_id in self._space_key_by_id:
            return self._space_key_by_id[space_id]

        for space in self.get_spaces():
            if space.id == space_id:
                return space.key

        # Last-resort fallback for direct tests/callers that pass a key.
        return space_id

    def _space_from_v1(self, data: dict) -> Space:
        links = data.get("_links", {})
        homepage = data.get("homepage") or {}
        return self._remember_space(
            Space(
                id=str(data.get("id", "")),
                key=data.get("key", ""),
                name=data.get("name", ""),
                type=data.get("type", ""),
                status=data.get("status", ""),
                homepage_id=str(data.get("homepageId", "") or homepage.get("id", "")),
                webui=links.get("webui", ""),
                base=links.get("base", ""),
            )
        )

    def _page_from_v1(self, data: dict) -> Page:
        links = data.get("_links", {})
        body = data.get("body", {})
        storage = body.get("storage", {}) if body else {}
        space = data.get("space") or {}
        history = data.get("history") or {}
        ancestors = data.get("ancestors") or []
        parent = ancestors[-1] if ancestors else {}
        extensions = data.get("extensions") or {}
        return Page(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            space_id=str(space.get("id", "")),
            parent_id=str(parent.get("id", "") or ""),
            parent_type=parent.get("type", ""),
            position=extensions.get("position", 0) or 0,
            status=data.get("status", ""),
            author_id=self._account_id(history.get("createdBy")),
            created_at=history.get("createdDate", ""),
            version=self._version_from_v1(data.get("version")),
            body_storage=storage.get("value", "") if storage else "",
            webui=links.get("webui", ""),
            editui=links.get("editui", ""),
            tinyui=links.get("tinyui", ""),
        )

    def _folder_from_v1(self, data: dict) -> dict:
        ancestors = data.get("ancestors") or []
        parent = ancestors[-1] if ancestors else {}
        space = data.get("space") or {}
        extensions = data.get("extensions") or {}
        return {
            "id": str(data.get("id", "")),
            "title": data.get("title", ""),
            "spaceId": str(space.get("id", "")),
            "parentId": str(parent.get("id", "") or ""),
            "parentType": parent.get("type", ""),
            "position": extensions.get("position", 0) or 0,
            "status": "folder",
        }

    def _attachment_from_v1(self, data: dict, page_id: str) -> Attachment:
        links = data.get("_links", {})
        metadata = data.get("metadata") or {}
        extensions = data.get("extensions") or {}
        history = data.get("history") or {}
        version = self._version_from_v1(data.get("version"))
        return Attachment(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            media_type=(
                data.get("mediaType", "")
                or metadata.get("mediaType", "")
                or extensions.get("mediaType", "")
            ),
            media_type_description=(
                data.get("mediaTypeDescription", "")
                or metadata.get("mediaTypeDescription", "")
            ),
            file_size=data.get("fileSize", 0) or extensions.get("fileSize", 0) or 0,
            page_id=page_id,
            comment=data.get("comment", ""),
            created_at=history.get("createdDate", "") or version.created_at,
            version=version,
            download_link=links.get("download", "") or data.get("downloadLink", ""),
            webui=links.get("webui", ""),
        )

    # -- API methods ---------------------------------------------------------

    def get_spaces(self) -> list[Space]:
        if self.api_flavor == "cookie_v1":
            results = self._paginate_offset("/wiki/rest/api/space", {"limit": "250"})
            return [self._space_from_v1(r) for r in results]

        results = self._paginate("/wiki/api/v2/spaces", {"limit": "250"})
        return [self._remember_space(Space.from_api(r)) for r in results]

    def get_pages_in_space(self, space_id: str, include_archived: bool = False) -> list[Page]:
        if self.api_flavor == "cookie_v1":
            space_key = self._space_key_for_v1(space_id)
            # v1 status is single-valued, so archived pages need a second call.
            statuses = ["current", "archived"] if include_archived else ["current"]
            pages: list[Page] = []
            for status in statuses:
                results = self._paginate_offset(
                    "/wiki/rest/api/content",
                    {
                        "spaceKey": space_key,
                        "type": "page",
                        "status": status,
                        "expand": "body.storage,version,ancestors,space,history,extensions",
                        "limit": "250",
                    },
                )
                pages.extend(self._page_from_v1(r) for r in results)
            return pages

        # body-format=storage returns the body inline with every page, so the
        # exporter doesn't need an N+1 round trip to fetch each body later.
        # v2 defaults to status=current,archived; no flag-gating needed.
        path = f"/wiki/api/v2/spaces/{space_id}/pages"
        results = self._paginate(path, {"limit": "250", "body-format": "storage"})
        return [Page.from_api(r) for r in results]

    def get_space_by_key(self, key: str) -> Space | None:
        """Look up a single space by key using the server-side `keys` filter."""
        if self.api_flavor == "cookie_v1":
            try:
                data = self._get(f"/wiki/rest/api/space/{quote(key, safe='')}")
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    return None
                raise
            return self._space_from_v1(data)

        data = self._get("/wiki/api/v2/spaces", {"keys": key, "limit": "1"})
        results = data.get("results", [])
        if not results:
            return None
        return self._remember_space(Space.from_api(results[0]))

    def get_page_by_id(self, page_id: str) -> Page:
        if self.api_flavor == "cookie_v1":
            data = self._get(
                f"/wiki/rest/api/content/{quote(page_id, safe='')}",
                {"expand": "body.storage,version,ancestors,space,history,extensions"},
            )
            return self._page_from_v1(data)

        data = self._get(f"/wiki/api/v2/pages/{page_id}", {"body-format": "storage"})
        return Page.from_api(data)

    def get_folder_by_id(self, folder_id: str) -> dict | None:
        """Fetch a folder by ID. Returns raw API dict or None on failure."""
        try:
            if self.api_flavor == "cookie_v1":
                data = self._get(
                    f"/wiki/rest/api/content/{quote(folder_id, safe='')}",
                    {"expand": "ancestors,space,extensions"},
                )
                return self._folder_from_v1(data)
            return self._get(f"/wiki/api/v2/folders/{folder_id}")
        except Exception:
            return None

    def get_attachments(self, page_id: str) -> list[Attachment]:
        if self.api_flavor == "cookie_v1":
            path = f"/wiki/rest/api/content/{quote(page_id, safe='')}/child/attachment"
            results = self._paginate_offset(
                path, {"expand": "version,metadata,extensions,history", "limit": "250"}
            )
            return [self._attachment_from_v1(r, page_id) for r in results]

        path = f"/wiki/api/v2/pages/{page_id}/attachments"
        results = self._paginate(path, {"limit": "250"})
        return [Attachment.from_api(r) for r in results]

    def get_user_info(self, account_id: str) -> dict | None:
        """Resolve an Atlassian account ID to user info (v1 API).

        The v2 API has no GET-by-accountId — the v2 equivalent is
        POST /wiki/api/v2/users-bulk with an accountIds array, which is
        awkward for the single-ID + cache pattern we use here. v1 remains
        supported and is the canonical single-user lookup path.

        Returns dict with 'displayName' and optionally 'email', or None on failure.
        """
        try:
            data = self._get("/wiki/rest/api/user", {"accountId": account_id})
            result = {"displayName": data.get("displayName") or data.get("publicName", "")}
            if data.get("email"):
                result["email"] = data["email"]
            return result
        except Exception:
            return None

    def download_attachment(self, download_path: str) -> bytes:
        """Download attachment content. download_path is the _links.download value."""
        resp = self._get_raw(download_path)
        try:
            return resp.content
        finally:
            resp.close()

    def download_attachment_to_file(self, download_path: str, dest: str) -> int:
        """Stream attachment to a file. Returns bytes written."""
        resp = self._get_raw(download_path)
        try:
            written = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    written += len(chunk)
            return written
        finally:
            resp.close()
