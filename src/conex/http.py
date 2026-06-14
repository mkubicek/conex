"""HTTP session wrapper with exponential backoff and cross-thread 429 coordination.

Invariants:
- A 429 on any thread extends a shared window; all threads wait before the
  next request so one rate-limit response backs off the whole pool.
- Retry-After is parsed as delta-seconds; junk/absent/negative/non-finite
  values fall back to _RETRY_AFTER_DEFAULT_S; values are capped at
  _RETRY_AFTER_CAP_S so a hostile/buggy header cannot stall the CLI.
- 429 exhausted (all retries spent) raises ApiError(status=429).
- 401/403 raises AuthError immediately (no retry).
- 404 raises ApiError(status=404) immediately.
- get_stream closes the response before any retry or raise so pooled
  connections are not leaked.
- HttpStats counters are updated under the rate lock for thread safety.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from conex.errors import ApiError, AuthError

# Maximum value accepted from a Retry-After header.  Anything longer is a
# hostile or buggy header that would otherwise stall all workers for hours.
_RETRY_AFTER_CAP_S: float = 300.0

# Fallback wait when Retry-After is absent, unparseable, negative, or
# non-finite.  One constant so the three fallback sites cannot drift apart.
_RETRY_AFTER_DEFAULT_S: float = 60.0


@dataclass
class HttpStats:
    """Counters accumulated over the lifetime of an Http instance.

    All fields are updated under Http._lock so reads from other threads see a
    consistent snapshot (though individual field reads are not atomically
    grouped with each other — callers that need a point-in-time snapshot must
    copy under the lock themselves).
    """

    requests: int = 0
    retries: int = 0
    rate_limit_sleep_s: float = 0.0


class Http:
    """Retry-capable HTTP session for Confluence API calls.

    Thread-safe for concurrent get_json/get_stream calls (used by 8-worker
    download pools).  A single shared 429 window coordinates all threads so
    one rate-limit response backs off the whole pool, not just the thread that
    received it.

    Invariants:
    - auth_headers are sent on every request via the session headers.
    - max_retries applies PER CALL, not per Http instance.
    - get_stream returns an open streaming Response; the CALLER must close it.
    - get_stream closes any failed response before raising or retrying (no
      leaked pooled connections).
    """

    def __init__(
        self,
        *,
        auth_headers: dict[str, str],
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 3,
        cookie_host: str = "",
    ) -> None:
        self._timeout = timeout
        # (connect, read): a stuck TCP/TLS handshake fails fast instead of
        # hanging for the full read budget; requests applies the read timeout
        # per socket read, bounding a server that stops sending mid-response.
        self._request_timeout: tuple[float, float] = (connect_timeout, timeout)
        self._max_retries = max_retries

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._session.headers.update(auth_headers)

        # Cookie auth: also install the cookie into the session cookiejar keyed to
        # the site host.  requests strips the manually-set Cookie HEADER when a
        # request redirects to a different host (the media-download endpoint often
        # 302s), re-applying only jar cookies — an empty jar leaves the redirected
        # request unauthenticated and the download fails.  Populating the jar lets
        # requests re-attach the cookie across same-host redirects.
        cookie_header = auth_headers.get("Cookie")
        if cookie_header and cookie_host:
            for part in cookie_header.split(";"):
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name = name.strip()
                if name:
                    self._session.cookies.set(name, value.strip(), domain=cookie_host)

        # Cross-thread 429 coordination: a 429 on any thread pushes
        # _rate_limit_until forward; every thread waits before each attempt.
        self._lock = threading.Lock()
        self._rate_limit_until: float = 0.0  # monotonic epoch

        self.stats: HttpStats = HttpStats()

    # -- internal helpers ----------------------------------------------------

    def _await_rate_limit(self) -> None:
        """Block until the shared 429 window has passed.

        Adds small jitter (0–0.5 s) so all pooled workers don't wake on the
        same instant and re-trip the limiter.  Never holds the lock while
        sleeping.
        """
        with self._lock:
            wait = self._rate_limit_until - time.monotonic()
        if wait > 0:
            wait += random.uniform(0, 0.5)
            time.sleep(wait)
            with self._lock:
                self.stats.rate_limit_sleep_s += wait

    def _note_rate_limit(self, retry_after: float) -> None:
        """Extend the shared 429 window and increment the retries counter.

        Called on EVERY 429, including the exhausted final attempt: concurrent
        workers must still honor the final Retry-After even when no retry
        follows on this thread.
        """
        with self._lock:
            self._rate_limit_until = max(
                self._rate_limit_until,
                time.monotonic() + retry_after,
            )
            self.stats.retries += 1

    def _backoff(self, attempt: int) -> None:
        """Exponential sleep for a transient 5xx / connection error."""
        wait = float(2**attempt)
        time.sleep(wait)
        with self._lock:
            self.stats.retries += 1

    def _parse_retry_after(self, response: requests.Response) -> float:
        """Return a safe, finite, capped Retry-After value in seconds.

        Falls back to _RETRY_AFTER_DEFAULT_S on junk/absent/negative/infinite
        headers.  Caps at _RETRY_AFTER_CAP_S.
        """
        raw = response.headers.get("Retry-After", _RETRY_AFTER_DEFAULT_S)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return _RETRY_AFTER_DEFAULT_S
        if not math.isfinite(value) or value < 0:
            return _RETRY_AFTER_DEFAULT_S
        return min(value, _RETRY_AFTER_CAP_S)

    def _handle_error(
        self,
        exc: Exception,
        attempt: int,
        url: str,
        *,
        resp: requests.Response | None = None,
    ) -> None:
        """Decide whether to retry or raise.

        Modifies shared state (429 window, stats) and either returns (retry)
        or raises a typed exception.  The resp argument is the failed streaming
        response from get_stream; if provided it is closed before any raise.

        Raises:
            AuthError: on 401 or 403.
            ApiError: on 404, exhausted 429, exhausted/non-retryable 5xx, and
                exhausted transient connection/timeout errors.  Every terminal
                failure is a typed ConexError so the CLI never leaks a raw
                requests traceback as an "Unexpected error".
        """
        if isinstance(exc, requests.exceptions.HTTPError):
            status = exc.response.status_code
            if status in (401, 403):
                if resp is not None:
                    _close_safe(resp)
                raise AuthError(f"HTTP {status} from {url}") from exc
            if status == 404:
                if resp is not None:
                    _close_safe(resp)
                raise ApiError(f"HTTP 404 from {url}", status=404, url=url) from exc
            if status == 429:
                retry_after = self._parse_retry_after(exc.response)
                self._note_rate_limit(retry_after)
                if attempt < self._max_retries - 1:
                    if resp is not None:
                        _close_safe(resp)
                    return  # retry
                if resp is not None:
                    _close_safe(resp)
                raise ApiError(
                    f"Rate limit exceeded (HTTP 429) from {url}",
                    status=429,
                    url=url,
                ) from exc
            if status >= 500 and attempt < self._max_retries - 1:
                if resp is not None:
                    _close_safe(resp)
                self._backoff(attempt)
                return  # retry
            if resp is not None:
                _close_safe(resp)
            # Non-retryable or exhausted 5xx → typed ApiError (not a raw
            # requests HTTPError) so the CLI reports it cleanly.
            raise ApiError(
                f"HTTP {status} from {url} after {self._max_retries} attempt(s)",
                status=status,
                url=url,
            ) from exc

        # ConnectionError / Timeout: transient, retry if budget remains
        if attempt < self._max_retries - 1:
            if resp is not None:
                _close_safe(resp)
            self._backoff(attempt)
            return  # retry
        if resp is not None:
            _close_safe(resp)
        # Exhausted transient failure → typed ApiError carrying a retry hint.
        raise ApiError(
            f"network request to {url} failed after {self._max_retries} "
            f"attempt(s): {exc}; re-run to retry",
            url=url,
        ) from exc

    # -- public API ----------------------------------------------------------

    def get_json(self, url: str, params: dict | None = None) -> Any:
        """GET url and return the parsed JSON body.

        Retries on 5xx and transient connection/timeout errors with exponential
        backoff.  Honors the shared 429 window before each attempt.

        Raises:
            AuthError: on 401 or 403.
            ApiError: on 404, or on 429 when all retries are exhausted.
        """
        for attempt in range(self._max_retries):
            self._await_rate_limit()
            try:
                with self._lock:
                    self.stats.requests += 1
                resp = self._session.get(url, params=params, timeout=self._request_timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as exc:
                self._handle_error(exc, attempt, url)
        raise ApiError(f"Max retries exceeded for {url}", url=url)  # pragma: no cover

    def get_stream(self, url: str) -> requests.Response:
        """GET url with stream=True; returns an open Response for the caller to read and close.

        Honors the same shared 429 window as get_json.  On error, closes the
        response before retrying or raising so no pooled connections are leaked.

        Raises:
            AuthError: on 401 or 403.
            ApiError: on 404, or on 429 when all retries are exhausted.
        """
        for attempt in range(self._max_retries):
            self._await_rate_limit()
            resp: requests.Response | None = None
            try:
                with self._lock:
                    self.stats.requests += 1
                resp = self._session.get(url, stream=True, timeout=self._request_timeout)
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as exc:
                self._handle_error(exc, attempt, url, resp=resp)
        raise ApiError(f"Max retries exceeded for {url}", url=url)  # pragma: no cover


def _close_safe(resp: requests.Response) -> None:
    """Close a response, swallowing any exception (best-effort cleanup)."""
    try:
        resp.close()
    except Exception:
        pass
