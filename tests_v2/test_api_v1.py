"""Tests for conex.api.v1 (CookieV1API).

Coverage goals:
- Pagination: multi-page via _links.next (PORT _paginate_offset semantics)
- Malformed envelopes: null results, null _links
- Model mapping: v1 JSON shapes including body.storage.value, ancestors,
  numeric ids, explicit nulls
- get_pages: current-only vs include_archived (two API calls)
- get_folders: always returns []
- get_space: from /wiki/rest/api/space/{key}
- get_page_body: separate per-page fetch
- get_user_display_name: success and failure
- download: delegates to Http.get_stream
- attachment_download_url: preferred REST endpoint vs fallback
- returns_archived == False
- parent_type is always "page" for pages with ancestors (v1 invariant)
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from conex.api.v1 import (
    CookieV1API,
    _attachment_from_v1,
    _page_from_v1,
    _space_from_v1,
    _version_from_v1,
)
from conex.config import Dialect, ResolvedConfig
from conex.errors import ApiError
from conex.models import Attachment, Page, PageVersion, Space


# ---------------------------------------------------------------------------
# Fake Http
# ---------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, responses: dict[str, object]) -> None:
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

    def close(self) -> None:
        pass


def _make_api(http: FakeHttp) -> CookieV1API:
    cfg = ResolvedConfig(
        site_url="https://wiki.example.com",
        api_base_url="https://wiki.example.com",
        auth_headers={"Cookie": "JSESSIONID=abc"},
        dialect=Dialect.COOKIE_V1,
    )
    api = CookieV1API(cfg)
    api._http = http
    return api


# ---------------------------------------------------------------------------
# returns_archived
# ---------------------------------------------------------------------------


def test_returns_archived_false():
    api = _make_api(FakeHttp({}))
    assert api.returns_archived is False


# ---------------------------------------------------------------------------
# get_space
# ---------------------------------------------------------------------------


def _space_data(sid: str = "101", key: str = "TS", name: str = "Test Space") -> dict:
    return {
        "id": sid,
        "key": key,
        "name": name,
        "_links": {"webui": "/display/TS", "self": "/rest/api/space/TS"},
        "homepage": {"id": "55"},
    }


def test_get_space():
    api = _make_api(FakeHttp({"/wiki/rest/api/space/TS": _space_data()}))
    space = api.get_space("TS")
    assert space.id == "101"
    assert space.key == "TS"
    assert space.name == "Test Space"
    assert space.homepage_id == "55"


def test_get_space_homepage_id_top_level():
    data = _space_data()
    data["homepageId"] = "99"
    data.pop("homepage", None)
    api = _make_api(FakeHttp({"/wiki/rest/api/space/TS": data}))
    space = api.get_space("TS")
    assert space.homepage_id == "99"


def test_get_space_int_id():
    data = _space_data(sid=123)  # type: ignore[arg-type]
    api = _make_api(FakeHttp({"/wiki/rest/api/space/TS": data}))
    space = api.get_space("TS")
    assert space.id == "123"


def test_get_space_propagates_api_error():
    api = _make_api(FakeHttp({"/wiki/rest/api/space/MISSING": ApiError("404", status=404)}))
    with pytest.raises(ApiError):
        api.get_space("MISSING")


# ---------------------------------------------------------------------------
# get_pages — current only, no pagination
# ---------------------------------------------------------------------------


def _page_data(
    pid: str = "10",
    title: str = "P",
    parent_id: str = "",
    status: str = "current",
) -> dict:
    ancestors = []
    if parent_id:
        ancestors = [{"id": parent_id, "type": "page", "title": "Parent"}]
    return {
        "id": pid,
        "title": title,
        "type": "page",
        "status": status,
        "space": {"id": "S1", "key": "TS"},
        "ancestors": ancestors,
        "extensions": {"position": 5},
        "history": {"createdDate": "2023-01-01"},
        "version": {
            "number": 2,
            "when": "2024-01-01T00:00:00.000Z",
            "message": "update",
            "by": {"accountId": "uid1"},
        },
        "body": {"storage": {"value": "<p>body</p>", "representation": "storage"}},
        "_links": {"webui": "/wiki/display/TS/P"},
    }


def _pages_envelope(rows: list[dict], next_url: str | None = None) -> dict:
    env: dict = {"results": rows, "_links": {}}
    if next_url:
        env["_links"]["next"] = next_url
    return env


def test_get_pages_current_only():
    env = _pages_envelope([_page_data()])
    api = _make_api(FakeHttp({"/wiki/rest/api/content": env}))
    pages = api.get_pages("S1", "TS", include_archived=False)
    assert len(pages) == 1
    p = pages[0]
    assert p.id == "10"
    assert p.title == "P"
    assert p.body_storage == "<p>body</p>"
    assert p.status == "current"
    assert p.version.number == 2


def test_get_pages_with_parent():
    env = _pages_envelope([_page_data(parent_id="99")])
    api = _make_api(FakeHttp({"/wiki/rest/api/content": env}))
    pages = api.get_pages("S1", "TS", include_archived=False)
    p = pages[0]
    assert p.parent_id == "99"
    assert p.parent_type == "page"


def test_get_pages_no_parent():
    env = _pages_envelope([_page_data(parent_id="")])
    api = _make_api(FakeHttp({"/wiki/rest/api/content": env}))
    pages = api.get_pages("S1", "TS", include_archived=False)
    assert pages[0].parent_type == ""


def test_get_pages_include_archived_makes_two_calls():
    """include_archived triggers a second call with status=archived."""
    current_env = _pages_envelope([_page_data(pid="1", status="current")])
    archived_env = _pages_envelope([_page_data(pid="2", status="archived")])

    calls: list[dict | None] = []

    class TrackingHttp:
        def get_json(self, url: str, params: dict | None = None) -> object:
            calls.append(params)
            if params and params.get("status") == "archived":
                return archived_env
            return current_env

    api = _make_api(FakeHttp({}))  # type: ignore[arg-type]
    api._http = TrackingHttp()  # type: ignore[assignment]
    pages = api.get_pages("S1", "TS", include_archived=True)
    statuses = [p.status for p in pages]
    assert "current" in statuses
    assert "archived" in statuses
    assert len(pages) == 2


def test_get_pages_include_archived_false_one_call():
    """include_archived=False: only one call with status=current."""
    calls: list[dict | None] = []
    current_env = _pages_envelope([_page_data()])

    class TrackingHttp:
        def get_json(self, url: str, params: dict | None = None) -> object:
            calls.append(params)
            return current_env

    api = _make_api(FakeHttp({}))  # type: ignore[arg-type]
    api._http = TrackingHttp()  # type: ignore[assignment]
    api.get_pages("S1", "TS", include_archived=False)
    assert len(calls) == 1
    assert calls[0] is not None and calls[0].get("status") == "current"


# ---------------------------------------------------------------------------
# Pagination — multi-page via _links.next
# ---------------------------------------------------------------------------


def test_get_pages_multi_page_pagination():
    page1 = _page_data(pid="1", title="First")
    page2 = _page_data(pid="2", title="Second")

    responses = {
        "/wiki/rest/api/content": _pages_envelope(
            [page1],
            next_url="/wiki/rest/api/content?start=1&limit=250",
        ),
        "start=1": _pages_envelope([page2]),
    }
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "TS", include_archived=False)
    assert len(pages) == 2
    ids = [p.id for p in pages]
    assert "1" in ids
    assert "2" in ids


def test_paginate_null_results_no_crash():
    responses = {"/wiki/rest/api/content": {"results": None, "_links": {}}}
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "TS", include_archived=False)
    assert pages == []


def test_paginate_null_links_no_crash():
    responses = {"/wiki/rest/api/content": {"results": [_page_data()], "_links": None}}
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "TS", include_archived=False)
    assert len(pages) == 1


def test_paginate_missing_links_key():
    responses = {"/wiki/rest/api/content": {"results": [_page_data()]}}
    api = _make_api(FakeHttp(responses))
    pages = api.get_pages("S1", "TS", include_archived=False)
    assert len(pages) == 1


# ---------------------------------------------------------------------------
# get_page_body
# ---------------------------------------------------------------------------


def test_get_page_body():
    data = {"body": {"storage": {"value": "<h1>Title</h1>"}}}
    api = _make_api(FakeHttp({"/wiki/rest/api/content/10": data}))
    body = api.get_page_body("10")
    assert body == "<h1>Title</h1>"


def test_get_page_body_null_body():
    data = {"body": None}
    api = _make_api(FakeHttp({"/wiki/rest/api/content/10": data}))
    assert api.get_page_body("10") == ""


# ---------------------------------------------------------------------------
# get_folders — always empty
# ---------------------------------------------------------------------------


def test_get_folders_always_empty():
    api = _make_api(FakeHttp({}))
    assert api.get_folders("S1", []) == []


def test_get_folders_warns_about_flattening(capsys):
    api = _make_api(FakeHttp({}))
    api.get_folders("S1", [])
    err = capsys.readouterr().err.lower()
    assert "folder" in err and "root" in err


# ---------------------------------------------------------------------------
# get_attachments
# ---------------------------------------------------------------------------


def _att_data(att_id: str = "A1", title: str = "img.png", page_id: str = "P1") -> dict:
    return {
        "id": att_id,
        "title": title,
        "metadata": {"mediaType": "image/png"},
        "extensions": {"fileSize": 2048},
        "_links": {"download": f"/download/attachments/{page_id}/{title}"},
        "version": {
            "number": 1,
            "when": "2024-01-01T00:00:00Z",
            "by": {"accountId": "uid2"},
        },
    }


def test_get_attachments():
    env = {"results": [_att_data()], "_links": {}}
    api = _make_api(FakeHttp({"/wiki/rest/api/content/P1/child/attachment": env}))
    atts = api.get_attachments("P1")
    assert len(atts) == 1
    a = atts[0]
    assert a.id == "A1"
    assert a.title == "img.png"
    assert a.media_type == "image/png"
    assert a.file_size == 2048
    assert a.page_id == "P1"
    assert a.version.number == 1
    assert a.version.author_id == "uid2"


def test_get_attachments_multi_page():
    a1 = _att_data("A1")
    a2 = _att_data("A2", title="doc.pdf")
    responses = {
        "/wiki/rest/api/content/P1/child/attachment": {
            "results": [a1],
            "_links": {"next": "/wiki/rest/api/content/P1/child/attachment?start=1"},
        },
        "start=1": {"results": [a2], "_links": {}},
    }
    api = _make_api(FakeHttp(responses))
    atts = api.get_attachments("P1")
    assert len(atts) == 2


def test_get_attachments_null_results():
    env = {"results": None, "_links": {}}
    api = _make_api(FakeHttp({"/wiki/rest/api/content/P1/child/attachment": env}))
    assert api.get_attachments("P1") == []


# ---------------------------------------------------------------------------
# get_user_display_name
# ---------------------------------------------------------------------------


def test_get_user_display_name():
    data = {"displayName": "Bob Builder"}
    api = _make_api(FakeHttp({"/wiki/rest/api/user": data}))
    assert api.get_user_display_name("uid1") == "Bob Builder"


def test_get_user_display_name_public_name():
    data = {"publicName": "carol99"}
    api = _make_api(FakeHttp({"/wiki/rest/api/user": data}))
    assert api.get_user_display_name("uid1") == "carol99"


def test_get_user_display_name_error_returns_empty():
    api = _make_api(FakeHttp({"/wiki/rest/api/user": RuntimeError("oops")}))
    assert api.get_user_display_name("uid1") == ""


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download():
    fake = FakeHttp({"/wiki/download/att": FakeResponse(b"data")})
    api = _make_api(fake)
    resp = api.download("https://wiki.example.com/wiki/download/att")
    assert isinstance(resp, FakeResponse)


# ---------------------------------------------------------------------------
# attachment_download_url
# ---------------------------------------------------------------------------


def test_attachment_download_url_preferred():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="A1", page_id="P1", download_url="/download/att/1")
    url = api.attachment_download_url(att)
    assert url == "https://wiki.example.com/wiki/rest/api/content/P1/child/attachment/A1/download"


def test_attachment_download_url_fallback_with_wiki():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="/wiki/download/att")
    url = api.attachment_download_url(att)
    assert url == "https://wiki.example.com/wiki/download/att"


def test_attachment_download_url_fallback_missing_wiki_prefix():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="/download/att")
    url = api.attachment_download_url(att)
    assert url == "https://wiki.example.com/wiki/download/att"


def test_attachment_download_url_absolute():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="https://cdn.example.com/att")
    url = api.attachment_download_url(att)
    assert url == "https://cdn.example.com/att"


def test_attachment_download_url_empty():
    api = _make_api(FakeHttp({}))
    att = Attachment(id="", page_id="", download_url="")
    assert api.attachment_download_url(att) == ""


# ---------------------------------------------------------------------------
# Model factories — explicit nulls and edge cases
# ---------------------------------------------------------------------------


def test_space_from_v1_explicit_nulls():
    data = {"id": None, "key": None, "name": None, "_links": None, "homepage": None}
    space = _space_from_v1(data)
    assert space.id == ""
    assert space.key == ""
    assert space.name == ""
    assert space.homepage_id == ""


def test_space_from_v1_int_id():
    data = {"id": 42, "key": "A", "name": "A Space"}
    space = _space_from_v1(data)
    assert space.id == "42"


def test_page_from_v1_explicit_nulls():
    data = {
        "id": None,
        "title": None,
        "space": None,
        "ancestors": None,
        "extensions": None,
        "history": None,
        "version": None,
        "body": None,
        "_links": None,
        "status": None,
    }
    page = _page_from_v1(data)
    assert page.id == ""
    assert page.title == ""
    assert page.space_id == ""
    assert page.parent_id == ""
    assert page.parent_type == ""
    assert page.body_storage == ""


def test_page_from_v1_int_ids():
    data = {
        "id": 100,
        "space": {"id": 200},
        "ancestors": [{"id": 300, "type": "page"}],
        "body": {"storage": {"value": ""}},
    }
    page = _page_from_v1(data)
    assert page.id == "100"
    assert page.space_id == "200"
    assert page.parent_id == "300"


def test_page_from_v1_body_storage_value():
    data = {
        "id": "1",
        "body": {"storage": {"value": "<p>content</p>"}},
    }
    page = _page_from_v1(data)
    assert page.body_storage == "<p>content</p>"


def test_page_from_v1_null_body_storage():
    data = {"id": "1", "body": {"storage": {"value": None}}}
    page = _page_from_v1(data)
    assert page.body_storage == ""


def test_page_from_v1_empty_body():
    data = {"id": "1", "body": {}}
    page = _page_from_v1(data)
    assert page.body_storage == ""


def test_page_from_v1_parent_type_always_page():
    """v1 ancestors are always pages; parent_type must be 'page' when parent exists."""
    data = {
        "id": "1",
        "ancestors": [{"id": "99", "type": "page"}],
    }
    page = _page_from_v1(data)
    assert page.parent_type == "page"


def test_page_from_v1_parent_type_empty_when_no_parent():
    data = {"id": "1", "ancestors": []}
    page = _page_from_v1(data)
    assert page.parent_type == ""


def test_attachment_from_v1_explicit_nulls():
    data = {
        "id": None,
        "title": None,
        "metadata": None,
        "extensions": None,
        "_links": None,
        "version": None,
    }
    att = _attachment_from_v1(data, "P1")
    assert att.id == ""
    assert att.title == ""
    assert att.media_type == ""
    assert att.file_size == 0
    assert att.page_id == "P1"
    assert att.download_url == ""
    assert att.version == PageVersion()


def test_attachment_from_v1_int_id():
    data = {"id": 777, "_links": {"download": "/d"}}
    att = _attachment_from_v1(data, "P1")
    assert att.id == "777"


def test_version_from_v1_none():
    v = _version_from_v1(None)
    assert v == PageVersion()


def test_version_from_v1_explicit_nulls():
    v = _version_from_v1({"number": None, "when": None, "message": None, "by": None})
    assert v.number == 0
    assert v.created_at == ""
    assert v.message == ""
    assert v.author_id == ""


def test_version_from_v1_when_field():
    """v1 uses 'when' for the timestamp."""
    v = _version_from_v1({"number": 1, "when": "2023-06-01T12:00:00.000Z", "by": {}})
    assert v.created_at == "2023-06-01T12:00:00.000Z"


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_protocol_compliance():
    api = _make_api(FakeHttp({}))
    assert hasattr(api, "returns_archived")
    assert hasattr(api, "get_space")
    assert hasattr(api, "get_pages")
    assert hasattr(api, "get_page_body")
    assert hasattr(api, "get_folders")
    assert hasattr(api, "get_attachments")
    assert hasattr(api, "get_user_display_name")
    assert hasattr(api, "download")
