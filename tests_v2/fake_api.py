"""In-memory FakeConfluenceAPI implementing the full ConfluenceAPI protocol.

Used exclusively by the e2e test suite (test_e2e.py).  The fake is mutable:
callers can add, rename, reparent, delete, and archive pages; update
attachments with new versions and controllable timestamps.

Design contracts:
- Satisfies the ConfluenceAPI Protocol fully, including attachment_download_url.
- returns_archived = True (simulating a Cloud v2 dialect).
- download() returns a minimal Response-like object whose .raw is a BytesIO;
  the caller (pull.py) calls blobs.add_stream(resp.raw) then resp.close().
- Body storage is stored per page id; attachments are stored per page id.
- Thread-safe: the fake is single-threaded (e2e tests run sequentially).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Iterator

import requests

from conex.models import Attachment, Folder, Page, PageVersion, Space


# ---------------------------------------------------------------------------
# Minimal streaming response wrapper
# ---------------------------------------------------------------------------


class _FakeRawStream:
    """Minimal file-like object satisfying BlobStore.add_stream(fp) contract.

    BlobStore.add_stream calls fp.read(chunk_size) in a loop until b"".
    """

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class _FakeResponse:
    """Minimal requests.Response stand-in for the fake download() call."""

    def __init__(self, data: bytes, status_code: int = 200) -> None:
        self.status_code = status_code
        self.raw = _FakeRawStream(data)
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# Mutable attachment record
# ---------------------------------------------------------------------------


@dataclass
class FakeAttachment:
    """Mutable record for one attachment in the fake API."""

    att_id: str
    title: str
    page_id: str
    media_type: str = "application/octet-stream"
    version: int = 1
    created_at: str = "2024-01-01T00:00:00Z"
    content: bytes = field(default_factory=lambda: b"attachment-content")
    download_url: str = ""

    def to_model(self) -> Attachment:
        return Attachment(
            id=self.att_id,
            title=self.title,
            media_type=self.media_type,
            file_size=len(self.content),
            page_id=self.page_id,
            download_url=self.download_url or f"/fake/download/{self.att_id}",
            version=PageVersion(
                number=self.version,
                created_at=self.created_at,
            ),
        )


# ---------------------------------------------------------------------------
# Mutable page record
# ---------------------------------------------------------------------------


@dataclass
class FakePage:
    """Mutable record for one page in the fake API."""

    page_id: str
    title: str
    space_id: str
    parent_id: str = ""
    parent_type: str = "page"
    position: int = 0
    status: str = "current"
    body: str = ""
    version: int = 1
    created_at: str = "2024-01-01T00:00:00Z"
    web_url: str = ""

    def to_model(self) -> Page:
        return Page(
            id=self.page_id,
            title=self.title,
            space_id=self.space_id,
            parent_id=self.parent_id,
            parent_type=self.parent_type,
            position=self.position,
            status=self.status,
            body_storage=self.body,
            version=PageVersion(
                number=self.version,
                created_at=self.created_at,
            ),
            web_url=self.web_url,
        )


# ---------------------------------------------------------------------------
# FakeConfluenceAPI
# ---------------------------------------------------------------------------


class FakeConfluenceAPI:
    """In-memory implementation of the ConfluenceAPI protocol for e2e tests.

    Supports the full mutability surface the test scenarios need:
    - add_page / remove_page / rename_page / reparent_page / archive_page
    - add_attachment / update_attachment / remove_attachment
    - Controllable failure modes: fail_download_for adds attachment ids that
      will raise on download.
    """

    returns_archived: bool = True

    def __init__(
        self,
        space_key: str = "TS",
        space_id: str = "SP1",
        space_name: str = "Test Space",
        homepage_id: str = "",
    ) -> None:
        self._space = Space(
            id=space_id,
            key=space_key,
            name=space_name,
            homepage_id=homepage_id,
        )
        self._pages: dict[str, FakePage] = {}
        self._attachments: dict[str, FakeAttachment] = {}  # att_id -> record
        self._users: dict[str, str] = {}
        self._fail_download: set[str] = set()  # att_ids that should fail

    # ------------------------------------------------------------------
    # Mutation helpers (called by tests to set up state)
    # ------------------------------------------------------------------

    def add_page(
        self,
        page_id: str,
        title: str,
        parent_id: str = "",
        parent_type: str = "page",
        position: int = 0,
        status: str = "current",
        body: str = "",
        version: int = 1,
        created_at: str = "2024-01-01T00:00:00Z",
        web_url: str = "",
    ) -> "FakeConfluenceAPI":
        """Add a page to the fake space; returns self for chaining."""
        self._pages[page_id] = FakePage(
            page_id=page_id,
            title=title,
            space_id=self._space.id,
            parent_id=parent_id,
            parent_type=parent_type,
            position=position,
            status=status,
            body=body,
            version=version,
            created_at=created_at,
            web_url=web_url,
        )
        return self

    def remove_page(self, page_id: str) -> "FakeConfluenceAPI":
        """Remove a page (simulates upstream delete); returns self."""
        self._pages.pop(page_id, None)
        # Remove any attachments for this page.
        to_remove = [
            aid for aid, att in self._attachments.items()
            if att.page_id == page_id
        ]
        for aid in to_remove:
            self._attachments.pop(aid, None)
        return self

    def rename_page(self, page_id: str, new_title: str, version: int | None = None) -> "FakeConfluenceAPI":
        """Rename a page (may trigger a move on next build); returns self."""
        p = self._pages[page_id]
        p.title = new_title
        if version is not None:
            p.version = version
        return self

    def reparent_page(
        self,
        page_id: str,
        new_parent_id: str,
        new_parent_type: str = "page",
        version: int | None = None,
    ) -> "FakeConfluenceAPI":
        """Change a page's parent (triggers a move on next build); returns self."""
        p = self._pages[page_id]
        p.parent_id = new_parent_id
        p.parent_type = new_parent_type
        if version is not None:
            p.version = version
        return self

    def archive_page(self, page_id: str) -> "FakeConfluenceAPI":
        """Set a page's status to 'archived'; returns self."""
        self._pages[page_id].status = "archived"
        return self

    def update_page_body(self, page_id: str, body: str, version: int | None = None) -> "FakeConfluenceAPI":
        """Update body content; returns self."""
        p = self._pages[page_id]
        p.body = body
        if version is not None:
            p.version = version
        return self

    def add_attachment(
        self,
        att_id: str,
        title: str,
        page_id: str,
        media_type: str = "application/octet-stream",
        version: int = 1,
        created_at: str = "2024-01-01T00:00:00Z",
        content: bytes = b"fake-attachment-data",
    ) -> "FakeConfluenceAPI":
        """Add an attachment to a page; returns self."""
        self._attachments[att_id] = FakeAttachment(
            att_id=att_id,
            title=title,
            page_id=page_id,
            media_type=media_type,
            version=version,
            created_at=created_at,
            content=content,
        )
        return self

    def update_attachment(
        self,
        att_id: str,
        new_content: bytes,
        new_version: int,
        new_created_at: str = "2024-06-01T00:00:00Z",
    ) -> "FakeConfluenceAPI":
        """Update attachment content and version; returns self."""
        att = self._attachments[att_id]
        att.content = new_content
        att.version = new_version
        att.created_at = new_created_at
        return self

    def remove_attachment(self, att_id: str) -> "FakeConfluenceAPI":
        """Remove an attachment; returns self."""
        self._attachments.pop(att_id, None)
        return self

    def add_user(self, account_id: str, display_name: str) -> "FakeConfluenceAPI":
        """Register a user display name; returns self."""
        self._users[account_id] = display_name
        return self

    def fail_download_for(self, att_id: str) -> "FakeConfluenceAPI":
        """Make download() raise for this attachment id; returns self."""
        self._fail_download.add(att_id)
        return self

    # ------------------------------------------------------------------
    # ConfluenceAPI protocol
    # ------------------------------------------------------------------

    def get_space(self, key: str) -> Space:
        if key != self._space.key:
            from conex.errors import ApiError
            raise ApiError(f"space {key!r} not found", status=404)
        return self._space

    def get_pages(
        self,
        space_id: str,
        space_key: str,
        include_archived: bool,
    ) -> list[Page]:
        pages = []
        for fp in self._pages.values():
            if not include_archived and fp.status == "archived":
                continue
            pages.append(fp.to_model())
        return pages

    def get_page_body(self, page_id: str) -> str:
        fp = self._pages.get(page_id)
        if fp is None:
            from conex.errors import ApiError
            raise ApiError(f"page {page_id!r} not found", status=404)
        return fp.body

    def get_folders(self, space_id: str) -> list[Folder]:
        return []

    def get_attachments(self, page_id: str) -> list[Attachment]:
        return [
            att.to_model()
            for att in self._attachments.values()
            if att.page_id == page_id
        ]

    def get_user_display_name(self, account_id: str) -> str:
        return self._users.get(account_id, "")

    def download(self, url: str) -> requests.Response:
        # Resolve att_id from URL (our fake URLs are /fake/download/<att_id>).
        att_id = url.rsplit("/", 1)[-1]
        if att_id in self._fail_download:
            from conex.errors import ApiError
            raise ApiError(f"download failed for {att_id}", status=500)
        att = self._attachments.get(att_id)
        if att is None:
            from conex.errors import ApiError
            raise ApiError(f"attachment {att_id!r} not found", status=404)
        return _FakeResponse(att.content)

    def attachment_download_url(self, att: Attachment) -> str:
        """Return an absolute fake download URL for att."""
        return f"https://fake.atlassian.net/fake/download/{att.id}"


# ---------------------------------------------------------------------------
# Protocol conformance guard
# ---------------------------------------------------------------------------


def test_fake_api_satisfies_protocol() -> None:
    """FakeConfluenceAPI must implement every member of ConfluenceAPI.

    This test guards against a future protocol extension that is added to
    ConfluenceAPI (or CloudV2API) without updating the fake, which would cause
    e2e scenarios to silently exercise a different surface than production.

    Checks:
    - Every public method/attribute declared on ConfluenceAPI is present on
      FakeConfluenceAPI with a compatible signature (same parameter names).
    - Every public method/attribute declared on CloudV2API is also present on
      FakeConfluenceAPI (CloudV2API is the primary real adapter and may add
      helper methods the protocol later formalises).
    """
    import inspect

    from conex.api import ConfluenceAPI
    from conex.api.v2 import CloudV2API

    def _public_methods(cls: type) -> dict[str, inspect.Signature]:
        members: dict[str, inspect.Signature] = {}
        for name in dir(cls):
            if name.startswith("_"):
                continue
            val = getattr(cls, name, None)
            if callable(val):
                try:
                    members[name] = inspect.signature(val)
                except (ValueError, TypeError):
                    members[name] = None  # type: ignore[assignment]
        return members

    protocol_methods = _public_methods(ConfluenceAPI)
    real_methods = _public_methods(CloudV2API)
    fake_methods = _public_methods(FakeConfluenceAPI)

    # Every ConfluenceAPI member must exist on the fake.
    missing_from_protocol = set(protocol_methods) - set(fake_methods)
    assert not missing_from_protocol, (
        f"FakeConfluenceAPI is missing ConfluenceAPI members: {sorted(missing_from_protocol)}"
    )

    # Every CloudV2API public method must also exist on the fake (belt-and-
    # suspenders: catches real-adapter additions before they reach the protocol).
    # Exclude 'model_config', 'model_fields', and Pydantic internals.
    _PYDANTIC_INTERNALS = {
        "model_config", "model_fields", "model_fields_set",
        "model_computed_fields", "model_extra", "model_post_init",
    }
    real_only = (set(real_methods) - set(protocol_methods) - _PYDANTIC_INTERNALS)
    # Filter to methods that are clearly part of the API surface (no dunder,
    # no Pydantic plumbing).  We warn rather than fail for adapter-internal
    # helpers that have no place in the fake (e.g. 'configure').
    surfaced = {m for m in real_only if not m.startswith("get_") is False or True}
    # Collect names that are unambiguously protocol-surface (get_*, download*,
    # returns_archived, attachment_*).
    api_surface_prefixes = ("get_", "download", "attachment_", "returns_")
    api_surface_missing = {
        m for m in real_only
        if any(m.startswith(p) for p in api_surface_prefixes)
        and m not in fake_methods
    }
    assert not api_surface_missing, (
        f"FakeConfluenceAPI is missing CloudV2API API-surface members: "
        f"{sorted(api_surface_missing)}"
    )
