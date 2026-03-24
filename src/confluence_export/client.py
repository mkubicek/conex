"""Confluence Cloud REST API v2 client with pagination and retry logic."""

from __future__ import annotations

import sys
import time
from urllib.parse import urlparse, parse_qs

import requests
from requests.auth import HTTPBasicAuth

from confluence_export.config import Config
from confluence_export.types import Attachment, Page, Space


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
        self.session.headers["Authorization"] = f"Bearer {token}"

    def set_cookies(self, cookie_string: str) -> None:
        """Replace current credentials with browser session cookies."""
        self.session.auth = None
        self.session.headers.pop("Authorization", None)
        for pair in cookie_string.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.session.cookies.set(name.strip(), value.strip())

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

    # -- API methods ---------------------------------------------------------

    def get_spaces(self) -> list[Space]:
        results = self._paginate("/wiki/api/v2/spaces", {"limit": "250"})
        return [Space.from_api(r) for r in results]

    def get_pages_in_space(self, space_id: str) -> list[Page]:
        path = f"/wiki/api/v2/spaces/{space_id}/pages"
        results = self._paginate(path, {"limit": "250"})
        return [Page.from_api(r) for r in results]

    def get_page_by_id(self, page_id: str) -> Page:
        data = self._get(f"/wiki/api/v2/pages/{page_id}", {"body-format": "storage"})
        return Page.from_api(data)

    def get_folder_by_id(self, folder_id: str) -> dict | None:
        """Fetch a folder by ID. Returns raw API dict or None on failure."""
        try:
            return self._get(f"/wiki/api/v2/folders/{folder_id}")
        except Exception:
            return None

    def get_attachments(self, page_id: str) -> list[Attachment]:
        path = f"/wiki/api/v2/pages/{page_id}/attachments"
        results = self._paginate(path, {"limit": "250"})
        return [Attachment.from_api(r) for r in results]

    def get_user_info(self, account_id: str) -> dict | None:
        """Resolve an Atlassian account ID to user info (v1 API).

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
