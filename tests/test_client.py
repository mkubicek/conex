"""Tests for ConfluenceClient methods."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from confluence_export.client import AuthenticationError, ConfluenceClient
from confluence_export.config import Config


def _make_client(**kwargs) -> ConfluenceClient:
    defaults = {"base_url": "https://x.atlassian.net", "email": "", "api_token": "tok"}
    defaults.update(kwargs)
    return ConfluenceClient(Config(**defaults))


class TestGet:
    def test_success(self):
        client = _make_client()
        with patch.object(client.session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"results": []}
            mock_get.return_value = mock_resp

            result = client._get("/wiki/api/v2/spaces")
            assert result == {"results": []}

    def test_429_retries(self):
        client = _make_client()
        with patch.object(client.session, "get") as mock_get, \
             patch("confluence_export.client.time.sleep"):
            rate_resp = MagicMock()
            rate_resp.status_code = 429
            rate_resp.headers = {"Retry-After": "1"}
            rate_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=rate_resp)

            ok_resp = MagicMock()
            ok_resp.status_code = 200
            ok_resp.raise_for_status.return_value = None
            ok_resp.json.return_value = {"ok": True}

            mock_get.side_effect = [rate_resp, ok_resp]
            result = client._get("/test")
            assert result == {"ok": True}

    def test_500_retries(self):
        client = _make_client()
        with patch.object(client.session, "get") as mock_get, \
             patch("confluence_export.client.time.sleep"):
            err_resp = MagicMock()
            err_resp.status_code = 500
            err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=err_resp)

            ok_resp = MagicMock()
            ok_resp.status_code = 200
            ok_resp.raise_for_status.return_value = None
            ok_resp.json.return_value = {"ok": True}

            mock_get.side_effect = [err_resp, ok_resp]
            result = client._get("/test")
            assert result == {"ok": True}

    def test_connection_error_retries(self):
        client = _make_client()
        with patch.object(client.session, "get") as mock_get, \
             patch("confluence_export.client.time.sleep"):
            ok_resp = MagicMock()
            ok_resp.status_code = 200
            ok_resp.raise_for_status.return_value = None
            ok_resp.json.return_value = {"ok": True}

            mock_get.side_effect = [requests.exceptions.ConnectionError(), ok_resp]
            result = client._get("/test")
            assert result == {"ok": True}

    def test_connection_error_exhausted(self):
        client = _make_client()
        with patch.object(client.session, "get") as mock_get, \
             patch("confluence_export.client.time.sleep"):
            mock_get.side_effect = requests.exceptions.ConnectionError()
            with pytest.raises(requests.exceptions.ConnectionError):
                client._get("/test", max_retries=2)


class TestPaginate:
    def test_single_page(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"results": [{"id": "1"}, {"id": "2"}], "_links": {}}
            results = client._paginate("/test")
            assert len(results) == 2

    def test_multi_page(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.side_effect = [
                {"results": [{"id": "1"}], "_links": {"next": "/test?cursor=abc"}},
                {"results": [{"id": "2"}], "_links": {}},
            ]
            results = client._paginate("/test")
            assert len(results) == 2
            assert results[0]["id"] == "1"
            assert results[1]["id"] == "2"


class TestApiMethods:
    def test_get_spaces(self):
        client = _make_client()
        with patch.object(client, "_paginate") as mock:
            mock.return_value = [{"id": "1", "key": "TEST", "name": "Test"}]
            spaces = client.get_spaces()
            assert len(spaces) == 1
            assert spaces[0].key == "TEST"

    def test_get_page_by_id(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"id": "42", "title": "Hello"}
            page = client.get_page_by_id("42")
            assert page.id == "42"
            assert page.title == "Hello"

    def test_get_folder_by_id_success(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"id": "f1", "title": "Folder"}
            result = client.get_folder_by_id("f1")
            assert result["id"] == "f1"

    def test_get_folder_by_id_failure(self):
        client = _make_client()
        with patch.object(client, "_get", side_effect=Exception("not found")):
            result = client.get_folder_by_id("f1")
            assert result is None

    def test_get_user_info_success(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"displayName": "Alice", "email": "a@b.com"}
            result = client.get_user_info("acc-123")
            assert result["displayName"] == "Alice"
            assert result["email"] == "a@b.com"

    def test_get_user_info_no_email(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"displayName": "Bob"}
            result = client.get_user_info("acc-456")
            assert result == {"displayName": "Bob"}

    def test_get_user_info_failure(self):
        client = _make_client()
        with patch.object(client, "_get", side_effect=Exception("fail")):
            result = client.get_user_info("acc-789")
            assert result is None

    def test_download_attachment(self):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.content = b"file-data"
        with patch.object(client, "_get_raw", return_value=mock_resp):
            data = client.download_attachment("/wiki/download/att1")
            assert data == b"file-data"
            mock_resp.close.assert_called_once()

    def test_download_attachment_to_file(self, tmp_path):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"chunk1", b"chunk2"]
        with patch.object(client, "_get_raw", return_value=mock_resp):
            dest = tmp_path / "file.bin"
            written = client.download_attachment_to_file("/wiki/download/att1", str(dest))
            assert written == 12
            assert dest.read_bytes() == b"chunk1chunk2"
            mock_resp.close.assert_called_once()


class TestMaxRetriesExhausted:
    def test_raises_runtime_error(self):
        client = _make_client()
        with patch.object(client.session, "get") as mock_get, \
             patch("confluence_export.client.time.sleep"):
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.headers = {"Retry-After": "0"}
            mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
            mock_get.return_value = mock_resp

            with pytest.raises(RuntimeError, match="Max retries"):
                client._get("/test", max_retries=3)


class TestGetRaw:
    def test_returns_response(self):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch.object(client.session, "get", return_value=mock_resp):
            result = client._get_raw("/test")
            assert result is mock_resp


class TestApiMethodsExtra:
    def test_get_pages_in_space(self):
        client = _make_client()
        with patch.object(client, "_paginate") as mock:
            mock.return_value = [{"id": "1", "title": "Page"}]
            pages = client.get_pages_in_space("space1")
            assert len(pages) == 1
            mock.assert_called_once()
            _, params = mock.call_args.args
            assert params["body-format"] == "storage"

    def test_get_space_by_key_found(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {
                "results": [{"id": "1", "key": "ENG", "name": "Engineering"}]
            }
            space = client.get_space_by_key("ENG")
            assert space is not None
            assert space.key == "ENG"
            mock.assert_called_once_with(
                "/wiki/api/v2/spaces", {"keys": "ENG", "limit": "1"}
            )

    def test_get_space_by_key_not_found(self):
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"results": []}
            assert client.get_space_by_key("NOPE") is None

    def test_get_attachments(self):
        client = _make_client()
        with patch.object(client, "_paginate") as mock:
            mock.return_value = [{"id": "a1", "title": "file.png"}]
            atts = client.get_attachments("page1")
            assert len(atts) == 1


class TestCookieV1Mode:
    def test_verify_auth_uses_v1_space_endpoint(self):
        client = _make_client()
        client.set_cookies("tenant.session.token=abc")

        with patch.object(client, "_get", return_value={"results": []}) as mock:
            client.verify_auth()

        mock.assert_called_once_with("/wiki/rest/api/space", {"limit": "1"})

    def test_verify_auth_uses_v2_for_token_auth(self):
        client = _make_client()

        with patch.object(client, "_get", return_value={"results": []}) as mock:
            client.verify_auth()

        mock.assert_called_once_with("/wiki/api/v2/spaces", {"limit": "1"})

    def test_get_spaces_uses_v1_mapper_in_cookie_mode(self):
        client = _make_client()
        client.set_cookies("session=abc")

        with patch.object(client, "_paginate_offset") as mock:
            mock.return_value = [
                {
                    "id": "100",
                    "key": "ENG",
                    "name": "Engineering",
                    "type": "global",
                    "status": "current",
                    "_links": {"webui": "/display/ENG", "base": "https://x.atlassian.net/wiki"},
                }
            ]
            spaces = client.get_spaces()

        assert spaces[0].id == "100"
        assert spaces[0].key == "ENG"
        assert client._space_key_by_id["100"] == "ENG"
        mock.assert_called_once_with("/wiki/rest/api/space", {"limit": "250"})

    def test_get_pages_in_space_uses_mapped_space_key(self):
        client = _make_client()
        client.set_cookies("session=abc")
        client._space_key_by_id["100"] = "ENG"

        with patch.object(client, "_paginate_offset") as mock:
            mock.return_value = [
                {
                    "id": "42",
                    "title": "Child",
                    "status": "current",
                    "space": {"id": "100"},
                    "ancestors": [{"id": "1", "type": "page"}],
                    "extensions": {"position": 7},
                    "body": {"storage": {"value": "<p>Hello</p>"}},
                    "history": {
                        "createdDate": "2026-01-01T00:00:00Z",
                        "createdBy": {"accountId": "creator"},
                    },
                    "version": {
                        "when": "2026-01-02T00:00:00Z",
                        "number": 3,
                        "by": {"accountId": "editor"},
                    },
                    "_links": {"webui": "/display/ENG/Child"},
                }
            ]
            pages = client.get_pages_in_space("100")

        assert pages[0].id == "42"
        assert pages[0].space_id == "100"
        assert pages[0].parent_id == "1"
        assert pages[0].position == 7
        assert pages[0].body_storage == "<p>Hello</p>"
        assert pages[0].author_id == "creator"
        assert pages[0].version.author_id == "editor"
        assert pages[0].version.number == 3
        _, params = mock.call_args.args
        assert params["spaceKey"] == "ENG"
        assert params["type"] == "page"
        assert params["status"] == "current"
        assert "body.storage" in params["expand"]

    def test_get_pages_in_space_archived_issues_second_call(self):
        client = _make_client()
        client.set_cookies("session=abc")
        client._space_key_by_id["100"] = "ENG"

        with patch.object(client, "_paginate_offset", return_value=[]) as mock:
            client.get_pages_in_space("100", include_archived=True)

        statuses = [call.args[1]["status"] for call in mock.call_args_list]
        assert statuses == ["current", "archived"]

    def test_get_space_by_key_uses_v1_endpoint(self):
        client = _make_client()
        client.set_cookies("session=abc")

        with patch.object(client, "_get") as mock:
            mock.return_value = {"id": "100", "key": "ENG", "name": "Engineering"}
            space = client.get_space_by_key("ENG")

        assert space is not None
        assert space.key == "ENG"
        mock.assert_called_once_with("/wiki/rest/api/space/ENG")

    def test_get_page_by_id_uses_v1_endpoint(self):
        client = _make_client()
        client.set_cookies("session=abc")

        with patch.object(client, "_get") as mock:
            mock.return_value = {
                "id": "42",
                "title": "Page",
                "space": {"id": "100"},
                "body": {"storage": {"value": "<p>Body</p>"}},
            }
            page = client.get_page_by_id("42")

        assert page.body_storage == "<p>Body</p>"
        assert mock.call_args.args[0] == "/wiki/rest/api/content/42"

    def test_get_attachments_uses_v1_endpoint(self):
        client = _make_client()
        client.set_cookies("session=abc")

        with patch.object(client, "_paginate_offset") as mock:
            mock.return_value = [
                {
                    "id": "att1",
                    "title": "file.png",
                    "metadata": {"mediaType": "image/png"},
                    "extensions": {"fileSize": 1234},
                    "version": {"number": 5, "when": "2026-01-03T00:00:00Z"},
                    "_links": {"download": "/download/attachments/42/file.png"},
                }
            ]
            atts = client.get_attachments("42")

        assert atts[0].id == "att1"
        assert atts[0].page_id == "42"
        assert atts[0].media_type == "image/png"
        assert atts[0].file_size == 1234
        assert atts[0].version.number == 5
        mock.assert_called_once_with(
            "/wiki/rest/api/content/42/child/attachment",
            {"expand": "version,metadata,extensions,history", "limit": "250"},
        )


class TestVerboseLogging:
    def test_log_when_verbose(self, capsys):
        client = _make_client()
        client.verbose = True
        client._log("test message")
        assert "test message" in capsys.readouterr().err

    def test_no_log_when_not_verbose(self, capsys):
        client = _make_client()
        client.verbose = False
        client._log("test message")
        assert capsys.readouterr().err == ""
