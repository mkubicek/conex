"""Tests for conex.store.state — state models + StateStore + SnapshotStore.

Coverage targets (per spec):
- Round-trip save/load for both StateStore and SnapshotStore.
- Corrupt JSON -> None + warning (via stderr or warnings module).
- Explicit-null fields produce defaults (all fields of all models).
- Partial/truncated file -> None (+ warning).
- Atomicity: a simulated failure during save must not corrupt the live file.
- schema_version is present and correct in serialised output.
- Missing file returns None (no warning).
- ExportState.pages round-trips nested PageState + AttachmentState correctly.
- Snapshot with nested Space/Page/Folder/Attachment objects round-trips.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conex.store.state import (
    AttachmentState,
    ExportState,
    PageState,
    Snapshot,
    SnapshotStore,
    StateStore,
)
from conex.models import Attachment, Folder, Page, PageVersion, Space


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    """A temporary export root (no .conex/ yet — tests exercise creation)."""
    return tmp_path


# ---------------------------------------------------------------------------
# AttachmentState
# ---------------------------------------------------------------------------


class TestAttachmentState:
    def test_defaults(self) -> None:
        a = AttachmentState()
        assert a.version == 0
        assert a.file == ""
        assert a.blob == ""
        assert a.size == 0

    def test_explicit_values(self) -> None:
        a = AttachmentState(version=3, file="img.png", blob="abc123", size=4096)
        assert a.version == 3
        assert a.file == "img.png"
        assert a.blob == "abc123"
        assert a.size == 4096

    def test_null_coercion_all_fields(self) -> None:
        """Explicit null on every field resolves to the field default."""
        a = AttachmentState.model_validate(
            {"version": None, "file": None, "blob": None, "size": None}
        )
        assert a.version == 0
        assert a.file == ""
        assert a.blob == ""
        assert a.size == 0

    def test_round_trip(self) -> None:
        original = AttachmentState(version=7, file="doc.pdf", blob="deadbeef", size=512)
        restored = AttachmentState.model_validate(original.model_dump())
        assert restored == original


# ---------------------------------------------------------------------------
# PageState
# ---------------------------------------------------------------------------


class TestPageState:
    def test_defaults(self) -> None:
        p = PageState()
        assert p.dir == ""
        assert p.file == ""
        assert p.html == ""
        assert p.title == ""
        assert p.version == 0
        assert p.status == "current"
        assert p.fingerprint == ""
        assert p.attachments == {}

    def test_null_coercion_all_fields(self) -> None:
        """Explicit null on every field resolves to the field default."""
        p = PageState.model_validate(
            {
                "dir": None,
                "file": None,
                "html": None,
                "title": None,
                "version": None,
                "status": None,
                "fingerprint": None,
                "attachments": None,
            }
        )
        assert p.dir == ""
        assert p.file == ""
        assert p.html == ""
        assert p.title == ""
        assert p.version == 0
        assert p.status == "current"
        assert p.fingerprint == ""
        assert p.attachments == {}

    def test_with_attachments(self) -> None:
        p = PageState(
            dir="Space/Page",
            file="Space/Page/Page.md",
            attachments={"att1": AttachmentState(file="a.png", blob="ff")},
        )
        assert p.attachments["att1"].file == "a.png"

    def test_round_trip(self) -> None:
        p = PageState(
            dir="Docs/Setup",
            file="Docs/Setup/Setup.md",
            html="Docs/Setup/Setup.html",
            title="Setup Guide",
            version=5,
            status="archived",
            fingerprint="cafebabe",
            attachments={"x": AttachmentState(version=2, file="x.pdf", blob="11", size=100)},
        )
        restored = PageState.model_validate(p.model_dump())
        assert restored == p


# ---------------------------------------------------------------------------
# ExportState
# ---------------------------------------------------------------------------


class TestExportState:
    def test_defaults(self) -> None:
        e = ExportState()
        assert e.schema_version == 1
        assert e.space_key == ""
        assert e.space_id == ""
        assert e.updated_at == ""
        assert e.converter_version == 0
        assert e.pages == {}
        assert e.folders == {}

    def test_schema_version_in_dump(self) -> None:
        e = ExportState()
        d = e.model_dump()
        assert "schema_version" in d
        assert d["schema_version"] == 1

    def test_null_coercion_all_fields(self) -> None:
        e = ExportState.model_validate(
            {
                "schema_version": None,
                "space_key": None,
                "space_id": None,
                "updated_at": None,
                "converter_version": None,
                "pages": None,
                "folders": None,
            }
        )
        assert e.schema_version == 1
        assert e.space_key == ""
        assert e.space_id == ""
        assert e.updated_at == ""
        assert e.converter_version == 0
        assert e.pages == {}
        assert e.folders == {}

    def test_full_round_trip(self) -> None:
        state = ExportState(
            space_key="DOCS",
            space_id="42",
            updated_at="2026-06-11T10:00:00Z",
            converter_version=1,
            pages={
                "pg1": PageState(
                    dir="Docs/Page",
                    file="Docs/Page/Page.md",
                    title="Page",
                    version=3,
                    attachments={"a1": AttachmentState(file="img.png", blob="aa", size=200)},
                )
            },
            folders={"f1": "Docs/Folder"},
        )
        restored = ExportState.model_validate(state.model_dump())
        assert restored.space_key == "DOCS"
        assert restored.pages["pg1"].title == "Page"
        assert restored.pages["pg1"].attachments["a1"].blob == "aa"
        assert restored.folders["f1"] == "Docs/Folder"

    def test_is_not_frozen(self) -> None:
        """ExportState must be mutable (build.py mutates copies)."""
        e = ExportState()
        e.space_key = "NEW"
        assert e.space_key == "NEW"

    def test_empty_dict_is_valid(self) -> None:
        e = ExportState.model_validate({})
        assert e.schema_version == 1


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_defaults(self) -> None:
        s = Snapshot()
        assert s.schema_version == 1
        assert s.space == Space()
        assert s.fetched_at == ""
        assert s.include_archived is False
        assert s.attachments_complete is True
        assert s.pages == []
        assert s.folders == []
        assert s.body_blobs == {}
        assert s.attachments == {}
        assert s.attachment_blobs == {}
        assert s.derived_blobs == {}
        assert s.users == {}

    def test_schema_version_in_dump(self) -> None:
        s = Snapshot()
        d = s.model_dump()
        assert "schema_version" in d
        assert d["schema_version"] == 1

    def test_null_coercion_all_fields(self) -> None:
        s = Snapshot.model_validate(
            {
                "schema_version": None,
                "space": None,
                "fetched_at": None,
                "include_archived": None,
                "attachments_complete": None,
                "pages": None,
                "folders": None,
                "body_blobs": None,
                "attachments": None,
                "attachment_blobs": None,
                "derived_blobs": None,
                "users": None,
            }
        )
        assert s.schema_version == 1
        assert s.space == Space()
        assert s.fetched_at == ""
        assert s.include_archived is False
        assert s.attachments_complete is True
        assert s.pages == []
        assert s.folders == []
        assert s.body_blobs == {}
        assert s.attachments == {}
        assert s.attachment_blobs == {}
        assert s.derived_blobs == {}
        assert s.users == {}

    def test_space_defaults_to_empty_space(self) -> None:
        """space field defaults to Space() — not required."""
        s = Snapshot.model_validate({})
        assert s.space.id == ""
        assert s.space.key == ""

    def test_full_round_trip(self) -> None:
        snap = Snapshot(
            space=Space(id="1", key="DOCS", name="Documentation"),
            fetched_at="2026-06-11T12:00:00Z",
            include_archived=True,
            pages=[
                Page(id="p1", title="Hello", space_id="1", version=PageVersion(number=5))
            ],
            folders=[Folder(id="f1", title="Guides", parent_id="")],
            body_blobs={"p1": "deadbeef"},
            attachments={
                "p1": [
                    Attachment(id="a1", title="diagram.png", page_id="p1",
                               media_type="image/png", file_size=1024)
                ]
            },
            attachment_blobs={"a1@5": "cafebabe"},
            derived_blobs={"drawio-png:v1:aabbcc": "11223344"},
            users={"acc1": "Alice"},
        )
        restored = Snapshot.model_validate(snap.model_dump())
        assert restored.space.key == "DOCS"
        assert restored.include_archived is True
        assert len(restored.pages) == 1
        assert restored.pages[0].id == "p1"
        assert restored.pages[0].title == "Hello"
        assert restored.body_blobs["p1"] == "deadbeef"
        assert restored.attachment_blobs["a1@5"] == "cafebabe"
        assert restored.derived_blobs["drawio-png:v1:aabbcc"] == "11223344"
        assert restored.users["acc1"] == "Alice"
        assert len(restored.folders) == 1
        assert restored.folders[0].id == "f1"

    def test_is_not_frozen(self) -> None:
        """Snapshot must be mutable (build.py mutates copies)."""
        s = Snapshot()
        s.fetched_at = "2026-01-01T00:00:00Z"
        assert s.fetched_at == "2026-01-01T00:00:00Z"

    def test_empty_dict_is_valid(self) -> None:
        s = Snapshot.model_validate({})
        assert s.schema_version == 1


# ---------------------------------------------------------------------------
# StateStore — round-trip
# ---------------------------------------------------------------------------


class TestStateStoreRoundTrip:
    def test_save_creates_conex_dirs(self, tmp_root: Path) -> None:
        store = StateStore(tmp_root)
        assert not (tmp_root / ".conex").exists()
        store.save(ExportState(space_key="TEST"))
        assert (tmp_root / ".conex" / "state.json").exists()
        assert (tmp_root / ".conex" / "tmp").exists()

    def test_round_trip(self, tmp_root: Path) -> None:
        state = ExportState(
            space_key="DOCS",
            space_id="99",
            updated_at="2026-06-11T00:00:00Z",
            converter_version=1,
            pages={"p1": PageState(dir="D", file="D/D.md", title="T", version=2)},
            folders={"f1": "FolderDir"},
        )
        store = StateStore(tmp_root)
        store.save(state)
        loaded = store.load()
        assert loaded is not None
        assert loaded.space_key == "DOCS"
        assert loaded.space_id == "99"
        assert loaded.pages["p1"].title == "T"
        assert loaded.folders["f1"] == "FolderDir"

    def test_load_missing_returns_none(self, tmp_root: Path) -> None:
        store = StateStore(tmp_root)
        assert store.load() is None

    def test_schema_version_present_in_file(self, tmp_root: Path) -> None:
        store = StateStore(tmp_root)
        store.save(ExportState())
        raw = json.loads((tmp_root / ".conex" / "state.json").read_text())
        assert raw["schema_version"] == 1


# ---------------------------------------------------------------------------
# StateStore — corrupt/partial JSON -> None + warning
# ---------------------------------------------------------------------------


class TestStateStoreCorrupt:
    def test_corrupt_json_returns_none_and_warns(
        self, tmp_root: Path, capsys: pytest.CaptureFixture
    ) -> None:
        conex_dir = tmp_root / ".conex"
        conex_dir.mkdir(parents=True)
        (conex_dir / "state.json").write_text("{invalid json", encoding="utf-8")

        store = StateStore(tmp_root)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = store.load()

        assert result is None
        # Warning should be issued (via warnings.warn in _load_json)
        assert len(caught) >= 1
        assert any("corrupt" in str(w.message).lower() for w in caught)

    def test_truncated_file_returns_none(
        self, tmp_root: Path
    ) -> None:
        conex_dir = tmp_root / ".conex"
        conex_dir.mkdir(parents=True)
        (conex_dir / "state.json").write_text('{"schema_version": 1, "space_key":', encoding="utf-8")

        store = StateStore(tmp_root)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = store.load()

        assert result is None
        assert len(caught) >= 1

    def test_validation_error_returns_none_and_warns(
        self, tmp_root: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A structurally invalid (but parseable) JSON triggers ValidationError path."""
        conex_dir = tmp_root / ".conex"
        conex_dir.mkdir(parents=True)
        # pages as a list instead of a dict triggers pydantic error
        (conex_dir / "state.json").write_text(
            json.dumps({"schema_version": 1, "pages": "not-a-dict"}),
            encoding="utf-8",
        )

        store = StateStore(tmp_root)
        result = store.load()
        assert result is None
        # Validation error emits to stderr
        captured = capsys.readouterr()
        assert "invalid" in captured.err.lower() or "state" in captured.err.lower()


# ---------------------------------------------------------------------------
# StateStore — atomicity
# ---------------------------------------------------------------------------


class TestStateStoreAtomicity:
    def test_no_partial_state_on_write_failure(self, tmp_root: Path) -> None:
        """If writing the tmp file fails, the live state.json is untouched."""
        store = StateStore(tmp_root)
        # Write an initial good state
        initial = ExportState(space_key="INIT")
        store.save(initial)

        # Now simulate a failure by patching os.replace to raise
        with patch("conex.store.state.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                store.save(ExportState(space_key="BROKEN"))

        # The original file is still intact
        loaded = store.load()
        assert loaded is not None
        assert loaded.space_key == "INIT"

    def test_tmp_file_removed_after_successful_save(self, tmp_root: Path) -> None:
        """After a successful save, no .tmp file lingers."""
        store = StateStore(tmp_root)
        store.save(ExportState(space_key="X"))
        tmp_files = list((tmp_root / ".conex" / "tmp").glob("*.tmp"))
        assert tmp_files == []

    def test_second_save_overwrites_first(self, tmp_root: Path) -> None:
        store = StateStore(tmp_root)
        store.save(ExportState(space_key="FIRST"))
        store.save(ExportState(space_key="SECOND"))
        loaded = store.load()
        assert loaded is not None
        assert loaded.space_key == "SECOND"

    def test_tmp_dir_not_cleared_by_store(self, tmp_root: Path) -> None:
        """The store must not remove pre-existing files in .conex/tmp."""
        conex_tmp = tmp_root / ".conex" / "tmp"
        conex_tmp.mkdir(parents=True)
        sentinel = conex_tmp / "sentinel.txt"
        sentinel.write_text("keep me", encoding="utf-8")

        store = StateStore(tmp_root)
        store.save(ExportState())

        assert sentinel.exists(), ".conex/tmp must not be cleared by the store"


# ---------------------------------------------------------------------------
# SnapshotStore — round-trip
# ---------------------------------------------------------------------------


class TestSnapshotStoreRoundTrip:
    def test_save_creates_conex_dirs(self, tmp_root: Path) -> None:
        store = SnapshotStore(tmp_root)
        assert not (tmp_root / ".conex").exists()
        store.save(Snapshot(fetched_at="2026-06-11T00:00:00Z"))
        assert (tmp_root / ".conex" / "snapshot.json").exists()

    def test_round_trip(self, tmp_root: Path) -> None:
        snap = Snapshot(
            space=Space(id="1", key="DOCS", name="Docs"),
            fetched_at="2026-06-11T12:00:00Z",
            include_archived=True,
            body_blobs={"p1": "aa"},
            users={"u1": "Bob"},
        )
        store = SnapshotStore(tmp_root)
        store.save(snap)
        loaded = store.load()
        assert loaded is not None
        assert loaded.space.key == "DOCS"
        assert loaded.include_archived is True
        assert loaded.body_blobs == {"p1": "aa"}
        assert loaded.users == {"u1": "Bob"}

    def test_load_missing_returns_none(self, tmp_root: Path) -> None:
        store = SnapshotStore(tmp_root)
        assert store.load() is None

    def test_schema_version_in_file(self, tmp_root: Path) -> None:
        store = SnapshotStore(tmp_root)
        store.save(Snapshot())
        raw = json.loads((tmp_root / ".conex" / "snapshot.json").read_text())
        assert raw["schema_version"] == 1


# ---------------------------------------------------------------------------
# SnapshotStore — corrupt/partial JSON -> None + warning
# ---------------------------------------------------------------------------


class TestSnapshotStoreCorrupt:
    def test_corrupt_json_returns_none_and_warns(
        self, tmp_root: Path
    ) -> None:
        conex_dir = tmp_root / ".conex"
        conex_dir.mkdir(parents=True)
        (conex_dir / "snapshot.json").write_text("}{bad", encoding="utf-8")

        store = SnapshotStore(tmp_root)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = store.load()

        assert result is None
        assert len(caught) >= 1
        assert any("corrupt" in str(w.message).lower() for w in caught)

    def test_truncated_file_returns_none(self, tmp_root: Path) -> None:
        conex_dir = tmp_root / ".conex"
        conex_dir.mkdir(parents=True)
        (conex_dir / "snapshot.json").write_text('{"schema_version":', encoding="utf-8")

        store = SnapshotStore(tmp_root)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = store.load()

        assert result is None
        assert len(caught) >= 1

    def test_validation_error_returns_none_and_warns(
        self, tmp_root: Path, capsys: pytest.CaptureFixture
    ) -> None:
        conex_dir = tmp_root / ".conex"
        conex_dir.mkdir(parents=True)
        (conex_dir / "snapshot.json").write_text(
            json.dumps({"schema_version": 1, "pages": "INVALID"}),
            encoding="utf-8",
        )

        store = SnapshotStore(tmp_root)
        result = store.load()
        assert result is None
        captured = capsys.readouterr()
        assert "snapshot" in captured.err.lower() or "invalid" in captured.err.lower()


# ---------------------------------------------------------------------------
# SnapshotStore — atomicity
# ---------------------------------------------------------------------------


class TestSnapshotStoreAtomicity:
    def test_no_partial_snapshot_on_write_failure(self, tmp_root: Path) -> None:
        store = SnapshotStore(tmp_root)
        initial = Snapshot(fetched_at="INIT")
        store.save(initial)

        with patch("conex.store.state.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                store.save(Snapshot(fetched_at="BROKEN"))

        loaded = store.load()
        assert loaded is not None
        assert loaded.fetched_at == "INIT"

    def test_tmp_file_removed_after_successful_save(self, tmp_root: Path) -> None:
        store = SnapshotStore(tmp_root)
        store.save(Snapshot())
        tmp_files = list((tmp_root / ".conex" / "tmp").glob("*.tmp"))
        assert tmp_files == []


# ---------------------------------------------------------------------------
# Explicit null -> default: cross-model coverage
# ---------------------------------------------------------------------------


class TestExplicitNullAllModels:
    """Explicit null on every field of every store model must produce defaults."""

    def test_attachment_state_all_nulls(self) -> None:
        a = AttachmentState.model_validate(
            {"version": None, "file": None, "blob": None, "size": None}
        )
        assert a == AttachmentState()

    def test_page_state_all_nulls(self) -> None:
        p = PageState.model_validate(
            {k: None for k in PageState.model_fields}
        )
        assert p.dir == ""
        assert p.status == "current"
        assert p.attachments == {}

    def test_export_state_all_nulls(self) -> None:
        e = ExportState.model_validate(
            {k: None for k in ExportState.model_fields}
        )
        assert e.schema_version == 1
        assert e.pages == {}
        assert e.folders == {}

    def test_snapshot_all_nulls(self) -> None:
        s = Snapshot.model_validate(
            {k: None for k in Snapshot.model_fields}
        )
        assert s.schema_version == 1
        assert s.space == Space()
        assert s.pages == []
        assert s.body_blobs == {}

    def test_nested_null_in_pages(self) -> None:
        """A null page entry inside an ExportState.pages dict is tolerated."""
        e = ExportState.model_validate(
            {"pages": {"p1": {"version": None, "status": None}}}
        )
        assert e.pages["p1"].version == 0
        assert e.pages["p1"].status == "current"


# ---------------------------------------------------------------------------
# store/__init__.py re-exports
# ---------------------------------------------------------------------------


class TestStoreInitExports:
    def test_all_names_importable_from_store(self) -> None:
        from conex.store import (  # noqa: F401
            AttachmentState,
            ExportState,
            PageState,
            Snapshot,
            SnapshotStore,
            StateStore,
        )
