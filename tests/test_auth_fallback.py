"""Tests for browser OAuth2 token fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from confluence_export.client import AuthenticationError, ConfluenceClient
from confluence_export.cli import _prompt_browser_credentials, _with_auth_fallback
from confluence_export.config import Config


# -- AuthenticationError raised on 401/403 ----------------------------------


def _make_http_error(status_code: int) -> requests.exceptions.HTTPError:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = {}
    return requests.exceptions.HTTPError(response=resp)


class TestAuthenticationError:
    def test_raised_on_401(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        client = ConfluenceClient(config)

        with patch.object(client.session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.raise_for_status.side_effect = _make_http_error(401)
            mock_get.return_value = mock_resp

            with pytest.raises(AuthenticationError) as exc_info:
                client._get("/wiki/api/v2/spaces")
            assert exc_info.value.status_code == 401

    def test_raised_on_403(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        client = ConfluenceClient(config)

        with patch.object(client.session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_resp.raise_for_status.side_effect = _make_http_error(403)
            mock_get.return_value = mock_resp

            with pytest.raises(AuthenticationError) as exc_info:
                client._get("/wiki/api/v2/spaces")
            assert exc_info.value.status_code == 403

    def test_not_raised_on_404(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        client = ConfluenceClient(config)

        with patch.object(client.session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.raise_for_status.side_effect = _make_http_error(404)
            mock_get.return_value = mock_resp

            with pytest.raises(requests.exceptions.HTTPError):
                client._get("/wiki/api/v2/spaces")


# -- set_bearer_token -------------------------------------------------------


class TestSetBearerToken:
    def test_replaces_auth(self):
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)
        assert client.session.auth is not None

        client.set_bearer_token("browser-tok-123")
        assert client.session.auth is None
        assert client.session.headers["Authorization"] == "Bearer browser-tok-123"


# -- Config.needs_token ------------------------------------------------------


class TestNeedsToken:
    def test_true_when_empty(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="")
        assert cfg.needs_token is True

    def test_false_when_set(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        assert cfg.needs_token is False


# -- _prompt_browser_credentials ---------------------------------------------


class TestPromptBrowserCredentials:
    def test_bearer_strips_prefix(self):
        with patch("confluence_export.cli._read_hidden_line", return_value="Bearer abc123"):
            cred_type, value = _prompt_browser_credentials("https://x.atlassian.net", "reason")
        assert cred_type == "bearer"
        assert value == "abc123"

    def test_bearer_case_insensitive(self):
        with patch("confluence_export.cli._read_hidden_line", return_value="bearer ABC"):
            cred_type, value = _prompt_browser_credentials("https://x.atlassian.net", "reason")
        assert cred_type == "bearer"
        assert value == "ABC"

    def test_raw_token_is_bearer(self):
        with patch("confluence_export.cli._read_hidden_line", return_value="raw-token-value"):
            cred_type, value = _prompt_browser_credentials("https://x.atlassian.net", "reason")
        assert cred_type == "bearer"
        assert value == "raw-token-value"

    def test_cookies_detected(self):
        cookies = "session=abc123; token=xyz789"
        with patch("confluence_export.cli._read_hidden_line", return_value=cookies):
            cred_type, value = _prompt_browser_credentials("https://x.atlassian.net", "reason")
        assert cred_type == "cookie"
        assert value == cookies

    def test_single_cookie_detected(self):
        cookie = "tenant.session.token=eyJhbGciOi..."
        with patch("confluence_export.cli._read_hidden_line", return_value=cookie):
            cred_type, value = _prompt_browser_credentials("https://x.atlassian.net", "reason")
        assert cred_type == "cookie"
        assert value == cookie

    def test_ctrl_c_exits(self):
        with patch("confluence_export.cli._read_hidden_line", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                _prompt_browser_credentials("https://x.atlassian.net", "reason")

    def test_empty_input_exits(self):
        with patch("confluence_export.cli._read_hidden_line", return_value=""):
            with pytest.raises(SystemExit):
                _prompt_browser_credentials("https://x.atlassian.net", "reason")


# -- _with_auth_fallback -----------------------------------------------------


class TestWithAuthFallback:
    def test_success_no_prompt(self):
        result = _with_auth_fallback(lambda: 42, MagicMock(), MagicMock())
        assert result == 42

    def test_retries_on_auth_error(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="bad")
        client = ConfluenceClient(config)

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AuthenticationError(401, "https://x.atlassian.net/wiki/api/v2/spaces")
            return "ok"

        with patch("confluence_export.cli._prompt_browser_credentials", return_value=("bearer", "good-token")), \
             patch.object(client, "_get", return_value={"results": []}):
            result = _with_auth_fallback(fn, client, config)

        assert result == "ok"
        assert call_count == 2
        assert client.session.headers["Authorization"] == "Bearer good-token"

    def test_exits_on_second_auth_error(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="bad")
        client = ConfluenceClient(config)

        def fn():
            raise AuthenticationError(401, "https://x.atlassian.net/wiki/api/v2/spaces")

        with patch("confluence_export.cli._prompt_browser_credentials", return_value=("bearer", "also-bad")), \
             patch.object(client, "_get", return_value={"results": []}):
            with pytest.raises(SystemExit):
                _with_auth_fallback(fn, client, config)

    def test_retries_on_http_404(self):
        """Confluence v2 API returns 404 for wrong-instance credentials."""
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = MagicMock(spec=requests.Response)
                resp.status_code = 404
                raise requests.exceptions.HTTPError(response=resp)
            return "ok"

        with patch("confluence_export.cli._prompt_browser_credentials", return_value=("bearer", "good-token")), \
             patch.object(client, "_get", return_value={"results": []}):
            result = _with_auth_fallback(fn, client, config)

        assert result == "ok"
        assert call_count == 2

    def test_does_not_catch_other_http_errors(self):
        """Non-auth HTTP errors should propagate, not trigger prompt."""
        config = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        client = ConfluenceClient(config)

        def fn():
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 500
            raise requests.exceptions.HTTPError(response=resp)

        with pytest.raises(requests.exceptions.HTTPError):
            _with_auth_fallback(fn, client, config)


# -- set_cookies -------------------------------------------------------------


class TestSetCookies:
    def test_parses_and_sets_cookies(self):
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)

        client.set_cookies("session=abc123; token=xyz789")
        assert client.session.auth is None
        assert "Authorization" not in client.session.headers
        assert client.session.cookies.get("session") == "abc123"
        assert client.session.cookies.get("token") == "xyz789"

    def test_clears_existing_bearer(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="pat")
        client = ConfluenceClient(config)
        assert "Authorization" in client.session.headers

        client.set_cookies("foo=bar; baz=qux")
        assert "Authorization" not in client.session.headers


# -- Client init without token ----------------------------------------------


class TestClientNoToken:
    def test_creates_without_credentials(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="")
        client = ConfluenceClient(config)
        assert client.session.auth is None
        assert "Authorization" not in client.session.headers
