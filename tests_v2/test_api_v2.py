"""Tests for conex.api.v2 (CloudV2API) and conex.api.make_api dispatching.

Coverage goals:
- Pagination: multi-page cursor pagination via _links.next
- Malformed envelopes: null results, null _links (must not crash)
- Model mapping: all fields including explicit nulls, int->str coercion
- Space / Page / Folder / Attachment model construction from v2 JSON
- get_page_body: separate fetch, nested body.storage.value
- get_user_display_name: success and failure paths
- download: delegates to Http.get_stream
- attachment_download_url: preferred endpoint vs fallback paths
- Archived listing: v2 returns_archived == True
- make_api dispatches CLOUD_V2 and GATEWAY_V2 to CloudV2API
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
import requests

from conex.api import ConfluenceAPI, make_api
from conex.api.v2 import CloudV2API, _attachment_from_v2, _folder_from_v2, _page_from_v2, _space_from_v2
from conex.config import Dialect, ResolvedConfig
from conex.errors import ApiError
from conex.models import Attachment, Folder, Page, PageVersion, Space


# ---------------------------------------------------------------------------
# Fake Http for injection
# ---------------------------------------------------------------------------


class FakeHttp:
    """Minimal Http double that serves canned JSON responses by URL pattern."""

    def __init__(self, responses: dict[str, object]) -> None:
        """responses maps url substrings to response values.

        Values may be:
        - dict: returned as-is from get_json
        - Exception subclass instance: raised from get_json/get_stream
        - "stream": sentinel meaning get_stream returns a FakeResponse
        """
        self._responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def _lookup(self, url: str, params: dict | None = None) -> object:
        # Adapters pass next-link queries via params, not the URL, so match
        # canned keys against the full url?query form. Query-fragment keys
        # (containing "=") win over bare path keys so a path key cannot
        # shadow the more specific follow-up-page key; ties go to the longer.
        full = url + ("?" + urlencode(params) if params else "")
        for key in sorted(self._responses, key=lambda k: ("=" in k, len(k)), reverse=True):
            if key in full:
                return self._responses[key]
        raise KeyError(f"No canned response for URL: {full!r}")

    def get_json(self, url: str, params: dict | None = None) -> object:
        self.calls.append((url, params))
        val = self._lookup(url, params)
        if isinstance(val, BaseException):
            raise val
        return val

    def get_stream(self, url: str) -> "FakeResponse":
        self.calls.append((url, None))
        val = self._lookup(url)
        if isinstance(val, BaseException):
            raise val
        return FakeResponse(b"bytes")


class FakeResponse:
    def __init__(self, content: bytes = b"") -> None:
        self.content = content
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int = 8192):
        yield self.content


def _make_api(http: FakeHttp, dialect: Dialect = Dialect.CLOUD_V2) -> CloudV2API:
    cfg = ResolvedConfig(
        site_url="https://example.atlassian.net",
        api_base_url="https://example.atlassian.net",
        auth_headers={"Authorization": "Bearer tok"},
        dialect=dialect,
    )
    api = CloudV2API(cfg)
    api._http = http  # inject fake
    return api


# ---------------------------------------------------------------------------
# make_api dispatch
# ---------------------------------------------------------------------------


def test_make_api_cloud_v2():
    cfg = ResolvedConfig(
        site_url="https://x.atlassian.net",
        api_base_url="https://x.atlassian.net",
        auth_headers={},
        dialect=Dialect.CLOUD_V2,
    )
    api = make_api(cfg)
    assert isinstance(api, CloudV2API)


def test_make_api_gateway_v2():
    cfg = ResolvedConfig(
        site_url="https://x.atlassian.net",
        api_base_url="https://api.atlassian.com/ex/confluence/cloud123",
        auth_headers={},
        dialect=Dialect.GATEWAY_V2,
    )
    api = make_api(cfg)
    assert isinstance(api, CloudV2API)


def test_make_api_cookie_v1():
    from conex.api.v1 import CookieV1API

    cfg = ResolvedConfig(
        site_url="https://x.atlassian.net",
        api_base_url="https://x.atlassian.net",
        auth_headers={},
        dialect=Dialect.COOKIE_V1,
    )
    api = make_api(cfg)
    assert isinstance(api, CookieV1API)


# ---------------------------------------------------------------------------
# returns_archived
# ---------------------------------------------------------------------------


def test_returns_archived_true():
    api = _make_api(FakeHttp({}))
    assert api.returns_archived is True


# ---------------------------------------------------------------------------
# get_space
# ---------------------------------------------------------------------------


def test_get_space_found():
    payload = {
        "results": [{"id": "42", "key": "TS", "name": "Test Space", "homepageId": "7"}],
        "_links": {},
    }
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces": payload}))
    space = api.get_space("TS")
    assert space.id == "42"
    assert space.key == "TS"
    assert space.name == "Test Space"
    assert space.homepage_id == "7"


def test_get_space_not_found_raises():
    payload = {"results": [], "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces": payload}))
    with pytest.raises(ApiError) as exc_info:
        api.get_space("MISSING")
    assert exc_info.value.status == 404


def test_get_space_null_results_raises():
    payload = {"results": None, "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces": payload}))
    with pytest.raises(ApiError):
        api.get_space("X")


def test_get_space_null_homepage_id():
    payload = {
        "results": [{"id": "1", "key": "A", "name": "A", "homepageId": None}],
    }
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces": payload}))
    space = api.get_space("A")
    assert space.homepage_id == ""


# ---------------------------------------------------------------------------
# get_pages — single page, no pagination
# ---------------------------------------------------------------------------


def _page_row(page_id: str = "10", title: str = "P", **kwargs) -> dict:
    row: dict = {
        "id": page_id,
        "title": title,
        "spaceId": "S1",
        "parentId": "",
        "parentType": "space",
        "position": 0,
        "status": "current",
        "body": {"storage": {"value": "<p>hello</p>", "representation": "storage"}},
        "version": {
            "number": 3,
            "createdAt": "2024-01-01T00:00:00Z",
            "message": "msg",
            "author": {"accountId": "uid1"},
        },
        "_links": {"webui": "/wiki/spaces/S1/pages/10"},
    }
    row.update(kwargs)
    return row


def test_get_pages_single():
    payload = {"results": [_page_row()], "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces/S1/pages": payload}))
    pages = api.get_pages("S1", "KEY", include_archived=False)
    assert len(pages) == 1
    p = pages[0]
    assert p.id == "10"
    assert p.title == "P"
    assert p.space_id == "S1"
    assert p.body_storage == "<p>hello</p>"
    assert p.version.number == 3
    assert p.version.author_id == "uid1"
    assert p.status == "current"


def test_get_pages_archived_included_by_default():
    """v2 always returns both current and archived — no filter applied."""
    archived_row = _page_row(page_id="20", status="archived")
    payload = {"results": [_page_row(), archived_row], "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces/S1/pages": payload}))
    pages = api.get_pages("S1", "KEY", include_archived=True)
    statuses = {p.status for p in pages}
    assert "archived" in statuses


def test_get_pages_include_archived_flag_ignored():
    """include_archived=False still gets all pages; v2 always returns both."""
    archived_row = _page_row(page_id="20", status="archived")
    payload = {"results": [_page_row(), archived_row], "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/spaces/S1/pages": payload}))
    pages = api.get_pages("S1", "KEY", include_archived=False)
    assert len(pages) == 2


# ---------------------------------------------------------------------------
# Pagination — multi-page via _links.next
# ---------------------------------------------------------------------------


def test_get_pages_multi_page_pagination():
    """Two pages across two cursor responses."""
    page1_row = _page_row(page_id="1", title="First")
    page2_row = _page_row(page_id="2", title="Second")

    responses = {
        # First page of results: has a next link
        "/wiki/api/v2/spaces/S1/pages": {
            "results": [page1_row],
            "_links": {"next": "/wiki/api/v2/spaces/S1/pages?cursor=abc"},
        },
        # Second page: cursor URL
        "cursor=abc": {
            "results": [page2_row],
            "_links": {},
        },
    }
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "KEY", include_archived=False)
    assert len(pages) == 2
    assert pages[0].id == "1"
    assert pages[1].id == "2"


def test_paginate_null_results_envelope():
    """A None results field must not crash — treat as empty list."""
    responses = {
        "/wiki/api/v2/spaces/S1/pages": {"results": None, "_links": {}},
    }
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "KEY", include_archived=False)
    assert pages == []


def test_paginate_null_links_envelope():
    """A None _links field must not crash — treat as no next link."""
    responses = {
        "/wiki/api/v2/spaces/S1/pages": {"results": [_page_row()], "_links": None},
    }
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "KEY", include_archived=False)
    assert len(pages) == 1


def test_paginate_missing_links_key():
    """Absent _links key must not crash."""
    responses = {
        "/wiki/api/v2/spaces/S1/pages": {"results": [_page_row()]},
    }
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "KEY", include_archived=False)
    assert len(pages) == 1


# ---------------------------------------------------------------------------
# get_page_body
# ---------------------------------------------------------------------------


def test_get_page_body():
    data = {"body": {"storage": {"value": "<p>content</p>"}}}
    api = _make_api(FakeHttp({"/wiki/api/v2/pages/42": data}))
    body = api.get_page_body("42")
    assert body == "<p>content</p>"


def test_get_page_body_null_body():
    data = {"body": None}
    api = _make_api(FakeHttp({"/wiki/api/v2/pages/42": data}))
    assert api.get_page_body("42") == ""


def test_get_page_body_missing_storage():
    data = {"body": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/pages/42": data}))
    assert api.get_page_body("42") == ""


# ---------------------------------------------------------------------------
# get_folders
# ---------------------------------------------------------------------------


# There is NO /spaces/{id}/folders endpoint — folders are discovered from the
# page set via /folders/{id}, recursing on folder-parented folders.


def _page(pid: str, parent_id: str = "", parent_type: str = "") -> Page:
    return Page(
        id=pid,
        title=pid,
        space_id="S1",
        parent_id=parent_id,
        parent_type=parent_type,
        version=PageVersion(number=1, created_at="2024-01-01T00:00:00Z"),
    )


def test_get_folders_discovers_from_page_parents():
    api = _make_api(FakeHttp({
        "/wiki/api/v2/folders/F1": {
            "id": "F1", "title": "Folder", "parentId": "S1", "parentType": "space",
        },
    }))
    pages = [_page("p1", parent_id="F1", parent_type="folder")]
    folders = api.get_folders("S1", pages)
    assert len(folders) == 1
    assert folders[0].id == "F1"
    assert folders[0].title == "Folder"
    assert folders[0].parent_type == "space"


def test_get_folders_recurses_folder_parents():
    api = _make_api(FakeHttp({
        "/wiki/api/v2/folders/F2": {
            "id": "F2", "title": "Child", "parentId": "F1", "parentType": "folder",
        },
        "/wiki/api/v2/folders/F1": {
            "id": "F1", "title": "Parent", "parentId": "S1", "parentType": "space",
        },
    }))
    pages = [_page("p1", parent_id="F2", parent_type="folder")]
    folders = api.get_folders("S1", pages)
    assert {f.id for f in folders} == {"F1", "F2"}


def test_get_folders_empty_when_no_folder_parents():
    # No page is folder-parented → no folder fetch at all (and no hit on a
    # non-existent endpoint).
    api = _make_api(FakeHttp({}))
    assert api.get_folders("S1", [_page("p1")]) == []


def test_get_folders_skips_unfetchable_folder():
    # A folder id that 404s is skipped (best-effort), never fatal.
    api = _make_api(FakeHttp({
        "/wiki/api/v2/folders/F1": ApiError("not found", status=404),
    }))
    pages = [_page("p1", parent_id="F1", parent_type="folder")]
    assert api.get_folders("S1", pages) == []


# ---------------------------------------------------------------------------
# get_attachments
# ---------------------------------------------------------------------------


def _att_row(att_id: str = "A1", page_id: str = "P1", title: str = "img.png") -> dict:
    return {
        "id": att_id,
        "title": title,
        "mediaType": "image/png",
        "fileSize": 1024,
        "_links": {"download": f"/wiki/download/attachments/{page_id}/{title}"},
        "version": {
            "number": 1,
            "createdAt": "2024-06-01T00:00:00Z",
            "author": {"accountId": "uid1"},
        },
    }


def test_get_attachments():
    payload = {"results": [_att_row()], "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/pages/P1/attachments": payload}))
    atts = api.get_attachments("P1")
    assert len(atts) == 1
    a = atts[0]
    assert a.id == "A1"
    assert a.title == "img.png"
    assert a.media_type == "image/png"
    assert a.file_size == 1024
    assert a.page_id == "P1"
    assert a.version.number == 1


def test_get_attachments_multi_page():
    a1 = _att_row("A1")
    a2 = _att_row("A2", title="doc.pdf")
    responses = {
        "/wiki/api/v2/pages/P1/attachments": {
            "results": [a1],
            "_links": {"next": "/wiki/api/v2/pages/P1/attachments?cursor=y"},
        },
        "cursor=y": {"results": [a2], "_links": {}},
    }
    api = _make_api(FakeHttp(responses))
    atts = api.get_attachments("P1")
    assert len(atts) == 2


def test_get_attachments_null_results():
    payload = {"results": None, "_links": {}}
    api = _make_api(FakeHttp({"/wiki/api/v2/pages/P1/attachments": payload}))
    assert api.get_attachments("P1") == []


# ---------------------------------------------------------------------------
# get_user_display_name
# ---------------------------------------------------------------------------


def test_get_user_display_name():
    data = {"displayName": "Alice Smith"}
    api = _make_api(FakeHttp({"/wiki/rest/api/user": data}))
    assert api.get_user_display_name("uid1") == "Alice Smith"


def test_get_user_display_name_public_name_fallback():
    data = {"publicName": "bob42"}
    api = _make_api(FakeHttp({"/wiki/rest/api/user": data}))
    assert api.get_user_display_name("uid1") == "bob42"


def test_get_user_display_name_not_found():
    api = _make_api(FakeHttp({"/wiki/rest/api/user": ApiError("404", status=404)}))
    assert api.get_user_display_name("uid1") == ""


def test_get_user_display_name_other_error():
    api = _make_api(FakeHttp({"/wiki/rest/api/user": RuntimeError("boom")}))
    assert api.get_user_display_name("uid1") == ""


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_calls_get_stream():
    fake = FakeHttp({"/download/url": FakeResponse(b"data")})
    api = _make_api(fake)
    resp = api.download("https://example.atlassian.net/download/url")
    assert isinstance(resp, FakeResponse)


# ---------------------------------------------------------------------------
# attachment_download_url — preferred endpoint vs fallback
# ---------------------------------------------------------------------------


def test_attachment_download_url_preferred():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="A1", page_id="P1", download_url="/wiki/download/att/1")
    url = api.attachment_download_url(att)
    assert url == "https://example.atlassian.net/wiki/rest/api/content/P1/child/attachment/A1/download"


def test_attachment_download_url_no_page_id_fallback_with_wiki():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="/wiki/download/att/1")
    url = api.attachment_download_url(att)
    assert url == "https://example.atlassian.net/wiki/download/att/1"


def test_attachment_download_url_no_page_id_fallback_missing_wiki_prefix():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="/download/att/1")
    url = api.attachment_download_url(att)
    assert url == "https://example.atlassian.net/wiki/download/att/1"


def test_attachment_download_url_absolute_fallback():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="https://other.host/att")
    url = api.attachment_download_url(att)
    assert url == "https://other.host/att"


def test_attachment_download_url_no_url_no_ids():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="")
    url = api.attachment_download_url(att)
    assert url == ""


# ---------------------------------------------------------------------------
# Gateway dialect — same surface, different base URL
# ---------------------------------------------------------------------------


def test_gateway_uses_gateway_base_url():
    cfg = ResolvedConfig(
        site_url="https://example.atlassian.net",
        api_base_url="https://api.atlassian.com/ex/confluence/cloud123",
        auth_headers={},
        dialect=Dialect.GATEWAY_V2,
    )
    http = FakeHttp(
        {
            "/wiki/api/v2/spaces": {
                "results": [{"id": "1", "key": "GW", "name": "GW Space"}],
                "_links": {},
            }
        }
    )
    api = CloudV2API(cfg)
    api._http = http
    space = api.get_space("GW")
    assert space.key == "GW"
    called_url = http.calls[0][0]
    assert "api.atlassian.com" in called_url


def test_gateway_attachment_download_url():
    cfg = ResolvedConfig(
        site_url="https://example.atlassian.net",
        api_base_url="https://api.atlassian.com/ex/confluence/cloud123",
        auth_headers={},
        dialect=Dialect.GATEWAY_V2,
    )
    api = CloudV2API(cfg)
    att = Attachment(id="A1", page_id="P1", download_url="/wiki/download/att")
    url = api.attachment_download_url(att)
    assert "api.atlassian.com" in url
    assert "/wiki/rest/api/content/P1/child/attachment/A1/download" in url


# ---------------------------------------------------------------------------
# Model mapping — explicit nulls and int id coercion
# ---------------------------------------------------------------------------


def test_space_from_v2_explicit_nulls():
    data = {"id": None, "key": None, "name": None, "homepageId": None}
    space = _space_from_v2(data)
    assert space.id == ""
    assert space.key == ""
    assert space.name == ""
    assert space.homepage_id == ""


def test_page_from_v2_explicit_nulls():
    data = {
        "id": None,
        "title": None,
        "spaceId": None,
        "parentId": None,
        "parentType": None,
        "position": None,
        "status": None,
        "body": None,
        "version": None,
        "_links": None,
    }
    page = _page_from_v2(data)
    assert page.id == ""
    assert page.title == ""
    assert page.body_storage == ""
    assert page.status == "current"  # model default


def test_page_from_v2_int_ids():
    data = {
        "id": 123,
        "spaceId": 456,
        "parentId": 789,
        "body": {"storage": {"value": ""}},
    }
    page = _page_from_v2(data)
    assert page.id == "123"
    assert page.space_id == "456"
    assert page.parent_id == "789"


def test_page_from_v2_null_version():
    data = {"id": "1", "version": None}
    page = _page_from_v2(data)
    assert page.version == PageVersion()


def test_folder_from_v2_explicit_nulls():
    data = {"id": None, "title": None, "parentId": None, "position": None}
    folder = _folder_from_v2(data)
    assert folder.id == ""
    assert folder.title == ""
    assert folder.parent_id == ""
    assert folder.position == 0


def test_attachment_from_v2_explicit_nulls():
    data = {
        "id": None,
        "title": None,
        "mediaType": None,
        "fileSize": None,
        "_links": None,
        "version": None,
    }
    att = _attachment_from_v2(data, "P1")
    assert att.id == ""
    assert att.title == ""
    assert att.media_type == ""
    assert att.file_size == 0
    assert att.page_id == "P1"
    assert att.download_url == ""
    assert att.version == PageVersion()


def test_attachment_from_v2_int_id():
    data = {"id": 99, "_links": {"download": "/wiki/d"}}
    att = _attachment_from_v2(data, "P1")
    assert att.id == "99"


def test_page_version_from_v2_null_author():
    data = {"id": "1", "version": {"number": 2, "author": None}}
    page = _page_from_v2(data)
    assert page.version.author_id == ""


def test_page_from_v2_body_storage_null():
    data = {"id": "1", "body": {"storage": {"value": None}}}
    page = _page_from_v2(data)
    assert page.body_storage == ""


def test_page_from_v2_no_body_key():
    data = {"id": "1"}
    page = _page_from_v2(data)
    assert page.body_storage == ""


# ---------------------------------------------------------------------------
# Protocol compliance smoke-test
# ---------------------------------------------------------------------------


def test_protocol_compliance():
    """CloudV2API satisfies ConfluenceAPI at runtime (structural check)."""
    # Just ensure the required attributes/methods are present.
    api = _make_api(FakeHttp({}))
    assert hasattr(api, "returns_archived")
    assert hasattr(api, "get_space")
    assert hasattr(api, "get_pages")
    assert hasattr(api, "get_page_body")
    assert hasattr(api, "get_folders")
    assert hasattr(api, "get_attachments")
    assert hasattr(api, "get_user_display_name")
    assert hasattr(api, "download")


# ---------------------------------------------------------------------------
# get_folders — closure termination + auth propagation
# ---------------------------------------------------------------------------


class _BoundedHttp(FakeHttp):
    """FakeHttp that aborts after max_folder_calls /folders/ fetches so a
    non-terminating folder-closure loop fails fast instead of hanging."""

    def __init__(self, responses: dict, max_folder_calls: int = 10) -> None:
        super().__init__(responses)
        self._max_folder_calls = max_folder_calls
        self._folder_calls = 0

    def get_json(self, url: str, params: dict | None = None) -> object:
        if "/folders/" in url:
            self._folder_calls += 1
            if self._folder_calls > self._max_folder_calls:
                raise AssertionError(
                    f"get_folders did not terminate: >{self._max_folder_calls} fetches"
                )
        return super().get_json(url, params)


def test_get_folders_terminates_on_folder_cycle():
    # Two folders that name each other as folder-parents (F1<->F2). The
    # `discovered` guard must break the loop and fetch each exactly once.
    http = _BoundedHttp({
        "/wiki/api/v2/folders/F1": {
            "id": "F1", "title": "Loop A", "parentId": "F2", "parentType": "folder",
        },
        "/wiki/api/v2/folders/F2": {
            "id": "F2", "title": "Loop B", "parentId": "F1", "parentType": "folder",
        },
    })
    api = _make_api(http)
    folders = api.get_folders("S1", [_page("p1", parent_id="F1", parent_type="folder")])
    assert {f.id for f in folders} == {"F1", "F2"}
    assert http._folder_calls == 2


def test_get_folders_propagates_auth_error():
    # A 404 is swallowed to None (skip), but an AuthError is a credential
    # problem and MUST propagate — never silently degraded into a missing folder.
    from conex.errors import AuthError

    api = _make_api(FakeHttp({
        "/wiki/api/v2/folders/F1": AuthError("401 Unauthorized"),
    }))
    with pytest.raises(AuthError):
        api.get_folders("S1", [_page("p1", parent_id="F1", parent_type="folder")])
