"""Shared test fixtures."""

import pytest

from confluence_export.types import Attachment, Page, PageNode, Version


@pytest.fixture
def sample_pages() -> list[Page]:
    """A flat list of pages forming a tree:
    Root
    ├── Child A
    │   └── Grandchild A1
    └── Child B
    """
    return [
        Page(
            id="1",
            title="Root",
            space_id="100",
            parent_id="",
            parent_type="space",
            position=0,
            version=Version(created_at="2025-01-01T00:00:00Z", number=1),
        ),
        Page(
            id="2",
            title="Child A",
            space_id="100",
            parent_id="1",
            parent_type="page",
            position=0,
            version=Version(created_at="2025-01-02T00:00:00Z", number=3),
        ),
        Page(
            id="3",
            title="Child B",
            space_id="100",
            parent_id="1",
            parent_type="page",
            position=1,
            version=Version(created_at="2025-01-03T00:00:00Z", number=2),
        ),
        Page(
            id="4",
            title="Grandchild A1",
            space_id="100",
            parent_id="2",
            parent_type="page",
            position=0,
            version=Version(created_at="2025-01-04T00:00:00Z", number=1),
        ),
    ]


@pytest.fixture
def sample_attachments() -> list[Attachment]:
    return [
        Attachment(
            id="att1",
            title="screenshot.png",
            media_type="image/png",
            file_size=1024,
            page_id="1",
            download_link="/wiki/rest/api/content/att1/download",
        ),
        Attachment(
            id="att2",
            title="diagram.drawio",
            media_type="application/x-drawio",
            file_size=2048,
            page_id="1",
            download_link="/wiki/rest/api/content/att2/download",
        ),
    ]
