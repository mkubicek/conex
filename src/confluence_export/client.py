"""Confluence Cloud REST API v2 client with pagination and retry logic."""

from __future__ import annotations

import math
import random
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlparse

import requests
from requests.auth import HTTPBasicAuth

from confluence_export.config import (
    ApiDialect,
    AuthConfig,
    AuthMode,
    Config,
    ConnectionProfile,
)
from confluence_export.types import Attachment, Page, Space, Version

# Longest Retry-After a server/proxy can impose on the shared backoff window.
# Real throttling waits are seconds to minutes; anything longer is a hostile
# or buggy header that would otherwise stall a CLI export for hours.
_RETRY_AFTER_CAP_S = 300.0

# Wait used when the header is absent or junk (unparseable, non-finite,
# negative). One constant for all three fallback sites so they cannot drift.
_RETRY_AFTER_DEFAULT_S = 60.0


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

    def __init__(self, config: Config | ConnectionProfile, verbose: bool = False):
        if isinstance(config, ConnectionProfile):
            profile = config
        else:
            profile = ConnectionProfile(
                site_url=config.base_url,
                api_base_url=config.base_url,
                cloud_id=None,
                auth_mode=AuthMode.BEARER_PAT if config.use_bearer else AuthMode.BASIC_API_TOKEN,
                api_dialect=ApiDialect.CLOUD_V2,
                config_source="legacy Config",
                interactive=True,
                auth=AuthConfig(
                    type=AuthMode.BEARER_PAT if config.use_bearer else AuthMode.BASIC_API_TOKEN,
                    email=config.email,
                    token=config.api_token,
                ),
            )

        self.profile = profile
        self.base_url = profile.api_base_url
        self.verbose = verbose

        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.session.timeout = 30
        self.api_dialect = profile.api_dialect
        self._space_key_by_id: dict[str, str] = {}

        # Shared rate-limit coordination (#39): a 429 on any thread sets a window
        # all threads honor before their next request, so one rate-limit response
        # backs off the whole worker pool instead of every worker re-hammering.
        self._rate_lock = threading.Lock()
        self._rate_limit_until = 0.0  # monotonic time; requests wait until it passes
        self.stats: dict[str, float] = {
            "requests": 0,
            "retries": 0,
            "rate_limit_sleep_s": 0.0,
        }

        auth = profile.auth
        if profile.auth_mode is AuthMode.COOKIE and auth.cookie_header:
            self._set_cookie_header(auth.cookie_header)
            self._log("Using browser cookie auth")
        elif profile.auth_mode is AuthMode.BEARER_PAT and auth.token:
            self.session.headers["Authorization"] = f"Bearer {auth.token}"
            self._log("Using Bearer token auth (PAT)")
        elif auth.token:
            self.session.auth = HTTPBasicAuth(auth.email, auth.token)
            self._log(f"Using Basic Auth with email: {auth.email}")
        else:
            self._log("No credentials configured — browser token required")

    # -- low-level helpers ---------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[debug] {msg}", file=sys.stderr)

    @property
    def api_flavor(self) -> str:
        """Backward-compatible flavor string derived from the explicit dialect."""
        return "cookie_v1" if self.api_dialect is ApiDialect.COOKIE_V1 else "v2"

    def set_bearer_token(self, token: str) -> None:
        """Replace current credentials with a Bearer token."""
        self.session.auth = None
        self.session.cookies.clear()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.api_dialect = ApiDialect.CLOUD_V2

    def _set_cookie_header(self, cookie_string: str) -> None:
        self.session.auth = None
        self.session.headers.pop("Authorization", None)
        self.session.cookies.clear()
        for pair in cookie_string.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.session.cookies.set(name.strip(), value.strip())

    def set_cookies(self, cookie_string: str) -> None:
        """Replace current credentials with browser session cookies."""
        self._set_cookie_header(cookie_string)
        self.api_dialect = ApiDialect.COOKIE_V1

    def verify_auth(self) -> None:
        """Verify current credentials with a minimal request."""
        if self.api_dialect is ApiDialect.COOKIE_V1:
            self._get("/wiki/rest/api/space", {"limit": "1"})
        else:
            self._get("/wiki/api/v2/spaces", {"limit": "1"})

    @property
    def returns_archived_pages(self) -> bool:
        """True when get_pages_in_space returns archived pages regardless of include_archived.

        v2 defaults to status=current,archived. cookie_v1 only fetches archived when
        the flag is set. Callers (e.g. the cache) use this to label what the server
        actually delivered, not just what the caller asked for.
        """
        return self.api_dialect is not ApiDialect.COOKIE_V1

    def probe_page_listing(self, space: Space) -> str | None:
        """Verify page listing and return one page ID when available."""
        if self.api_dialect is ApiDialect.COOKIE_V1:
            space_key = space.key or self._space_key_for_v1(space.id)
            data = self._get(
                "/wiki/rest/api/content",
                {
                    "spaceKey": space_key,
                    "type": "page",
                    "status": "current",
                    "limit": "1",
                },
            )
        else:
            data = self._get(f"/wiki/api/v2/spaces/{space.id}/pages", {"limit": "1"})
        results = data.get("results", [])
        if not results:
            return None
        return str(results[0].get("id", "") or "") or None

    def probe_attachment_listing(self, page_id: str) -> None:
        """Verify attachment listing for a page without downloading files."""
        if self.api_dialect is ApiDialect.COOKIE_V1:
            self._get(
                f"/wiki/rest/api/content/{quote(page_id, safe='')}/child/attachment",
                {"limit": "1"},
            )
        else:
            self._get(f"/wiki/api/v2/pages/{quote(page_id, safe='')}/attachments", {"limit": "1"})

    def _await_rate_limit(self) -> None:
        """Block until a rate-limit window set by a 429 (possibly on another worker
        thread) has passed, so one 429 backs off the whole pool. Adds small jitter so
        the pooled workers don't all wake on the same instant and re-trip the limiter
        (Atlassian's rate-limit guidance). Best-effort single-shot: a window extended
        by a peer mid-sleep is honored on this thread's next request, not mid-sleep.
        Never holds the lock while sleeping."""
        with self._rate_lock:
            wait = self._rate_limit_until - time.monotonic()
        if wait > 0:
            wait += random.uniform(0, 0.5)
            time.sleep(wait)
            with self._rate_lock:
                self.stats["rate_limit_sleep_s"] += wait

    def _note_rate_limit(self, retry_after: float) -> None:
        """Record a 429: extend the shared backoff window and count the
        rate-limited attempt in ``stats["retries"]``. Runs on EVERY 429 —
        including an exhausted final attempt, where no retry follows but the
        pool-wide pacing must survive the raise."""
        with self._rate_lock:
            self._rate_limit_until = max(
                self._rate_limit_until, time.monotonic() + retry_after
            )
            self.stats["retries"] += 1

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff for a transient 5xx / connection error."""
        wait = 2 ** attempt
        self._log(f"Transient error, retrying in {wait}s")
        time.sleep(wait)
        with self._rate_lock:
            self.stats["retries"] += 1

    def _on_request_error(self, exc, attempt: int, max_retries: int, url: str) -> None:
        """Shared retry decision for _get and _get_raw: coordinate/back off and
        return so the caller retries, or (re-)raise if not retryable / exhausted."""
        if isinstance(exc, requests.exceptions.HTTPError):
            status = exc.response.status_code
            if status in (401, 403):
                raise AuthenticationError(status, url) from exc
            if status == 429:
                # Retry-After is normally delta-seconds, but a proxy/gateway can
                # send an HTTP-date (or junk). Don't let int() raise a ValueError
                # that would escape the (HTTPError, ConnectionError)-only except
                # and crash a retryable 429; fall back to 60s.
                try:
                    retry_after = float(
                        exc.response.headers.get("Retry-After", _RETRY_AFTER_DEFAULT_S)
                    )
                except (TypeError, ValueError):
                    retry_after = _RETRY_AFTER_DEFAULT_S
                # Trust the header only within reason: float() accepts "inf"
                # (time.sleep(inf) raises OverflowError OUTSIDE the requests-
                # only except — a raw traceback), and a huge finite value
                # would stall every worker via the shared window for days.
                if not math.isfinite(retry_after) or retry_after < 0:
                    retry_after = _RETRY_AFTER_DEFAULT_S
                retry_after = min(retry_after, _RETRY_AFTER_CAP_S)
                # Extend the shared cross-thread backoff window on EVERY 429,
                # including the exhausted last attempt: concurrent download/
                # prefetch workers must still honor the final Retry-After
                # instead of firing straight into a throttling server.
                self._note_rate_limit(retry_after)
                if attempt < max_retries - 1:
                    self._log(f"Rate limited, waiting {retry_after}s")
                    return
                # Exhausted: fall through (429 < 500) to the typed-HTTPError
                # raise below, so the CLI's RequestException handler reports it
                # cleanly — the generic RuntimeError fallback isn't a
                # RequestException and escaped that handler as a traceback (#46).
            if status >= 500 and attempt < max_retries - 1:
                self._backoff(attempt)
                return
            raise exc
        # ConnectionError / Timeout (ReadTimeout, ConnectTimeout): transient, retry.
        # A read timeout on a single per-page attachment-list call must not abort the
        # whole space refresh/export with an uncaught traceback (issue #39 follow-up).
        if attempt < max_retries - 1:
            self._backoff(attempt)
            return
        raise exc

    def _get(self, path: str, params: dict | None = None, max_retries: int = 3) -> dict:
        """GET with retry + shared rate-limit handling."""
        url = self.base_url + path
        for attempt in range(max_retries):
            self._await_rate_limit()
            try:
                self._log(f"GET {url} params={params}")
                with self._rate_lock:
                    self.stats["requests"] += 1
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                self._on_request_error(exc, attempt, max_retries, url)
        raise RuntimeError(f"Max retries exceeded for {url}")

    def _get_raw(self, path: str, max_retries: int = 3) -> requests.Response:
        """GET returning a raw streaming response (file downloads), with the SAME
        retry + shared rate-limit coordination as _get so attachment downloads honor
        429/5xx/transient errors and the shared backoff window (#39)."""
        url = self.base_url + path
        for attempt in range(max_retries):
            self._await_rate_limit()
            resp = None
            try:
                self._log(f"GET (raw) {url}")
                with self._rate_lock:
                    self.stats["requests"] += 1
                resp = self.session.get(url, stream=True, timeout=60)
                resp.raise_for_status()
                return resp
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                # raise_for_status() leaves a streamed body unread, so a failed
                # response holds its pooled connection until GC. Close it before we
                # retry or raise so the connection is released, not leaked across the
                # retry loop (#39) or a sustained outage.
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:  # pragma: no cover - best-effort cleanup
                        pass
                self._on_request_error(exc, attempt, max_retries, url)
        raise RuntimeError(f"Max retries exceeded for {url}")

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages of results using cursor-based pagination."""
        all_results: list[dict] = []
        current_path = path
        current_params = dict(params) if params else {}

        while True:
            data = self._get(current_path, current_params)
            # `or` coalescing (#47 class): an explicit-null envelope field
            # would raise TypeError/AttributeError here — not a
            # RequestException, so it would escape the CLI's network-error
            # handler as a raw traceback.
            all_results.extend(data.get("results") or [])

            next_link = (data.get("_links") or {}).get("next")
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
            # `or` coalescing (#47 class): an explicit-null envelope field
            # would raise TypeError/AttributeError here — not a
            # RequestException, so it would escape the CLI's network-error
            # handler as a raw traceback.
            all_results.extend(data.get("results") or [])

            next_link = (data.get("_links") or {}).get("next")
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
        # `or` coalescing (#47 class) — see types.Version.from_api.
        return Version(
            created_at=data.get("createdAt") or data.get("when") or "",
            message=data.get("message") or "",
            number=data.get("number") or 0,
            minor_edit=data.get("minorEdit") or False,
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
        # `or` coalescing everywhere: a v1 record can carry explicit nulls just
        # like v2 (#47 class) — see types.Space.from_api.
        links = data.get("_links") or {}
        homepage = data.get("homepage") or {}
        return self._remember_space(
            Space(
                id=str(data.get("id") or ""),
                key=data.get("key") or "",
                name=data.get("name") or "",
                type=data.get("type") or "",
                status=data.get("status") or "",
                homepage_id=str(data.get("homepageId") or homepage.get("id") or ""),
                webui=links.get("webui") or "",
                base=links.get("base") or "",
            )
        )

    def _page_from_v1(self, data: dict) -> Page:
        # `or` coalescing everywhere (#47 class) — see types.Page.from_api.
        links = data.get("_links") or {}
        body = data.get("body", {})
        storage = body.get("storage", {}) if body else {}
        space = data.get("space") or {}
        history = data.get("history") or {}
        ancestors = data.get("ancestors") or []
        parent = (ancestors[-1] if ancestors else {}) or {}
        extensions = data.get("extensions") or {}
        return Page(
            id=str(data.get("id") or ""),
            title=data.get("title") or "",
            space_id=str(space.get("id") or ""),
            parent_id=str(parent.get("id") or ""),
            parent_type=parent.get("type") or "",
            position=extensions.get("position") or 0,
            status=data.get("status") or "",
            author_id=self._account_id(history.get("createdBy")),
            created_at=history.get("createdDate") or "",
            version=self._version_from_v1(data.get("version")),
            body_storage=(storage.get("value") or "") if storage else "",
            webui=links.get("webui") or "",
            editui=links.get("editui") or "",
            tinyui=links.get("tinyui") or "",
        )

    def _folder_from_v1(self, data: dict) -> dict:
        ancestors = data.get("ancestors") or []
        parent = (ancestors[-1] if ancestors else {}) or {}
        space = data.get("space") or {}
        extensions = data.get("extensions") or {}
        return {
            "id": str(data.get("id") or ""),
            "title": data.get("title") or "",
            "spaceId": str(space.get("id") or ""),
            "parentId": str(parent.get("id") or ""),
            "parentType": parent.get("type") or "",
            "position": extensions.get("position") or 0,
            "status": "folder",
        }

    def _attachment_from_v1(self, data: dict, page_id: str) -> Attachment:
        # `or` coalescing: a v1 record can carry explicit nulls just like v2;
        # None here crashes .casefold()/.get() consumers (#47 class).
        links = data.get("_links") or {}
        metadata = data.get("metadata") or {}
        extensions = data.get("extensions") or {}
        history = data.get("history") or {}
        version = self._version_from_v1(data.get("version"))
        return Attachment(
            id=str(data.get("id") or ""),
            title=data.get("title") or "",
            media_type=(
                data.get("mediaType")
                or metadata.get("mediaType")
                or extensions.get("mediaType")
                or ""
            ),
            media_type_description=(
                data.get("mediaTypeDescription")
                or metadata.get("mediaTypeDescription")
                or ""
            ),
            file_size=data.get("fileSize") or extensions.get("fileSize") or 0,
            page_id=page_id,
            comment=data.get("comment") or "",
            created_at=history.get("createdDate") or version.created_at,
            version=version,
            download_link=links.get("download") or data.get("downloadLink") or "",
            webui=links.get("webui") or "",
        )

    # -- API methods ---------------------------------------------------------

    def get_spaces(self) -> list[Space]:
        if self.api_dialect is ApiDialect.COOKIE_V1:
            results = self._paginate_offset("/wiki/rest/api/space", {"limit": "250"})
            return [self._space_from_v1(r) for r in results]

        results = self._paginate("/wiki/api/v2/spaces", {"limit": "250"})
        return [self._remember_space(Space.from_api(r)) for r in results]

    def get_pages_in_space(self, space_id: str, include_archived: bool = False) -> list[Page]:
        if self.api_dialect is ApiDialect.COOKIE_V1:
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
        if self.api_dialect is ApiDialect.COOKIE_V1:
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
        if self.api_dialect is ApiDialect.COOKIE_V1:
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
            if self.api_dialect is ApiDialect.COOKIE_V1:
                data = self._get(
                    f"/wiki/rest/api/content/{quote(folder_id, safe='')}",
                    {"expand": "ancestors,space,extensions"},
                )
                return self._folder_from_v1(data)
            return self._get(f"/wiki/api/v2/folders/{folder_id}")
        except Exception:
            return None

    def get_attachments(self, page_id: str) -> list[Attachment]:
        if self.api_dialect is ApiDialect.COOKIE_V1:
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
