"""Tests for ConfluenceClient methods."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from confluence_export.client import AuthenticationError, ConfluenceClient
from confluence_export.config import (
    ApiDialect,
    AuthConfig,
    AuthMode,
    Config,
    ConnectionProfile,
)


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


def _make_profile_client(auth_mode, *, dialect=ApiDialect.CLOUD_V2, **auth_kwargs) -> ConfluenceClient:
    """Build a client from an explicit ConnectionProfile (the non-legacy path)."""
    profile = ConnectionProfile(
        site_url="https://x.atlassian.net",
        api_base_url="https://x.atlassian.net",
        cloud_id=None,
        auth_mode=auth_mode,
        api_dialect=dialect,
        config_source="test",
        interactive=False,
        auth=AuthConfig(type=auth_mode, **auth_kwargs),
    )
    return ConfluenceClient(profile)


class TestConstructorWithProfile:
    def test_profile_passed_through_directly(self):
        # Line 40: a ConnectionProfile is used as-is, not wrapped from a Config.
        client = _make_profile_client(AuthMode.BASIC_API_TOKEN, email="a@b.com", token="tok")
        assert client.base_url == "https://x.atlassian.net"
        assert client.api_dialect is ApiDialect.CLOUD_V2
        assert client.session.auth is not None

    def test_cookie_auth_mode_sets_cookie_header(self, capsys):
        # Lines 69-70: COOKIE auth_mode with a cookie_header installs the cookies.
        client = _make_profile_client(
            AuthMode.COOKIE,
            dialect=ApiDialect.COOKIE_V1,
            cookie_header="session=abc; other=def",
        )
        client.verbose = True
        # Cookies were installed on the session, no Authorization header.
        assert client.session.cookies.get("session") == "abc"
        assert client.session.cookies.get("other") == "def"
        assert "Authorization" not in client.session.headers
        assert client.session.auth is None

    def test_bearer_pat_sets_authorization_header(self):
        client = _make_profile_client(AuthMode.BEARER_PAT, token="pat-xyz")
        assert client.session.headers["Authorization"] == "Bearer pat-xyz"

    def test_no_credentials_leaves_session_unauthenticated(self):
        client = _make_profile_client(AuthMode.BASIC_API_TOKEN)
        assert client.session.auth is None
        assert "Authorization" not in client.session.headers


class TestProbeListings:
    def test_probe_page_listing_v2_returns_first_id(self):
        # Lines 143-148: v2 path returns the first result's id.
        client = _make_client()
        from confluence_export.types import Space

        space = Space(id="100", key="ENG", name="Engineering")
        with patch.object(client, "_get") as mock:
            mock.return_value = {"results": [{"id": "55"}, {"id": "56"}]}
            page_id = client.probe_page_listing(space)
        assert page_id == "55"
        mock.assert_called_once_with(
            "/wiki/api/v2/spaces/100/pages", {"limit": "1"}
        )

    def test_probe_page_listing_v2_empty_returns_none(self):
        # Lines 146-147: empty results -> None.
        client = _make_client()
        from confluence_export.types import Space

        space = Space(id="100", key="ENG", name="Engineering")
        with patch.object(client, "_get", return_value={"results": []}):
            assert client.probe_page_listing(space) is None

    def test_probe_page_listing_v1_uses_content_endpoint(self):
        # Lines 132-142: cookie_v1 path queries /content with spaceKey.
        client = _make_client()
        client.set_cookies("session=abc")
        client._space_key_by_id["100"] = "ENG"
        from confluence_export.types import Space

        space = Space(id="100", key="", name="Engineering")
        with patch.object(client, "_get") as mock:
            mock.return_value = {"results": [{"id": "9"}]}
            page_id = client.probe_page_listing(space)
        assert page_id == "9"
        path, params = mock.call_args.args
        assert path == "/wiki/rest/api/content"
        assert params["spaceKey"] == "ENG"
        assert params["type"] == "page"

    def test_probe_attachment_listing_v2(self):
        # Line 158: v2 attachment probe.
        client = _make_client()
        with patch.object(client, "_get", return_value={"results": []}) as mock:
            client.probe_attachment_listing("42")
        mock.assert_called_once_with(
            "/wiki/api/v2/pages/42/attachments", {"limit": "1"}
        )

    def test_probe_attachment_listing_v1(self):
        # Lines 152-156: cookie_v1 attachment probe.
        client = _make_client()
        client.set_cookies("session=abc")
        with patch.object(client, "_get", return_value={"results": []}) as mock:
            client.probe_attachment_listing("42")
        mock.assert_called_once_with(
            "/wiki/rest/api/content/42/child/attachment", {"limit": "1"}
        )


class TestPaginateOffset:
    def test_single_page(self):
        # Lines 224-234: single page, no next link.
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.return_value = {"results": [{"id": "1"}, {"id": "2"}], "_links": {}}
            results = client._paginate_offset("/wiki/rest/api/space")
        assert [r["id"] for r in results] == ["1", "2"]

    def test_multi_page_follows_next_link(self):
        # Lines 232-240: a next link drives a second fetch with parsed params.
        client = _make_client()
        with patch.object(client, "_get") as mock:
            mock.side_effect = [
                {"results": [{"id": "1"}], "_links": {"next": "/wiki/rest/api/space?start=25&limit=25"}},
                {"results": [{"id": "2"}], "_links": {}},
            ]
            results = client._paginate_offset("/wiki/rest/api/space", {"limit": "25"})
        assert [r["id"] for r in results] == ["1", "2"]
        # Second call uses the path + params parsed out of the next link.
        second_path, second_params = mock.call_args_list[1].args
        assert second_path == "/wiki/rest/api/space"
        assert second_params == {"start": "25", "limit": "25"}


class TestSpaceKeyForV1:
    def test_resolves_via_get_spaces_when_not_cached(self):
        # Lines 275-277: not cached -> scan get_spaces() for a matching id.
        client = _make_client()
        from confluence_export.types import Space

        matching = Space(id="100", key="ENG", name="Engineering")
        other = Space(id="200", key="OPS", name="Operations")
        with patch.object(client, "get_spaces", return_value=[other, matching]):
            assert client._space_key_for_v1("100") == "ENG"

    def test_last_resort_returns_input_when_unknown(self):
        # Line 280: no cache hit, no get_spaces match -> echo the input.
        client = _make_client()
        with patch.object(client, "get_spaces", return_value=[]):
            assert client._space_key_for_v1("UNKNOWN") == "UNKNOWN"


class TestFolderFromV1:
    def test_maps_v1_folder_fields(self):
        # Lines 325-329 (and the returned dict): v1 folder mapping.
        client = _make_client()
        data = {
            "id": 77,
            "title": "Docs",
            "space": {"id": "100"},
            "ancestors": [{"id": "1"}, {"id": "5", "type": "page"}],
            "extensions": {"position": 3},
        }
        result = client._folder_from_v1(data)
        assert result == {
            "id": "77",
            "title": "Docs",
            "spaceId": "100",
            "parentId": "5",
            "parentType": "page",
            "position": 3,
            "status": "folder",
        }

    def test_no_ancestors_yields_empty_parent(self):
        client = _make_client()
        result = client._folder_from_v1({"id": "9", "title": "Top"})
        assert result["parentId"] == ""
        assert result["parentType"] == ""
        assert result["position"] == 0


class TestGetSpaceByKeyV1:
    def test_404_returns_none(self):
        # Lines 408-410: a 404 from the v1 space endpoint maps to None.
        client = _make_client()
        client.set_cookies("session=abc")
        err_resp = MagicMock()
        err_resp.status_code = 404
        http_err = requests.exceptions.HTTPError(response=err_resp)
        with patch.object(client, "_get", side_effect=http_err):
            assert client.get_space_by_key("MISSING") is None

    def test_other_http_error_reraises(self):
        # Line 411: a non-404 HTTP error propagates.
        client = _make_client()
        client.set_cookies("session=abc")
        err_resp = MagicMock()
        err_resp.status_code = 500
        http_err = requests.exceptions.HTTPError(response=err_resp)
        with patch.object(client, "_get", side_effect=http_err):
            with pytest.raises(requests.exceptions.HTTPError):
                client.get_space_by_key("ENG")

    def test_success_maps_v1_space(self):
        client = _make_client()
        client.set_cookies("session=abc")
        with patch.object(client, "_get") as mock:
            mock.return_value = {"id": "100", "key": "ENG", "name": "Engineering"}
            space = client.get_space_by_key("ENG")
        assert space is not None
        assert space.id == "100"
        assert space.key == "ENG"


class TestGetFolderByIdV1:
    def test_cookie_v1_uses_content_endpoint(self):
        # Lines 434-439: cookie_v1 folder fetch goes through /content + v1 mapper.
        client = _make_client()
        client.set_cookies("session=abc")
        with patch.object(client, "_get") as mock:
            mock.return_value = {
                "id": "77",
                "title": "Docs",
                "space": {"id": "100"},
            }
            result = client.get_folder_by_id("77")
        assert result["id"] == "77"
        assert result["status"] == "folder"
        path, params = mock.call_args.args
        assert path == "/wiki/rest/api/content/77"
        assert params["expand"] == "ancestors,space,extensions"

    def test_cookie_v1_failure_returns_none(self):
        client = _make_client()
        client.set_cookies("session=abc")
        with patch.object(client, "_get", side_effect=Exception("boom")):
            assert client.get_folder_by_id("77") is None
