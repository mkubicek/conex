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

    def test_round_trip(self):
        p = Page(id="1", title="Test", space_id="s1", parent_id="p1",
                 parent_type="page", position=2, status="current",
                 version=Version(created_at="2025-01-01", number=3))
        d = p.to_dict()
        p2 = Page.from_dict(d)
        assert p2.id == p.id
        assert p2.title == p.title
        assert p2.version.number == 3


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
        )
        d = cs.to_dict()
        cs2 = CachedSpace.from_dict(d)
        assert cs2.space.key == "TEST"
        assert len(cs2.pages) == 1
        assert len(cs2.attachments["p1"]) == 1
        assert cs2.updated_at == "2025-01-01T00:00:00Z"
