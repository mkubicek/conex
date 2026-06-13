"""Tests for conex.http — retry/backoff, 429 window, typed errors, stream close.

Covers:
- get_json: success path, 401/403 -> AuthError, 404 -> ApiError, 5xx backoff +
  retry, 5xx exhausted, 429 retry with Retry-After, 429 exhausted -> ApiError,
  transient connection/timeout retry, stats counters.
- get_stream: same error mapping; response closed before retry/raise; success
  returns streaming response that caller closes.
- Retry-After parsing: numeric string, float string, junk/absent/negative/huge/
  infinite -> capped defaults.
- Cross-thread 429 window: a 429 on one thread extends the window seen by other
  threads; monotonic window never moves backward.
- HttpStats: requests/retries/rate_limit_sleep_s incremented correctly.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from conex.errors import ApiError, AuthError
from conex.http import Http, HttpStats, _RETRY_AFTER_CAP_S, _RETRY_AFTER_DEFAULT_S


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_http(max_retries: int = 3, timeout: float = 30.0) -> Http:
    return Http(auth_headers={"Authorization": "Bearer tok"}, timeout=timeout, max_retries=max_retries)


def test_cookie_header_installed_in_session_cookiejar() -> None:
    """Cookie auth must also populate the cookiejar (keyed to the site host) so
    requests re-attaches it across same-host redirects instead of an empty jar."""
    h = Http(
        auth_headers={"Cookie": "sess=abc; other=def"},
        cookie_host="site.atlassian.net",
    )
    jar = {c.name: (c.value, c.domain) for c in h._session.cookies}
    assert jar.get("sess") == ("abc", "site.atlassian.net")
    assert jar.get("other") == ("def", "site.atlassian.net")
    # The raw header is still present for the non-redirect case.
    assert h._session.headers.get("Cookie") == "sess=abc; other=def"


def _http_error(status: int, url: str = "https://example.com/api") -> requests.exceptions.HTTPError:
    """Return an HTTPError carrying a response with the given status."""
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = {}
    exc = requests.exceptions.HTTPError(response=response)
    return exc


def _http_error_with_retry_after(
    status: int, retry_after: str, url: str = "https://example.com/api"
) -> requests.exceptions.HTTPError:
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = {"Retry-After": retry_after}
    exc = requests.exceptions.HTTPError(response=response)
    return exc


# ---------------------------------------------------------------------------
# HttpStats dataclass
# ---------------------------------------------------------------------------


class TestHttpStats:
    def test_defaults(self) -> None:
        s = HttpStats()
        assert s.requests == 0
        assert s.retries == 0
        assert s.rate_limit_sleep_s == 0.0

    def test_mutable(self) -> None:
        s = HttpStats()
        s.requests += 5
        assert s.requests == 5


# ---------------------------------------------------------------------------
# Http construction
# ---------------------------------------------------------------------------


class TestHttpConstruction:
    def test_auth_header_applied(self) -> None:
        h = Http(auth_headers={"X-Token": "abc"})
        assert h._session.headers["X-Token"] == "abc"

    def test_accept_header_set(self) -> None:
        h = Http(auth_headers={})
        assert h._session.headers.get("Accept") == "application/json"

    def test_stats_initial(self) -> None:
        h = _make_http()
        assert h.stats.requests == 0
        assert h.stats.retries == 0
        assert h.stats.rate_limit_sleep_s == 0.0


# ---------------------------------------------------------------------------
# get_json — success
# ---------------------------------------------------------------------------


class TestGetJsonSuccess:
    def test_returns_parsed_json(self) -> None:
        h = _make_http()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"key": "value"}

        with patch.object(h._session, "get", return_value=mock_resp) as mock_get:
            result = h.get_json("https://example.com/api", params={"q": "1"})

        assert result == {"key": "value"}
        mock_get.assert_called_once_with(
            "https://example.com/api", params={"q": "1"}, timeout=30.0
        )

    def test_increments_request_counter(self) -> None:
        h = _make_http()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {}

        with patch.object(h._session, "get", return_value=mock_resp):
            h.get_json("https://example.com/api")
            h.get_json("https://example.com/api")

        assert h.stats.requests == 2
        assert h.stats.retries == 0

    def test_no_params(self) -> None:
        h = _make_http()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = []

        with patch.object(h._session, "get", return_value=mock_resp) as mock_get:
            result = h.get_json("https://example.com/api")

        assert result == []
        mock_get.assert_called_once_with(
            "https://example.com/api", params=None, timeout=30.0
        )


# ---------------------------------------------------------------------------
# get_json — auth errors (no retry)
# ---------------------------------------------------------------------------


class TestGetJsonAuthErrors:
    @pytest.mark.parametrize("status", [401, 403])
    def test_raises_auth_error(self, status: int) -> None:
        h = _make_http()
        exc = _http_error(status)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = exc

        with patch.object(h._session, "get", return_value=mock_resp):
            with pytest.raises(AuthError):
                h.get_json("https://example.com/api")

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_error_no_retry(self, status: int) -> None:
        h = _make_http(max_retries=3)
        exc = _http_error(status)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = exc

        with patch.object(h._session, "get", return_value=mock_resp) as mock_get:
            with pytest.raises(AuthError):
                h.get_json("https://example.com/api")

        assert mock_get.call_count == 1  # no retries

    def test_retries_not_incremented_for_auth_error(self) -> None:
        h = _make_http()
        exc = _http_error(401)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = exc

        with patch.object(h._session, "get", return_value=mock_resp):
            with pytest.raises(AuthError):
                h.get_json("https://example.com/api")

        assert h.stats.retries == 0


# ---------------------------------------------------------------------------
# get_json — 404
# ---------------------------------------------------------------------------


class TestGetJson404:
    def test_raises_api_error_404(self) -> None:
        h = _make_http()
        exc = _http_error(404)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = exc

        with patch.object(h._session, "get", return_value=mock_resp):
            with pytest.raises(ApiError) as exc_info:
                h.get_json("https://example.com/api")

        assert exc_info.value.status == 404

    def test_404_no_retry(self) -> None:
        h = _make_http(max_retries=3)
        exc = _http_error(404)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = exc

        with patch.object(h._session, "get", return_value=mock_resp) as mock_get:
            with pytest.raises(ApiError):
                h.get_json("https://example.com/api")

        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# get_json — 5xx backoff and retry
# ---------------------------------------------------------------------------


class TestGetJson5xx:
    def test_retries_on_500_succeeds_eventually(self) -> None:
        h = _make_http(max_retries=3)
        fail_resp = MagicMock()
        fail_resp.raise_for_status.side_effect = _http_error(500)
        success_resp = MagicMock()
        success_resp.raise_for_status.return_value = None
        success_resp.json.return_value = {"ok": True}

        with patch.object(h._session, "get", side_effect=[fail_resp, success_resp]):
            with patch("conex.http.time.sleep") as mock_sleep:
                result = h.get_json("https://example.com/api")

        assert result == {"ok": True}
        assert h.stats.retries == 1
        assert h.stats.requests == 2
        mock_sleep.assert_called_once_with(1.0)  # 2^0 = 1

    def test_backoff_schedule_exponential(self) -> None:
        """2^0, 2^1 sleeps before the 3rd attempt succeeds."""
        h = _make_http(max_retries=3)
        fail = MagicMock()
        fail.raise_for_status.side_effect = _http_error(503)
        success = MagicMock()
        success.raise_for_status.return_value = None
        success.json.return_value = {}

        with patch.object(h._session, "get", side_effect=[fail, fail, success]):
            with patch("conex.http.time.sleep") as mock_sleep:
                h.get_json("https://example.com/api")

        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0  # 2^0
        assert mock_sleep.call_args_list[1][0][0] == 2.0  # 2^1
        assert h.stats.retries == 2

    def test_5xx_exhausted_raises(self) -> None:
        h = _make_http(max_retries=2)
        fail = MagicMock()
        fail.raise_for_status.side_effect = _http_error(500)

        with patch.object(h._session, "get", return_value=fail):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")

    def test_5xx_exhausted_call_count(self) -> None:
        h = _make_http(max_retries=3)
        fail = MagicMock()
        fail.raise_for_status.side_effect = _http_error(502)

        with patch.object(h._session, "get", return_value=fail) as mock_get:
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")

        assert mock_get.call_count == 3
        assert h.stats.retries == 2  # 2 backooffs for 3 attempts (last raises)


# ---------------------------------------------------------------------------
# get_json — 429 retry and exhaustion
# ---------------------------------------------------------------------------


class TestGetJson429:
    def test_429_retries_then_succeeds(self) -> None:
        h = _make_http(max_retries=3)
        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "1"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_mock = MagicMock()
        fail_mock.raise_for_status.side_effect = fail_exc

        success = MagicMock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"result": "ok"}

        with patch.object(h._session, "get", side_effect=[fail_mock, success]):
            with patch("conex.http.time.sleep"):
                result = h.get_json("https://example.com/api")

        assert result == {"result": "ok"}
        assert h.stats.retries == 1

    def test_429_exhausted_raises_api_error_status_429(self) -> None:
        h = _make_http(max_retries=2)
        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "1"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_mock = MagicMock()
        fail_mock.raise_for_status.side_effect = fail_exc

        with patch.object(h._session, "get", return_value=fail_mock):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError) as exc_info:
                    h.get_json("https://example.com/api")

        assert exc_info.value.status == 429

    def test_429_exhausted_window_still_updated(self) -> None:
        """The 429 window is extended even on the exhausted attempt."""
        h = _make_http(max_retries=1)
        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "30"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_mock = MagicMock()
        fail_mock.raise_for_status.side_effect = fail_exc

        before = time.monotonic()
        with patch.object(h._session, "get", return_value=fail_mock):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")

        assert h._rate_limit_until > before


# ---------------------------------------------------------------------------
# Retry-After header parsing
# ---------------------------------------------------------------------------


class TestRetryAfterParsing:
    def _make_response_with_header(self, header_value: str | None) -> requests.Response:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 429
        resp.headers = {"Retry-After": header_value} if header_value is not None else {}
        return resp

    def test_numeric_seconds(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("45")
        assert h._parse_retry_after(resp) == 45.0

    def test_float_seconds(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("12.5")
        assert h._parse_retry_after(resp) == 12.5

    def test_absent_header_returns_default(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header(None)
        assert h._parse_retry_after(resp) == _RETRY_AFTER_DEFAULT_S

    def test_junk_string_returns_default(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("Wed, 21 Oct 2015 07:28:00 GMT")
        assert h._parse_retry_after(resp) == _RETRY_AFTER_DEFAULT_S

    def test_negative_returns_default(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("-5")
        assert h._parse_retry_after(resp) == _RETRY_AFTER_DEFAULT_S

    def test_zero_is_valid(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("0")
        assert h._parse_retry_after(resp) == 0.0

    def test_huge_value_capped(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("99999")
        assert h._parse_retry_after(resp) == _RETRY_AFTER_CAP_S

    def test_infinity_string_returns_default(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("inf")
        assert h._parse_retry_after(resp) == _RETRY_AFTER_DEFAULT_S

    def test_nan_returns_default(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header("nan")
        assert h._parse_retry_after(resp) == _RETRY_AFTER_DEFAULT_S

    def test_exactly_cap_accepted(self) -> None:
        h = _make_http()
        resp = self._make_response_with_header(str(_RETRY_AFTER_CAP_S))
        assert h._parse_retry_after(resp) == _RETRY_AFTER_CAP_S


# ---------------------------------------------------------------------------
# Connection / Timeout retry
# ---------------------------------------------------------------------------


class TestTransientErrors:
    def test_connection_error_retries(self) -> None:
        h = _make_http(max_retries=3)
        success = MagicMock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"ok": True}

        with patch.object(
            h._session,
            "get",
            side_effect=[requests.exceptions.ConnectionError("connection refused"), success],
        ):
            with patch("conex.http.time.sleep"):
                result = h.get_json("https://example.com/api")

        assert result == {"ok": True}
        assert h.stats.retries == 1

    def test_timeout_retries(self) -> None:
        h = _make_http(max_retries=3)
        success = MagicMock()
        success.raise_for_status.return_value = None
        success.json.return_value = {}

        with patch.object(
            h._session,
            "get",
            side_effect=[requests.exceptions.Timeout("timed out"), success],
        ):
            with patch("conex.http.time.sleep"):
                h.get_json("https://example.com/api")

        assert h.stats.retries == 1

    def test_connection_error_exhausted_raises(self) -> None:
        h = _make_http(max_retries=2)

        with patch.object(
            h._session,
            "get",
            side_effect=requests.exceptions.ConnectionError("no route"),
        ):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")

    def test_chunked_encoding_error_wrapped_in_api_error(self) -> None:
        """A truncated chunked transfer (ChunkedEncodingError) is a RequestException
        that is NOT ConnectionError/Timeout/HTTPError — it must still be retried
        and surface as a typed ApiError, not escape as 'Unexpected error'."""
        h = _make_http(max_retries=2)
        with patch.object(
            h._session,
            "get",
            side_effect=requests.exceptions.ChunkedEncodingError("truncated"),
        ):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")

    def test_json_decode_error_wrapped_in_api_error(self) -> None:
        """A 200 with a non-JSON body (proxy interstitial) raises requests'
        JSONDecodeError from resp.json() — must surface as ApiError, not a raw
        traceback."""
        h = _make_http(max_retries=2)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = requests.exceptions.JSONDecodeError("x", "doc", 0)
        with patch.object(h._session, "get", return_value=resp):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")


# ---------------------------------------------------------------------------
# get_stream — success
# ---------------------------------------------------------------------------


class TestGetStreamSuccess:
    def test_returns_streaming_response(self) -> None:
        h = _make_http()
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.raise_for_status.return_value = None

        with patch.object(h._session, "get", return_value=mock_resp) as mock_get:
            result = h.get_stream("https://example.com/file")

        assert result is mock_resp
        mock_get.assert_called_once_with(
            "https://example.com/file", stream=True, timeout=30.0
        )

    def test_increments_request_counter(self) -> None:
        h = _make_http()
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.raise_for_status.return_value = None

        with patch.object(h._session, "get", return_value=mock_resp):
            h.get_stream("https://example.com/file")

        assert h.stats.requests == 1


# ---------------------------------------------------------------------------
# get_stream — error closes response before retry/raise
# ---------------------------------------------------------------------------


class TestGetStreamClosesOnError:
    def test_closes_response_before_retry_on_5xx(self) -> None:
        h = _make_http(max_retries=3)
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.raise_for_status.side_effect = _http_error(500)
        success_resp = MagicMock(spec=requests.Response)
        success_resp.raise_for_status.return_value = None

        with patch.object(h._session, "get", side_effect=[fail_resp, success_resp]):
            with patch("conex.http.time.sleep"):
                result = h.get_stream("https://example.com/file")

        fail_resp.close.assert_called_once()
        assert result is success_resp

    def test_closes_response_before_raise_on_auth_error(self) -> None:
        h = _make_http()
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.raise_for_status.side_effect = _http_error(401)

        with patch.object(h._session, "get", return_value=fail_resp):
            with pytest.raises(AuthError):
                h.get_stream("https://example.com/file")

        fail_resp.close.assert_called_once()

    def test_closes_response_before_raise_on_404(self) -> None:
        h = _make_http()
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.raise_for_status.side_effect = _http_error(404)

        with patch.object(h._session, "get", return_value=fail_resp):
            with pytest.raises(ApiError) as exc_info:
                h.get_stream("https://example.com/file")

        fail_resp.close.assert_called_once()
        assert exc_info.value.status == 404

    def test_closes_response_before_raise_on_exhausted_5xx(self) -> None:
        h = _make_http(max_retries=2)
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.raise_for_status.side_effect = _http_error(500)

        with patch.object(h._session, "get", return_value=fail_resp):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_stream("https://example.com/file")

        assert fail_resp.close.call_count == 2  # once per attempt

    def test_closes_response_before_raise_on_exhausted_429(self) -> None:
        h = _make_http(max_retries=1)
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "1"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_resp.raise_for_status.side_effect = fail_exc

        with patch.object(h._session, "get", return_value=fail_resp):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError) as exc_info:
                    h.get_stream("https://example.com/file")

        fail_resp.close.assert_called_once()
        assert exc_info.value.status == 429

    def test_closes_response_before_retry_on_429(self) -> None:
        """429 retry path must close the failed response."""
        h = _make_http(max_retries=3)
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "1"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_resp.raise_for_status.side_effect = fail_exc

        success_resp = MagicMock(spec=requests.Response)
        success_resp.raise_for_status.return_value = None

        with patch.object(h._session, "get", side_effect=[fail_resp, success_resp]):
            with patch("conex.http.time.sleep"):
                result = h.get_stream("https://example.com/file")

        fail_resp.close.assert_called_once()
        assert result is success_resp


# ---------------------------------------------------------------------------
# Cross-thread 429 window coordination
# ---------------------------------------------------------------------------


class TestSharedRateLimitWindow:
    def test_window_blocks_concurrent_thread(self) -> None:
        """A 429 on thread A pushes the window forward; thread B waits."""
        h = _make_http(max_retries=3)

        # Push the window forward by 1 second manually (simulating a 429)
        with h._lock:
            h._rate_limit_until = time.monotonic() + 0.1

        # _await_rate_limit should sleep for ~0.1s on the next thread
        slept = []

        real_sleep = time.sleep

        def recording_sleep(t: float) -> None:
            slept.append(t)

        with patch("conex.http.time.sleep", side_effect=recording_sleep):
            h._await_rate_limit()

        # jitter of up to 0.5 is added, so at least some sleep happened
        assert len(slept) == 1
        assert slept[0] > 0

    def test_window_not_moved_backward(self) -> None:
        """_note_rate_limit uses max so it never shortens an existing window."""
        h = _make_http()
        big_deadline = time.monotonic() + 200
        with h._lock:
            h._rate_limit_until = big_deadline

        h._note_rate_limit(1.0)  # small retry_after
        assert h._rate_limit_until >= big_deadline

    def test_multiple_threads_see_same_window(self) -> None:
        """Multiple threads observing _rate_limit_until all see the extended value."""
        h = _make_http()

        deadline_values: list[float] = []

        def worker() -> None:
            time.sleep(0.01)  # slight stagger so main sets the window first
            with h._lock:
                deadline_values.append(h._rate_limit_until)

        # Extend the window from the main thread
        h._note_rate_limit(50.0)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for v in deadline_values:
            assert v > time.monotonic()  # all threads saw a future deadline

    def test_window_extends_across_threads_via_429(self) -> None:
        """A 429 on one thread's _note_rate_limit is visible to another thread's
        _await_rate_limit: simulates the producer/consumer pattern in a worker pool."""
        h = _make_http()

        results: dict[str, float] = {}

        def producer() -> None:
            # Simulates a worker that hit a 429
            h._note_rate_limit(0.15)

        def consumer() -> None:
            time.sleep(0.02)  # wait for producer to have set the window
            slept_amounts: list[float] = []
            with patch("conex.http.time.sleep", side_effect=lambda t: slept_amounts.append(t)):
                h._await_rate_limit()
            results["slept"] = sum(slept_amounts)

        t_prod = threading.Thread(target=producer)
        t_cons = threading.Thread(target=consumer)
        t_prod.start()
        t_cons.start()
        t_prod.join()
        t_cons.join()

        # The consumer should have slept because the producer set the window
        assert results.get("slept", 0) > 0

    def test_rate_limit_sleep_s_accumulated(self) -> None:
        h = _make_http()
        # Manually force a window in the past (already expired) + large jitter
        # We need the window to still be in the future to trigger sleep.
        with h._lock:
            h._rate_limit_until = time.monotonic() + 0.05

        with patch("conex.http.time.sleep"):
            h._await_rate_limit()

        assert h.stats.rate_limit_sleep_s > 0


# ---------------------------------------------------------------------------
# Stats accuracy
# ---------------------------------------------------------------------------


class TestStatsAccuracy:
    def test_retries_count_429_attempts(self) -> None:
        """Each 429 response increments retries once."""
        h = _make_http(max_retries=3)

        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "0"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_mock = MagicMock()
        fail_mock.raise_for_status.side_effect = fail_exc

        success = MagicMock()
        success.raise_for_status.return_value = None
        success.json.return_value = {}

        # 2 failures then success
        with patch.object(h._session, "get", side_effect=[fail_mock, fail_mock, success]):
            with patch("conex.http.time.sleep"):
                h.get_json("https://example.com/api")

        assert h.stats.retries == 2
        assert h.stats.requests == 3

    def test_retries_count_5xx_attempts(self) -> None:
        h = _make_http(max_retries=3)
        fail = MagicMock()
        fail.raise_for_status.side_effect = _http_error(503)
        success = MagicMock()
        success.raise_for_status.return_value = None
        success.json.return_value = {}

        with patch.object(h._session, "get", side_effect=[fail, fail, success]):
            with patch("conex.http.time.sleep"):
                h.get_json("https://example.com/api")

        assert h.stats.retries == 2
        assert h.stats.requests == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_403_raises_auth_error(self) -> None:
        h = _make_http()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = _http_error(403)

        with patch.object(h._session, "get", return_value=mock_resp):
            with pytest.raises(AuthError):
                h.get_json("https://example.com/api")

    def test_get_stream_403_raises_auth_error(self) -> None:
        h = _make_http()
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.raise_for_status.side_effect = _http_error(403)

        with patch.object(h._session, "get", return_value=fail_resp):
            with pytest.raises(AuthError):
                h.get_stream("https://example.com/file")

    def test_get_stream_429_exhausted_raises_api_error(self) -> None:
        h = _make_http(max_retries=2)
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.status_code = 429
        fail_resp.headers = {"Retry-After": "5"}
        fail_exc = requests.exceptions.HTTPError(response=fail_resp)
        fail_resp.raise_for_status.side_effect = fail_exc

        with patch.object(h._session, "get", return_value=fail_resp):
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError) as exc_info:
                    h.get_stream("https://example.com/file")

        assert exc_info.value.status == 429

    def test_close_safe_swallows_exception(self) -> None:
        """_close_safe must never propagate an exception from resp.close()."""
        from conex.http import _close_safe

        bad_resp = MagicMock()
        bad_resp.close.side_effect = RuntimeError("close failed")
        _close_safe(bad_resp)  # should not raise

    def test_max_retries_1_no_retry(self) -> None:
        """max_retries=1 means one attempt, no retries."""
        h = _make_http(max_retries=1)
        fail = MagicMock()
        fail.raise_for_status.side_effect = _http_error(500)

        with patch.object(h._session, "get", return_value=fail) as mock_get:
            with patch("conex.http.time.sleep"):
                with pytest.raises(ApiError):
                    h.get_json("https://example.com/api")

        assert mock_get.call_count == 1
        assert h.stats.retries == 0
