"""Tests for data type serialization round-trips."""

from __future__ import annotations

from confluence_export.types import (
    Attachment,
    CachedSpace,
    Page,
    Space,
    Version,
)


class TestSpace:
    def test_from_api(self):
        data = {
            "id": "1",
            "key": "TEST",
            "name": "Test Space",
            "type": "global",
            "status": "current",
            "homepageId": "42",
            "_links": {"webui": "/wiki/spaces/TEST", "base": "https://x.atlassian.net"},
        }
        s = Space.from_api(data)
        assert s.id == "1"
        assert s.key == "TEST"
        assert s.homepage_id == "42"
        assert s.webui == "/wiki/spaces/TEST"

    def test_from_api_null_links_coalesced(self):
        # #47 class: an explicit null _links must not crash from_api.
        s = Space.from_api({"id": "1", "key": "TEST", "_links": None})
        assert s.webui == ""
        assert s.base == ""

    def test_from_api_explicit_nulls_coalesced_everywhere(self):
        # #47 class: space.key/name feed directory naming; a None crashes it.
        s = Space.from_api({"id": None, "key": None, "name": None,
                            "type": None, "status": None, "homepageId": None})
        assert s.id == ""        # not the truthy string "None"
        assert s.key == ""
        assert s.name == ""
        assert s.homepage_id == ""


class TestVersion:
    def test_from_api_none(self):
        v = Version.from_api(None)
        assert v.number == 0
        assert v.created_at == ""

    def test_from_api(self):
        v = Version.from_api({"createdAt": "2025-01-01", "number": 5, "authorId": "abc"})
        assert v.number == 5
        assert v.author_id == "abc"


class TestPage:
    def test_from_api_with_body(self):
        data = {
            "id": "42",
            "title": "Hello",
            "spaceId": "1",
            "parentId": "10",
            "parentType": "page",
            "status": "current",
            "body": {"storage": {"value": "<p>content</p>"}},
            "_links": {"webui": "/wiki/42", "editui": "/wiki/42/edit", "tinyui": "/wiki/x/abc"},
        }
        p = Page.from_api(data)
        assert p.id == "42"
        assert p.body_storage == "<p>content</p>"
        assert p.webui == "/wiki/42"

    def test_from_api_null_links_coalesced(self):
        # #47 class: an explicit null _links must not crash from_api.
        p = Page.from_api({"id": "42", "title": "Hello", "_links": None})
        assert p.webui == ""
        assert p.editui == ""
        assert p.tinyui == ""

    def test_from_api_explicit_nulls_coalesced_everywhere(self):
        # #47 class: a null title used to abort the WHOLE space export in the
        # layout planner — and to_dict round-tripped the None into the cache,
        # so every --cached run crashed too until a refresh.
        p = Page.from_api({
            "id": "42", "title": None, "spaceId": None, "parentId": None,
            "parentType": None, "position": None, "status": None,
            "authorId": None, "createdAt": None,
            "version": {"number": None, "createdAt": None},
        })
        assert p.title == ""
        assert p.space_id == ""      # not the truthy string "None"
        assert p.parent_id == ""
        assert p.position == 0
        assert p.version.number == 0
        d = p.to_dict()
        p2 = Page.from_dict(d)
        assert p2.title == ""        # the cache round-trip stays clean

    def test_round_trip(self):
        p = Page(id="1", title="Test", space_id="s1", parent_id="p1",
                 parent_type="page", position=2, status="current",
                 version=Version(created_at="2025-01-01", number=3))
        d = p.to_dict()
        p2 = Page.from_dict(d)
        assert p2.id == p.id
        assert p2.title == p.title
        assert p2.version.number == 3

    def test_round_trip_preserves_body_storage(self):
        p = Page(id="1", title="Test", space_id="s1",
                 body_storage="<p>Hello <strong>world</strong></p>")
        d = p.to_dict()
        assert d["body"]["storage"]["value"] == "<p>Hello <strong>world</strong></p>"
        p2 = Page.from_dict(d)
        assert p2.body_storage == p.body_storage

    def test_to_dict_omits_body_when_empty(self):
        p = Page(id="1", title="Test", space_id="s1", body_storage="")
        d = p.to_dict()
        assert "body" not in d


class TestAttachment:
    def test_from_api(self):
        data = {
            "id": "att1",
            "title": "img.png",
            "mediaType": "image/png",
            "fileSize": 1024,
            "pageId": "42",
            "_links": {"download": "/wiki/download/att1", "webui": "/wiki/att1"},
        }
        a = Attachment.from_api(data)
        assert a.download_link == "/wiki/download/att1"
        assert a.file_size == 1024

    def test_from_api_null_strings_coalesced(self):
        # #47: the API/cache can carry an explicit null for these fields (the
        # dict-key defaults only apply when the key is ABSENT); a None would
        # crash every .casefold()/.endswith() consumer (the drawio matchers).
        data = {"id": "att1", "title": None, "mediaType": None, "mediaTypeDescription": None}
        a = Attachment.from_api(data)
        assert a.title == ""
        assert a.media_type == ""
        assert a.media_type_description == ""

    def test_from_api_null_links_coalesced(self):
        # #47 class: an explicit null _links crashed from_api itself
        # (links.get on None), one line above the fields #47 fixed.
        a = Attachment.from_api({"id": "att1", "title": "f.png", "_links": None})
        assert a.download_link == ""
        assert a.webui == ""

    def test_from_api_null_page_id_and_file_size_coalesced(self):
        # str(None) is the TRUTHY string "None": a null pageId used to defeat
        # the `if att.page_id` guard and build a /content/None/ download URL.
        a = Attachment.from_api({"id": "a1", "title": "f.png",
                                 "pageId": None, "fileSize": None, "comment": None})
        assert a.page_id == ""
        assert a.file_size == 0
        assert a.comment == ""

    def test_round_trip(self):
        a = Attachment(id="a1", title="f.pdf", media_type="application/pdf",
                       file_size=2048, page_id="p1",
                       download_link="/wiki/download/a1")
        d = a.to_dict()
        a2 = Attachment.from_dict(d)
        assert a2.id == a.id
        assert a2.download_link == a.download_link


class TestCachedSpace:
    def test_round_trip(self):
        space = Space(id="1", key="TEST", name="Test")
        page = Page(id="p1", title="Page", space_id="1",
                    version=Version(created_at="2025-01-01", number=1))
        att = Attachment(id="a1", title="f.png", page_id="p1",
                         download_link="/wiki/download/a1")
        cs = CachedSpace(
            space=space,
            pages=[page],
            attachments={"p1": [att]},
            updated_at="2025-01-01T00:00:00Z",
            include_archived=True,
        )
        d = cs.to_dict()
        cs2 = CachedSpace.from_dict(d)
        assert cs2.space.key == "TEST"
        assert len(cs2.pages) == 1
        assert len(cs2.attachments["p1"]) == 1
        assert cs2.updated_at == "2025-01-01T00:00:00Z"
        assert cs2.include_archived is True

    def test_round_trip_defaults_old_cache_to_current_only(self):
        cs = CachedSpace.from_dict({
            "space": {"id": "1", "key": "TEST", "name": "Test"},
            "pages": [],
            "attachments": {},
            "updated_at": "2025-01-01T00:00:00Z",
        })
        assert cs.include_archived is False

    def test_round_trip_preserves_page_bodies(self):
        space = Space(id="1", key="TEST", name="Test")
        with_body = Page(id="p1", title="Has Body", space_id="1",
                         body_storage="<p>content</p>",
                         version=Version(number=1))
        without_body = Page(id="p2", title="No Body", space_id="1",
                            status="folder", version=Version(number=1))
        cs = CachedSpace(space=space, pages=[with_body, without_body],
                         attachments={}, updated_at="2025-01-01T00:00:00Z")
        d = cs.to_dict()
        cs2 = CachedSpace.from_dict(d)
        assert cs2.pages[0].body_storage == "<p>content</p>"
        assert cs2.pages[1].body_storage == ""
