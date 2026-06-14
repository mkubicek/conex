"""Tests for conex.pull — pull() integration with FakeAPI.

Coverage:
- Body fetch only when body_storage == "" (inline bodies stored directly).
- Incremental attachment skip when (att_id, version) is in prev.attachment_blobs
  AND blobs.has(digest).
- Version bump triggers re-download.
- Download failure -> stderr warning + attachments_complete=False + no blob entry.
- derived_blobs carry-forward from prev verbatim.
- include_archived=True but api.returns_archived==False -> warn + record False.
- users map populated from page + attachment version author_ids.
- author_lookup=False skips user lookups entirely.
- Snapshot persisted and loadable via SnapshotStore.
- Deterministic snapshot under shuffled pool completion order (body_blobs keys).
- Snapshot.pages never contain body_storage content (always "").
- Snapshot.include_archived reflects what was ACTUALLY fetched.
- No network calls in tests (all via FakeAPI).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests

from conex.api import ConfluenceAPI
from conex.models import Attachment, Folder, Page, PageVersion, Space
from conex.pull import PullOptions, pull
from conex.store.blobs import BlobStore
from conex.store.state import Snapshot, SnapshotStore


# ---------------------------------------------------------------------------
# FakeAPI — ConfluenceAPI implementation for tests
# ---------------------------------------------------------------------------


class FakeAPI:
    """In-memory ConfluenceAPI for testing.

    All returned objects are real conex.models instances.
    download() returns a minimal requests.Response-like object wrapping
    a bytes payload.
    """

    def __init__(
        self,
        *,
        space: Space | None = None,
        pages: list[Page] | None = None,
        folders: list[Folder] | None = None,
        attachments: dict[str, list[Attachment]] | None = None,
        users: dict[str, str] | None = None,
        download_data: dict[str, bytes] | None = None,
        download_error: dict[str, Exception] | None = None,
        returns_archived: bool = True,
    ) -> None:
        self.returns_archived = returns_archived
        self._space = space or Space(id="S1", key="TEST", name="Test Space")
        self._pages = pages or []
        self._folders = folders or []
        self._attachments = attachments or {}
        self._users = users or {}
        self._download_data = download_data or {}
        self._download_error = download_error or {}

        # body_overrides: page_id -> body returned by get_page_body
        # (used to simulate COOKIE_V1 dialect where bodies are fetched separately)
        self._body_overrides: dict[str, str] = {}

        # Call tracking
        self.get_space_calls: list[str] = []
        self.get_pages_calls: list[tuple[str, str, bool]] = []
        self.get_folders_calls: list[str] = []
        self.get_page_body_calls: list[str] = []
        self.get_attachments_calls: list[str] = []
        self.get_user_display_name_calls: list[str] = []
        self.download_calls: list[str] = []

    def get_space(self, key: str) -> Space:
        self.get_space_calls.append(key)
        return self._space

    def get_pages(
        self,
        space_id: str,
        space_key: str,
        include_archived: bool,
    ) -> list[Page]:
        self.get_pages_calls.append((space_id, space_key, include_archived))
        return list(self._pages)

    def get_page_body(self, page_id: str) -> str:
        self.get_page_body_calls.append(page_id)
        if page_id in self._body_overrides:
            return self._body_overrides[page_id]
        for page in self._pages:
            if page.id == page_id:
                return page.body_storage
        return ""

    def get_folders(self, space_id: str, pages: list[Page]) -> list[Folder]:
        self.get_folders_calls.append(space_id)
        return list(self._folders)

    def get_attachments(self, page_id: str) -> list[Attachment]:
        self.get_attachments_calls.append(page_id)
        return list(self._attachments.get(page_id, []))

    def get_user_display_name(self, account_id: str) -> str:
        self.get_user_display_name_calls.append(account_id)
        return self._users.get(account_id, "")

    def download(self, url: str) -> requests.Response:
        self.download_calls.append(url)
        if url in self._download_error:
            raise self._download_error[url]
        data = self._download_data.get(url, b"fake-binary-content")
        resp = requests.Response()
        resp.status_code = 200
        resp.raw = io.BytesIO(data)
        return resp

    def attachment_download_url(self, att: Attachment) -> str:
        """Return att.download_url as-is (adequate for tests; adapters resolve)."""
        return att.download_url


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def blobs(tmp_root: Path) -> BlobStore:
    return BlobStore(tmp_root)


def _default_opts(**kwargs: Any) -> PullOptions:
    return PullOptions(
        include_archived=kwargs.get("include_archived", False),
        fetch_media=kwargs.get("fetch_media", True),
        author_lookup=kwargs.get("author_lookup", False),
        workers=kwargs.get("workers", 2),
    )


# ---------------------------------------------------------------------------
# Basic smoke test
# ---------------------------------------------------------------------------


class TestPullBasic:
    def test_pull_returns_snapshot(self, tmp_root: Path, blobs: BlobStore) -> None:
        api = FakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="Home",
                    space_id="S1",
                    body_storage="<p>Hello</p>",
                )
            ]
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert isinstance(snap, Snapshot)
        assert snap.space.key == "TEST"
        assert len(snap.pages) == 1

    def test_pull_resolves_space(self, tmp_root: Path, blobs: BlobStore) -> None:
        api = FakeAPI()
        pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert api.get_space_calls == ["TEST"]

    def test_pull_fetched_at_is_iso(self, tmp_root: Path, blobs: BlobStore) -> None:
        api = FakeAPI()
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert "T" in snap.fetched_at
        assert snap.fetched_at.endswith("+00:00") or snap.fetched_at.endswith("Z")


# ---------------------------------------------------------------------------
# Body fetch behaviour
# ---------------------------------------------------------------------------


class TestBodyFetch:
    def test_inline_body_not_refetched(self, tmp_root: Path, blobs: BlobStore) -> None:
        """When body_storage != '', get_page_body must NOT be called."""
        api = FakeAPI(
            pages=[
                Page(id="p1", title="P1", space_id="S1", body_storage="<p>inline</p>"),
            ]
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert api.get_page_body_calls == []
        assert "p1" in snap.body_blobs

    def test_empty_body_triggers_fetch(self, tmp_root: Path, blobs: BlobStore) -> None:
        """When body_storage == '', get_page_body IS called."""
        # Pages listed with empty body_storage (simulates v1 / COOKIE dialect).
        # _body_overrides maps page_id -> body returned by get_page_body.
        api = FakeAPI(
            pages=[
                Page(id="p2", title="P2", space_id="S1", body_storage=""),
            ]
        )
        api._body_overrides = {"p2": "<b>fetched</b>"}

        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert "p2" in api.get_page_body_calls
        assert "p2" in snap.body_blobs
        body = blobs.read_bytes(snap.body_blobs["p2"])
        assert body == b"<b>fetched</b>"

    def test_body_blob_stores_encoded_body(self, tmp_root: Path, blobs: BlobStore) -> None:
        api = FakeAPI(
            pages=[
                Page(id="p1", title="P1", space_id="S1", body_storage="<h1>Test</h1>"),
            ]
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        raw = blobs.read_bytes(snap.body_blobs["p1"])
        assert raw == b"<h1>Test</h1>"

    def test_snapshot_pages_have_empty_body_storage(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """Snapshot pages must never carry body_storage content."""
        api = FakeAPI(
            pages=[
                Page(id="p1", title="P1", space_id="S1", body_storage="<p>content</p>"),
            ]
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        for page in snap.pages:
            assert page.body_storage == "", (
                f"page {page.id} has body_storage in snapshot"
            )

    def test_mixed_pages(self, tmp_root: Path, blobs: BlobStore) -> None:
        """Some pages have inline body, others need fetch."""
        api = FakeAPI(
            pages=[
                Page(id="p1", title="P1", space_id="S1", body_storage="<p>inline</p>"),
                Page(id="p2", title="P2", space_id="S1", body_storage=""),
            ]
        )
        api._body_overrides = {"p2": "<p>fetched</p>"}
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert "p2" in api.get_page_body_calls
        assert "p1" not in api.get_page_body_calls
        assert "p1" in snap.body_blobs
        assert "p2" in snap.body_blobs


# ---------------------------------------------------------------------------
# Incremental attachment skip
# ---------------------------------------------------------------------------


class TestIncrementalAttachments:
    def _make_attachment(self, att_id: str, version: int = 1) -> Attachment:
        return Attachment(
            id=att_id,
            title=f"file_{att_id}.png",
            media_type="image/png",
            page_id="p1",
            download_url=f"http://fake/download/{att_id}",
            version=PageVersion(number=version),
        )

    def test_body_fetch_failure_carries_prev_body(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """A transient body-fetch failure must NOT blank a previously-good page:
        carry the prev body blob so the fingerprint is unchanged and the
        last-good markdown is preserved."""
        from conex.errors import ApiError

        good = blobs.add_bytes(b"Important content")
        prev = Snapshot(body_blobs={"p1": good})

        class FailBodyAPI(FakeAPI):
            def get_page_body(self, page_id: str) -> str:
                raise ApiError("HTTP 404", status=404, url="x")

        api = FailBodyAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="")],
            attachments={},
        )
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts())
        assert snap.body_blobs["p1"] == good  # carried prev, not an empty blob

    def test_body_fetch_failure_is_best_effort(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """A single page-body fetch failure must NOT abort the whole pull — the
        page gets an empty body and the export proceeds (v1 parity)."""
        from conex.errors import ApiError

        class FailBodyAPI(FakeAPI):
            def get_page_body(self, page_id: str) -> str:
                raise ApiError("HTTP 404", status=404, url="x")

        api = FailBodyAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="")],
            attachments={},
        )
        # Must not raise.
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert "p1" in snap.body_blobs
        assert blobs.read_bytes(snap.body_blobs["p1"]) == b""

    def test_no_media_carries_prev_attachment_blobs(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """fetch_media=False (the diff path) must carry prev.attachment_blobs
        forward — persisting an empty map would let a later build's GC delete
        the blobs out from under the exported .media/ files."""
        existing_digest = blobs.add_bytes(b"some-bytes")
        prev = Snapshot(attachment_blobs={"a1@3": existing_digest})
        att = self._make_attachment("a1", version=3)
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts(fetch_media=False))
        assert snap.attachment_blobs.get("a1@3") == existing_digest
        assert att.download_url not in api.download_calls  # no download attempted

    def test_incremental_skip_when_blob_present(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """An attachment already in prev.attachment_blobs + blobs.has() is skipped."""
        att = self._make_attachment("a1", version=3)
        # Pre-populate the blob store.
        existing_digest = blobs.add_bytes(b"original-bytes")

        prev = Snapshot(
            attachment_blobs={"a1@3": existing_digest},
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts(fetch_media=True))
        # download() must not have been called for the skipped attachment.
        assert "http://fake/download/a1" not in api.download_calls
        assert snap.attachment_blobs.get("a1@3") == existing_digest

    def test_version_bump_triggers_redownload(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """Version 4 is not in prev (which has version 3) -> re-download."""
        att_v4 = self._make_attachment("a1", version=4)
        old_digest = blobs.add_bytes(b"old-bytes")
        prev = Snapshot(attachment_blobs={"a1@3": old_digest})

        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att_v4]},
            download_data={att_v4.download_url: b"new-bytes"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts(fetch_media=True))
        assert att_v4.download_url in api.download_calls
        new_digest = snap.attachment_blobs.get("a1@4")
        assert new_digest is not None
        assert blobs.read_bytes(new_digest) == b"new-bytes"

    def test_blob_absent_from_store_triggers_redownload(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """prev lists the att_key but the blob is gone -> re-download."""
        att = self._make_attachment("a1", version=2)
        # prev references a digest that doesn't exist in blobs
        prev = Snapshot(attachment_blobs={"a1@2": "nonexistent-digest"})

        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_data={att.download_url: b"fresh-bytes"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts(fetch_media=True))
        assert att.download_url in api.download_calls
        assert "a1@2" in snap.attachment_blobs

    def test_no_prev_downloads_all(self, tmp_root: Path, blobs: BlobStore) -> None:
        atts = [
            self._make_attachment("a1", 1),
            self._make_attachment("a2", 1),
        ]
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": atts},
            download_data={
                atts[0].download_url: b"bytes-a1",
                atts[1].download_url: b"bytes-a2",
            },
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert "a1@1" in snap.attachment_blobs
        assert "a2@1" in snap.attachment_blobs


# ---------------------------------------------------------------------------
# Download failure -> warning + attachments_complete=False + no blob entry
# ---------------------------------------------------------------------------


class TestDownloadFailure:
    def test_failure_sets_attachments_complete_false(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        att = Attachment(
            id="bad",
            title="broken.png",
            page_id="p1",
            download_url="http://fake/broken.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_error={"http://fake/broken.png": OSError("connection refused")},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert snap.attachments_complete is False

    def test_failure_emits_stderr_warning(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        att = Attachment(
            id="bad",
            title="broken.png",
            page_id="p1",
            download_url="http://fake/broken.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_error={"http://fake/broken.png": RuntimeError("timeout")},
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "broken.png" in captured.err

    def test_failure_produces_no_blob_entry(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        att = Attachment(
            id="bad",
            title="broken.png",
            page_id="p1",
            download_url="http://fake/broken.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_error={"http://fake/broken.png": OSError("gone")},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert "bad@1" not in snap.attachment_blobs

    def test_partial_failure_complete_false(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        """One success + one failure -> attachments_complete=False."""
        att_ok = Attachment(
            id="ok",
            title="good.png",
            page_id="p1",
            download_url="http://fake/good.png",
            version=PageVersion(number=1),
        )
        att_bad = Attachment(
            id="bad",
            title="bad.png",
            page_id="p1",
            download_url="http://fake/bad.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att_ok, att_bad]},
            download_data={"http://fake/good.png": b"ok-bytes"},
            download_error={"http://fake/bad.png": OSError("fail")},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert snap.attachments_complete is False
        assert "ok@1" in snap.attachment_blobs
        assert "bad@1" not in snap.attachment_blobs

    def test_all_success_complete_true(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        att = Attachment(
            id="a1",
            title="ok.png",
            page_id="p1",
            download_url="http://fake/ok.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_data={"http://fake/ok.png": b"ok"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert snap.attachments_complete is True

    def test_pull_never_raises_for_download_failure(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """pull() must not propagate download exceptions."""
        att = Attachment(
            id="bad",
            title="bad.png",
            page_id="p1",
            download_url="http://fake/bad.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_error={"http://fake/bad.png": Exception("unrecoverable")},
        )
        # Must not raise
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert snap is not None


# ---------------------------------------------------------------------------
# derived_blobs carry-forward
# ---------------------------------------------------------------------------


class TestDerivedBlobsCarryForward:
    def test_derived_blobs_carried_from_prev(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        prev = Snapshot(
            derived_blobs={
                "drawio-png:v1:aabbcc": "digest-of-rendered-png",
                "drawio-png:v1:ddeeff": "digest-of-other-png",
            }
        )
        api = FakeAPI(pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")])
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts())
        assert snap.derived_blobs == prev.derived_blobs

    def test_no_prev_empty_derived_blobs(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")])
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert snap.derived_blobs == {}

    def test_derived_blobs_not_modified_by_pull(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """pull() carries derived_blobs verbatim — no additions or deletions."""
        prev = Snapshot(
            derived_blobs={"drawio-png:v1:cafebabe": "somedigest"}
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={
                "p1": [
                    Attachment(
                        id="a1",
                        title="file.drawio",
                        page_id="p1",
                        download_url="http://fake/file.drawio",
                        version=PageVersion(number=1),
                    )
                ]
            },
            download_data={"http://fake/file.drawio": b"<mxGraph/>"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, prev, _default_opts(fetch_media=True))
        # Only the exact key from prev should be in derived_blobs.
        assert "drawio-png:v1:cafebabe" in snap.derived_blobs
        assert snap.derived_blobs["drawio-png:v1:cafebabe"] == "somedigest"


# ---------------------------------------------------------------------------
# Archived mode
# ---------------------------------------------------------------------------


class TestArchivedMode:
    def test_include_archived_false_passes_false_to_api(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(returns_archived=True)
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(include_archived=False))
        _, _, archived_arg = api.get_pages_calls[0]
        assert archived_arg is False

    def test_include_archived_true_passes_true_when_supported(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(returns_archived=True)
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(include_archived=True))
        _, _, archived_arg = api.get_pages_calls[0]
        assert archived_arg is True

    def test_include_archived_true_downgraded_when_not_supported(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        """returns_archived=False: warn + record include_archived=False."""
        api = FakeAPI(returns_archived=False)
        snap = pull(
            api,
            "TEST",
            tmp_root,
            blobs,
            None,
            _default_opts(include_archived=True),
        )
        assert snap.include_archived is False
        captured = capsys.readouterr()
        assert "archived" in captured.err.lower()

    def test_archived_downgrade_passes_false_to_api(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        """After downgrade the API is called with include_archived=False."""
        api = FakeAPI(returns_archived=False)
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(include_archived=True))
        _, _, archived_arg = api.get_pages_calls[0]
        assert archived_arg is False

    def test_snapshot_include_archived_true_when_supported(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(returns_archived=True)
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(include_archived=True))
        assert snap.include_archived is True


# ---------------------------------------------------------------------------
# Users map
# ---------------------------------------------------------------------------


class TestUsersMap:
    def test_users_populated_from_page_authors(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id="u1"),
                )
            ],
            users={"u1": "Alice"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert snap.users.get("u1") == "Alice"

    def test_users_populated_from_attachment_authors(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        att = Attachment(
            id="a1",
            title="file.png",
            page_id="p1",
            download_url="http://fake/file.png",
            version=PageVersion(number=1, author_id="u2"),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_data={"http://fake/file.png": b"bytes"},
            users={"u2": "Bob"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert snap.users.get("u2") == "Bob"

    def test_author_lookup_false_skips_lookups(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id="u1"),
                )
            ],
            users={"u1": "Alice"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=False))
        assert snap.users == {}
        assert api.get_user_display_name_calls == []

    def test_empty_author_id_not_looked_up(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id=""),
                )
            ]
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert api.get_user_display_name_calls == []

    def test_deduplication_of_author_ids(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """Same author_id on multiple pages -> single lookup call."""
        api = FakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id="u1"),
                ),
                Page(
                    id="p2",
                    title="P2",
                    space_id="S1",
                    body_storage="y",
                    version=PageVersion(number=1, author_id="u1"),
                ),
            ],
            users={"u1": "Alice"},
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert api.get_user_display_name_calls.count("u1") == 1

    def test_unknown_author_not_in_users(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """An author_id that returns "" (unknown) is excluded from users."""
        api = FakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id="unknown-id"),
                )
            ],
            users={},  # empty -> get_user_display_name returns ""
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert "unknown-id" not in snap.users


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------


class TestSnapshotPersistence:
    def test_snapshot_saved_and_loadable(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(
            pages=[Page(id="p1", title="Home", space_id="S1", body_storage="x")],
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        store = SnapshotStore(tmp_root)
        loaded = store.load()
        assert loaded is not None
        assert loaded.space.key == snap.space.key
        assert loaded.fetched_at == snap.fetched_at
        assert loaded.body_blobs == snap.body_blobs

    def test_saved_snapshot_has_correct_page_ids(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(
            pages=[
                Page(id="p1", title="P1", space_id="S1", body_storage="a"),
                Page(id="p2", title="P2", space_id="S1", body_storage="b"),
            ]
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        loaded = SnapshotStore(tmp_root).load()
        assert loaded is not None
        ids = {p.id for p in loaded.pages}
        assert ids == {"p1", "p2"}

    def test_snapshot_body_blobs_resolvable_after_load(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="<h1>X</h1>")]
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        loaded = SnapshotStore(tmp_root).load()
        assert loaded is not None
        assert "p1" in loaded.body_blobs
        raw = blobs.read_bytes(loaded.body_blobs["p1"])
        assert raw == b"<h1>X</h1>"

    def test_second_pull_overwrites_snapshot(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api1 = FakeAPI(
            pages=[Page(id="p1", title="V1", space_id="S1", body_storage="v1")]
        )
        snap1 = pull(api1, "TEST", tmp_root, blobs, None, _default_opts())

        api2 = FakeAPI(
            space=Space(id="S1", key="TEST", name="Test Space"),
            pages=[Page(id="p1", title="V2", space_id="S1", body_storage="v2")]
        )
        snap2 = pull(api2, "TEST", tmp_root, blobs, None, _default_opts())

        loaded = SnapshotStore(tmp_root).load()
        assert loaded is not None
        # Should match snap2's fetched_at (more recent).
        assert loaded.fetched_at == snap2.fetched_at


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_body_blobs_keys_complete(self, tmp_root: Path, blobs: BlobStore) -> None:
        """All page ids have a body_blobs entry regardless of thread ordering."""
        page_ids = [f"p{i}" for i in range(10)]
        pages = [
            Page(id=pid, title=pid, space_id="S1", body_storage=f"body-{pid}")
            for pid in page_ids
        ]
        api = FakeAPI(pages=pages)
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert set(snap.body_blobs.keys()) == set(page_ids)

    def test_body_blobs_digests_correct(self, tmp_root: Path, blobs: BlobStore) -> None:
        """Each body_blob digest resolves to the correct page body."""
        pages = [
            Page(id="p1", title="P1", space_id="S1", body_storage="<p>alpha</p>"),
            Page(id="p2", title="P2", space_id="S1", body_storage="<p>beta</p>"),
        ]
        api = FakeAPI(pages=pages)
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert blobs.read_bytes(snap.body_blobs["p1"]) == b"<p>alpha</p>"
        assert blobs.read_bytes(snap.body_blobs["p2"]) == b"<p>beta</p>"

    def test_attachment_blobs_all_present(self, tmp_root: Path, blobs: BlobStore) -> None:
        atts = [
            Attachment(
                id=f"a{i}",
                title=f"file{i}.png",
                page_id="p1",
                download_url=f"http://fake/a{i}.png",
                version=PageVersion(number=1),
            )
            for i in range(5)
        ]
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": atts},
            download_data={f"http://fake/a{i}.png": f"bytes-{i}".encode() for i in range(5)},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        for i in range(5):
            assert f"a{i}@1" in snap.attachment_blobs


# ---------------------------------------------------------------------------
# fetch_media=False
# ---------------------------------------------------------------------------


class TestFetchMediaFalse:
    def test_no_download_calls_when_media_false(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        att = Attachment(
            id="a1",
            title="file.png",
            page_id="p1",
            download_url="http://fake/file.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=False))
        assert api.download_calls == []

    def test_attachment_blobs_empty_when_media_false(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        att = Attachment(
            id="a1",
            title="file.png",
            page_id="p1",
            download_url="http://fake/file.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=False))
        assert snap.attachment_blobs == {}
        assert snap.attachments_complete is True

    def test_attachments_listed_even_when_media_false(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """Attachment metadata is always fetched; only binaries are skipped."""
        att = Attachment(
            id="a1",
            title="file.png",
            page_id="p1",
            download_url="http://fake/file.png",
            version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=False))
        assert "p1" in snap.attachments
        assert len(snap.attachments["p1"]) == 1


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


class TestFolders:
    def test_folders_in_snapshot(self, tmp_root: Path, blobs: BlobStore) -> None:
        api = FakeAPI(
            folders=[
                Folder(id="f1", title="Guides", parent_id=""),
                Folder(id="f2", title="Reference", parent_id="f1"),
            ]
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert len(snap.folders) == 2
        ids = {f.id for f in snap.folders}
        assert ids == {"f1", "f2"}

    def test_get_folders_called_with_space_id(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(space=Space(id="SPACEID", key="TEST", name="Test"))
        pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert api.get_folders_calls == ["SPACEID"]


# ---------------------------------------------------------------------------
# Empty space (no pages, no attachments)
# ---------------------------------------------------------------------------


class TestEmptySpace:
    def test_empty_space_returns_valid_snapshot(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        api = FakeAPI(pages=[], folders=[], attachments={})
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts())
        assert snap.pages == []
        assert snap.folders == []
        assert snap.body_blobs == {}
        assert snap.attachment_blobs == {}
        assert snap.attachments_complete is True


# ---------------------------------------------------------------------------
# Determinism — persisted snapshot.json is byte-identical across runs
# regardless of thread-pool completion order (BLOCKER fix verification)
# ---------------------------------------------------------------------------


class TestDeterminismBytes:
    def test_snapshot_json_sorted_keys(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """body_blobs, attachment_blobs, and users must be sorted so the
        persisted snapshot.json is byte-identical across runs with different
        thread-pool completion orders."""
        import json

        # Use page ids that would have different dict-insertion orders
        # depending on which future completes first (z > a lexicographically).
        pages = [
            Page(id="z9", title="Z9", space_id="S1", body_storage=""),
            Page(id="a1", title="A1", space_id="S1", body_storage=""),
            Page(id="m5", title="M5", space_id="S1", body_storage=""),
        ]
        api = FakeAPI(pages=pages)
        api._body_overrides = {
            "z9": "<p>z</p>",
            "a1": "<p>a</p>",
            "m5": "<p>m</p>",
        }
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(workers=3))

        store = SnapshotStore(tmp_root)
        loaded = store.load()
        assert loaded is not None

        body_keys = list(loaded.body_blobs.keys())
        assert body_keys == sorted(body_keys), (
            f"body_blobs keys are not sorted: {body_keys}"
        )

    def test_attachment_blobs_sorted(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """attachment_blobs keys must be sorted in the persisted snapshot."""
        import json

        atts = [
            Attachment(
                id=f"att_{c}",
                title=f"{c}.png",
                page_id="p1",
                download_url=f"http://fake/{c}.png",
                version=PageVersion(number=1),
            )
            for c in ["z", "a", "m", "b", "y"]
        ]
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": atts},
            download_data={f"http://fake/{c}.png": f"bytes-{c}".encode() for c in ["z", "a", "m", "b", "y"]},
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True, workers=4))

        loaded = SnapshotStore(tmp_root).load()
        assert loaded is not None
        att_keys = list(loaded.attachment_blobs.keys())
        assert att_keys == sorted(att_keys), (
            f"attachment_blobs keys are not sorted: {att_keys}"
        )

    def test_users_sorted(self, tmp_root: Path, blobs: BlobStore) -> None:
        """users dict must be sorted in the persisted snapshot."""
        pages = [
            Page(
                id=f"p{i}",
                title=f"P{i}",
                space_id="S1",
                body_storage="x",
                version=PageVersion(number=1, author_id=uid),
            )
            for i, uid in enumerate(["u_z", "u_a", "u_m"])
        ]
        api = FakeAPI(
            pages=pages,
            users={"u_z": "Zara", "u_a": "Alice", "u_m": "Mike"},
        )
        pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True, workers=3))

        loaded = SnapshotStore(tmp_root).load()
        assert loaded is not None
        user_keys = list(loaded.users.keys())
        assert user_keys == sorted(user_keys), (
            f"users keys are not sorted: {user_keys}"
        )


# ---------------------------------------------------------------------------
# Author prefetch crash-safety (BLOCKER fix verification)
# ---------------------------------------------------------------------------


class TestAuthorLookupCrashSafety:
    def test_raising_user_lookup_does_not_abort_pull(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """A get_user_display_name that raises must not propagate — pull
        completes normally and the failing author is simply absent from users."""

        class RaisingFakeAPI(FakeAPI):
            def get_user_display_name(self, account_id: str) -> str:
                self.get_user_display_name_calls.append(account_id)
                raise RuntimeError("user service unavailable")

        api = RaisingFakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id="u1"),
                )
            ],
        )
        # Must not raise even though the lookup always raises.
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert snap is not None
        # The failed author must not appear in users.
        assert "u1" not in snap.users

    def test_partial_user_lookup_failure_keeps_successes(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """When one user lookup fails, successful lookups are still recorded."""

        class PartialRaisingFakeAPI(FakeAPI):
            def get_user_display_name(self, account_id: str) -> str:
                self.get_user_display_name_calls.append(account_id)
                if account_id == "u_bad":
                    raise RuntimeError("not found")
                return self._users.get(account_id, "")

        api = PartialRaisingFakeAPI(
            pages=[
                Page(
                    id="p1",
                    title="P1",
                    space_id="S1",
                    body_storage="x",
                    version=PageVersion(number=1, author_id="u_ok"),
                ),
                Page(
                    id="p2",
                    title="P2",
                    space_id="S1",
                    body_storage="y",
                    version=PageVersion(number=1, author_id="u_bad"),
                ),
            ],
            users={"u_ok": "Alice"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(author_lookup=True))
        assert snap is not None
        assert snap.users.get("u_ok") == "Alice"
        assert "u_bad" not in snap.users


# ---------------------------------------------------------------------------
# attachment_download_url delegation (MAJOR fix verification)
# ---------------------------------------------------------------------------


class TestAttachmentDownloadUrl:
    def test_pull_uses_attachment_download_url(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """pull must call attachment_download_url(att) and pass its result to
        download(), not use att.download_url directly."""

        resolved_urls: list[str] = []

        class TrackingFakeAPI(FakeAPI):
            def attachment_download_url(self, att: Attachment) -> str:
                resolved = f"http://resolved/{att.id}"
                resolved_urls.append(resolved)
                return resolved

        att = Attachment(
            id="a1",
            title="file.png",
            page_id="p1",
            download_url="http://SHOULD-NOT-BE-USED/a1",
            version=PageVersion(number=1),
        )
        api = TrackingFakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_data={"http://resolved/a1": b"data"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))

        assert "http://resolved/a1" in api.download_calls, (
            "pull must download via attachment_download_url result"
        )
        assert "http://SHOULD-NOT-BE-USED/a1" not in api.download_calls, (
            "pull must not use raw att.download_url"
        )
        assert "a1@1" in snap.attachment_blobs

    def test_empty_url_from_attachment_download_url_warns_and_skips(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        """When attachment_download_url returns '', pull warns and skips the
        download without raising; attachments_complete is set to False."""

        class NoUrlFakeAPI(FakeAPI):
            def attachment_download_url(self, att: Attachment) -> str:
                return ""

        att = Attachment(
            id="a1",
            title="nouri.png",
            page_id="p1",
            download_url="http://original/a1",
            version=PageVersion(number=1),
        )
        api = NoUrlFakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert snap.attachments_complete is False
        assert "a1@1" not in snap.attachment_blobs
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()


# ---------------------------------------------------------------------------
# Content-Encoding decode (BLOCKER fix verification)
# ---------------------------------------------------------------------------


class TestContentEncodingDecode:
    def test_gzip_encoded_response_stored_as_decompressed_bytes(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """Regression (BLOCKER): when the download response has a raw stream
        with decode_content=False and returns gzip-compressed bytes, pull must
        decompress them before storing in the blob store.  The stored blob must
        equal the original (decoded) bytes, not the raw gzip stream.

        This replicates the exact failure mode: urllib3's HTTPResponse is
        constructed with decode_content=False (requests' HTTPAdapter default),
        so reading resp.raw directly yields compressed bytes.  pull() must set
        decode_content=True before calling blobs.add_stream(resp.raw).
        """
        import gzip

        original_bytes = b"SVG diagram content \xc3\xa9\xc3\xa0"  # UTF-8, non-ASCII
        compressed_bytes = gzip.compress(original_bytes)

        # Build a raw stream that mimics urllib3's behaviour with
        # decode_content=False: the stream returns the COMPRESSED bytes by
        # default, but honours decode_content when set to True.
        class MockRawStream(io.RawIOBase):
            """Mimics urllib3 HTTPResponse.raw with decode_content toggle."""

            def __init__(self, raw_data: bytes) -> None:
                self._raw = raw_data
                self._decoded = original_bytes
                self.decode_content: bool = False
                self._pos = 0

            def read(self, n: int = -1) -> bytes:
                data = self._decoded if self.decode_content else self._raw
                if n == -1:
                    chunk = data[self._pos:]
                    self._pos = len(data)
                else:
                    chunk = data[self._pos: self._pos + n]
                    self._pos += len(chunk)
                return chunk

            def readable(self) -> bool:
                return True

        raw_stream = MockRawStream(compressed_bytes)

        class GzipFakeAPI(FakeAPI):
            def download(self, url: str) -> requests.Response:
                self.download_calls.append(url)
                resp = requests.Response()
                resp.status_code = 200
                resp.raw = raw_stream
                return resp

        att = Attachment(
            id="svg1",
            title="diagram.svg",
            media_type="image/svg+xml",
            page_id="p1",
            download_url="http://fake/diagram.svg",
            version=PageVersion(number=1),
        )
        api = GzipFakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
        )

        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))

        assert "svg1@1" in snap.attachment_blobs, (
            "attachment must be recorded in attachment_blobs"
        )
        stored = blobs.read_bytes(snap.attachment_blobs["svg1@1"])
        assert stored == original_bytes, (
            "stored blob must contain the DECOMPRESSED bytes, not the raw gzip stream; "
            f"got first 4 bytes {stored[:4]!r} (gzip magic is b'\\x1f\\x8b')"
        )
        assert stored[:2] != b"\x1f\x8b", (
            "stored blob must NOT start with gzip magic bytes — it was stored compressed"
        )


# ---------------------------------------------------------------------------
# W2: editor-cruft (temp/lock) attachments filtered during pull
# ---------------------------------------------------------------------------


class TestNoiseAttachmentFilter:
    def test_temp_and_lock_attachments_filtered(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        """Office lock (~$x.xlsx) and draw.io autosave (~x.drawio.tmp) titles are
        editor cruft: pull must drop them so they are never downloaded,
        materialized, or surfaced — while a genuine sibling attachment survives."""
        good = Attachment(
            id="ok", title="good.png", page_id="p1",
            download_url="http://fake/good.png", version=PageVersion(number=1),
        )
        lock = Attachment(
            id="lk", title="~$report.xlsx", page_id="p1",
            download_url="http://fake/lock", version=PageVersion(number=1),
        )
        autosave = Attachment(
            id="tm", title="~mydiagram.drawio.tmp", page_id="p1",
            download_url="http://fake/tmp", version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P", space_id="S1", body_storage="x")],
            attachments={"p1": [good, lock, autosave]},
            download_data={"http://fake/good.png": b"g"},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))

        titles = {a.title for a in snap.attachments.get("p1", [])}
        assert titles == {"good.png"}, "only the genuine attachment must survive"
        assert "ok@1" in snap.attachment_blobs
        assert "lk@1" not in snap.attachment_blobs
        assert "tm@1" not in snap.attachment_blobs


# ---------------------------------------------------------------------------
# Attachment-LISTING failure marks the snapshot incomplete (best-effort)
# ---------------------------------------------------------------------------


class TestListingFailure:
    class _ListingFailAPI(FakeAPI):
        def __init__(self, *args: Any, fail_page_id: str, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._fail_page_id = fail_page_id

        def get_attachments(self, page_id: str) -> list[Attachment]:
            self.get_attachments_calls.append(page_id)
            if page_id == self._fail_page_id:
                raise RuntimeError("listing blew up")
            return list(self._attachments.get(page_id, []))

    def test_listing_failure_marks_incomplete_and_keeps_page(
        self, tmp_root: Path, blobs: BlobStore, capsys: pytest.CaptureFixture
    ) -> None:
        good_att = Attachment(
            id="ok", title="good.png", page_id="p2",
            download_url="http://fake/good.png", version=PageVersion(number=1),
        )
        api = self._ListingFailAPI(
            pages=[
                Page(id="p1", title="Broken Listing", space_id="S1", body_storage="x"),
                Page(id="p2", title="Fine", space_id="S1", body_storage="y"),
            ],
            attachments={"p2": [good_att]},
            download_data={"http://fake/good.png": b"ok-bytes"},
            fail_page_id="p1",
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))

        # A failed listing must mark the snapshot incomplete so a later build
        # never prunes existing media on a partial listing.
        assert snap.attachments_complete is False
        # Best-effort: both pages still exported; the healthy page's attachment
        # still downloaded (no whole-export abort).
        assert {p.id for p in snap.pages} == {"p1", "p2"}
        assert "ok@1" in snap.attachment_blobs
        assert blobs.read_bytes(snap.attachment_blobs["ok@1"]) == b"ok-bytes"
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "Broken Listing" in captured.err


# ---------------------------------------------------------------------------
# Archived pages: current-only by default (v1 parity); included on request
# ---------------------------------------------------------------------------


class TestArchivedDefault:
    def _api(self) -> "FakeAPI":
        # A v2-style API (returns_archived=True) that returns archived pages
        # regardless of the request — pull must filter them when not requested.
        return FakeAPI(
            returns_archived=True,
            pages=[
                Page(id="p1", title="Live", space_id="S1", body_storage="<p>live</p>"),
                Page(id="p2", title="Old", space_id="S1", status="archived",
                     body_storage="<p>old</p>"),
            ],
        )

    def test_archived_excluded_by_default(self, tmp_root: Path, blobs: BlobStore) -> None:
        snap = pull(self._api(), "TEST", tmp_root, blobs, None,
                    _default_opts(include_archived=False))
        ids = {p.id for p in snap.pages}
        assert ids == {"p1"}, "a plain export must be current-only (no archived)"
        assert snap.include_archived is False

    def test_archived_included_when_requested(self, tmp_root: Path, blobs: BlobStore) -> None:
        snap = pull(self._api(), "TEST", tmp_root, blobs, None,
                    _default_opts(include_archived=True))
        ids = {p.id for p in snap.pages}
        assert ids == {"p1", "p2"}, "--include-archived must keep archived pages"
        assert snap.include_archived is True


# ---------------------------------------------------------------------------
# Bug 2: pull-stage failures are recorded on the snapshot (surfaced by the CLI)
# ---------------------------------------------------------------------------


class TestPullWarningsRecorded:
    def test_download_failure_recorded_in_snapshot_warnings(
        self, tmp_root: Path, blobs: BlobStore
    ) -> None:
        att = Attachment(
            id="bad", title="broken.png", page_id="p1",
            download_url="http://fake/broken.png", version=PageVersion(number=1),
        )
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
            attachments={"p1": [att]},
            download_error={"http://fake/broken.png": RuntimeError("timeout")},
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert any("broken.png" in w for w in snap.warnings), snap.warnings

    def test_clean_run_has_no_warnings(self, tmp_root: Path, blobs: BlobStore) -> None:
        api = FakeAPI(
            pages=[Page(id="p1", title="P1", space_id="S1", body_storage="x")],
        )
        snap = pull(api, "TEST", tmp_root, blobs, None, _default_opts(fetch_media=True))
        assert snap.warnings == []
