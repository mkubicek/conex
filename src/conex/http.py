"""HTTP session wrapper with exponential backoff and cross-thread 429 coordination.

PORT: retry/backoff/429-window semantics from confluence_export/client.py
(_get, _get_raw, _await_rate_limit, _note_rate_limit, _backoff, _on_request_error).

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
        max_retries: int = 3,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._session.headers.update(auth_headers)

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
            ApiError: on 404 or exhausted 429.
            requests.exceptions.HTTPError: on non-retryable 5xx (exhausted).
            requests.exceptions.ConnectionError/Timeout: on exhausted transient.
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
            raise exc  # non-retryable or exhausted 5xx

        # ConnectionError / Timeout: transient, retry if budget remains
        if attempt < self._max_retries - 1:
            if resp is not None:
                _close_safe(resp)
            self._backoff(attempt)
            return  # retry
        if resp is not None:
            _close_safe(resp)
        raise exc

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
                resp = self._session.get(url, params=params, timeout=self._timeout)
                resp.raise_for_status()
                return resp.json()
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
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
                resp = self._session.get(url, stream=True, timeout=self._timeout)
                resp.raise_for_status()
                return resp
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                self._handle_error(exc, attempt, url, resp=resp)
        raise ApiError(f"Max retries exceeded for {url}", url=url)  # pragma: no cover


def _close_safe(resp: requests.Response) -> None:
    """Close a response, swallowing any exception (best-effort cleanup)."""
    try:
        resp.close()
    except Exception:
        pass
