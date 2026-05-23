"""Tests for auth errors and explicit cookie mode."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from confluence_export.client import AuthenticationError, ConfluenceClient
from confluence_export.cli import _exit_on_auth_error
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
        assert client.api_flavor == "v2"


class TestReturnsArchivedPages:
    def test_true_for_v2_default(self):
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)
        assert client.returns_archived_pages is True

    def test_false_after_set_cookies(self):
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)
        client.set_cookies("session=abc")
        assert client.returns_archived_pages is False

    def test_true_again_after_set_bearer(self):
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)
        client.set_cookies("session=abc")
        client.set_bearer_token("tok-2")
        assert client.returns_archived_pages is True


# -- Config.needs_token ------------------------------------------------------


class TestNeedsToken:
    def test_true_when_empty(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="")
        assert cfg.needs_token is True

    def test_false_when_set(self):
        cfg = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        assert cfg.needs_token is False


# -- _exit_on_auth_error -----------------------------------------------------


class TestExitOnAuthError:
    def test_success_no_prompt(self):
        result = _exit_on_auth_error(lambda: 42)
        assert result == 42

    def test_exits_on_auth_error_without_prompt(self, capsys):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="bad")
        client = ConfluenceClient(config)

        def fn():
            raise AuthenticationError(401, "https://x.atlassian.net/wiki/api/v2/spaces")

        with pytest.raises(SystemExit):
            _exit_on_auth_error(fn)

        assert "no interactive prompt" in capsys.readouterr().err

    def test_exits_on_repeated_auth_error(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="bad")
        client = ConfluenceClient(config)

        def fn():
            raise AuthenticationError(401, "https://x.atlassian.net/wiki/api/v2/spaces")

        with patch.object(client, "_get", return_value={"results": []}):
            with pytest.raises(SystemExit):
                _exit_on_auth_error(fn)

    def test_exits_on_http_404_without_prompt(self, capsys):
        """Confluence v2 API returns 404 for wrong-instance credentials."""
        config = Config(base_url="https://x.atlassian.net", email="a@b.com", api_token="tok")
        client = ConfluenceClient(config)

        def fn():
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 404
            raise requests.exceptions.HTTPError(response=resp)

        with pytest.raises(SystemExit):
            _exit_on_auth_error(fn)

        assert "HTTP 404" in capsys.readouterr().err

    def test_does_not_catch_other_http_errors(self):
        """Non-auth HTTP errors should propagate, not trigger prompt."""
        config = Config(base_url="https://x.atlassian.net", email="", api_token="tok")
        client = ConfluenceClient(config)

        def fn():
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 500
            raise requests.exceptions.HTTPError(response=resp)

        with pytest.raises(requests.exceptions.HTTPError):
            _exit_on_auth_error(fn)


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
        assert client.api_flavor == "cookie_v1"

    def test_clears_existing_bearer(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="pat")
        client = ConfluenceClient(config)
        assert "Authorization" in client.session.headers

        client.set_cookies("foo=bar; baz=qux")
        assert "Authorization" not in client.session.headers
        assert client.api_flavor == "cookie_v1"


# -- Client init without token ----------------------------------------------


class TestClientNoToken:
    def test_creates_without_credentials(self):
        config = Config(base_url="https://x.atlassian.net", email="", api_token="")
        client = ConfluenceClient(config)
        assert client.session.auth is None
        assert "Authorization" not in client.session.headers
