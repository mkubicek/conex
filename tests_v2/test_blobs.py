"""Tests for conex.store.blobs.BlobStore.

Coverage targets (per SPEC-V2.md):
- digest correctness (SHA-256)
- dedup: adding the same content twice yields the same digest and only one
  on-disk file
- atomic promote: no partial files are ever visible outside .conex/tmp
- materialize mtime: os.utime is called when mtime is supplied
- gc keep-set: blobs NOT in keep are removed; blobs IN keep are preserved
- materialize containment: dest outside the export root is rejected
- KeyError on path()/read_bytes()/materialize() for absent digest
- has() reflects add/gc correctly
- add_bytes convenience wrapper
- store lazily creates .conex/blobs/ and .conex/tmp/ directories
- GC on empty store returns 0 without error
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

import pytest

from conex.store.blobs import BlobStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def export_root(tmp_path: Path) -> Path:
    """A subdirectory of tmp_path acting as the export root.

    Using a subdirectory (not tmp_path itself) means tmp_path / "outside.txt"
    is genuinely outside the export root for containment tests.
    """
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture()
def store(export_root: Path) -> BlobStore:
    return BlobStore(export_root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Digest correctness
# ---------------------------------------------------------------------------


class TestDigestCorrectness:
    def test_add_stream_returns_correct_digest(self, store: BlobStore) -> None:
        data = b"hello world"
        expected = _sha256(data)
        digest, size = store.add_stream(io.BytesIO(data))
        assert digest == expected
        assert size == len(data)

    def test_add_bytes_returns_correct_digest(self, store: BlobStore) -> None:
        data = b"hello bytes"
        expected = _sha256(data)
        assert store.add_bytes(data) == expected

    def test_add_stream_empty(self, store: BlobStore) -> None:
        expected = _sha256(b"")
        digest, size = store.add_stream(io.BytesIO(b""))
        assert digest == expected
        assert size == 0

    def test_blob_stored_at_canonical_path(self, store: BlobStore, export_root: Path) -> None:
        data = b"path check"
        digest, _ = store.add_stream(io.BytesIO(data))
        expected_path = export_root / ".conex" / "blobs" / digest[:2] / digest
        assert expected_path.exists()
        assert expected_path.read_bytes() == data

    def test_read_bytes_returns_stored_content(self, store: BlobStore) -> None:
        data = b"read me back"
        digest = store.add_bytes(data)
        assert store.read_bytes(digest) == data

    def test_large_stream(self, store: BlobStore) -> None:
        """Stream larger than the 65 536-byte read chunk."""
        data = os.urandom(200_000)
        expected = _sha256(data)
        digest, size = store.add_stream(io.BytesIO(data))
        assert digest == expected
        assert size == len(data)
        assert store.read_bytes(digest) == data


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_content_returns_same_digest(self, store: BlobStore) -> None:
        data = b"duplicate content"
        d1 = store.add_bytes(data)
        d2 = store.add_bytes(data)
        assert d1 == d2

    def test_no_extra_blob_file_on_dedup(self, store: BlobStore, export_root: Path) -> None:
        data = b"dedup data"
        digest = store.add_bytes(data)
        blob_path = export_root / ".conex" / "blobs" / digest[:2] / digest

        mtime_before = blob_path.stat().st_mtime_ns
        store.add_bytes(data)
        mtime_after = blob_path.stat().st_mtime_ns

        # File was NOT replaced on the second add (same modification time).
        assert mtime_before == mtime_after

    def test_different_content_different_digest(self, store: BlobStore) -> None:
        d1 = store.add_bytes(b"one")
        d2 = store.add_bytes(b"two")
        assert d1 != d2
        assert store.has(d1)
        assert store.has(d2)


# ---------------------------------------------------------------------------
# Atomic promotion (no partial files outside tmp)
# ---------------------------------------------------------------------------


class TestAtomicPromote:
    def test_no_partial_file_outside_tmp(
        self, store: BlobStore, export_root: Path
    ) -> None:
        """Interrupting a write must leave no orphan file outside .conex/tmp/."""
        data = b"atomic test"
        digest = store.add_bytes(data)

        blob_dir = export_root / ".conex" / "blobs"
        tmp_dir = export_root / ".conex" / "tmp"

        # After a successful add, the file exists only at the canonical path.
        canonical = blob_dir / digest[:2] / digest
        assert canonical.exists()

        # The tmp dir must have NO leftover blob-* files after the add.
        leftover = [f for f in tmp_dir.iterdir() if f.name.startswith("blob-")]
        assert leftover == []

    def test_failed_stream_leaves_no_orphan(
        self, store: BlobStore, export_root: Path
    ) -> None:
        """A stream that raises mid-read must not leave a staging file."""

        class FailingStream:
            def read(self, n: int) -> bytes:
                raise RuntimeError("simulated I/O error")

        with pytest.raises(RuntimeError):
            store.add_stream(FailingStream())  # type: ignore[arg-type]

        tmp_dir = export_root / ".conex" / "tmp"
        if tmp_dir.exists():
            leftover = [f for f in tmp_dir.iterdir() if f.name.startswith("blob-")]
            assert leftover == []

    def test_add_stream_within_max_bytes_ok(self, store: BlobStore) -> None:
        data = b"x" * 100
        digest, size = store.add_stream(io.BytesIO(data), max_bytes=1000)
        assert size == 100
        assert store.has(digest)

    def test_add_stream_exceeding_max_bytes_raises_and_cleans_up(
        self, store: BlobStore, export_root: Path
    ) -> None:
        data = b"x" * 5000
        with pytest.raises(ValueError, match="byte cap"):
            store.add_stream(io.BytesIO(data), max_bytes=1024)
        tmp_dir = export_root / ".conex" / "tmp"
        if tmp_dir.exists():
            leftover = [f for f in tmp_dir.iterdir() if f.name.startswith("blob-")]
            assert leftover == []


# ---------------------------------------------------------------------------
# has() / path() / KeyError
# ---------------------------------------------------------------------------


class TestHasAndPath:
    def test_has_returns_false_before_add(self, store: BlobStore) -> None:
        fake = "a" * 64
        assert not store.has(fake)

    def test_has_returns_true_after_add(self, store: BlobStore) -> None:
        digest = store.add_bytes(b"exists now")
        assert store.has(digest)

    def test_path_raises_key_error_for_absent(self, store: BlobStore) -> None:
        with pytest.raises(KeyError):
            store.path("b" * 64)

    def test_path_returns_valid_path_after_add(self, store: BlobStore) -> None:
        data = b"path test"
        digest = store.add_bytes(data)
        p = store.path(digest)
        assert p.exists()
        assert p.read_bytes() == data

    def test_read_bytes_raises_key_error_for_absent(self, store: BlobStore) -> None:
        with pytest.raises(KeyError):
            store.read_bytes("c" * 64)


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------


class TestMaterialize:
    def test_materialize_copies_content(
        self, store: BlobStore, export_root: Path
    ) -> None:
        data = b"materialize me"
        digest = store.add_bytes(data)
        dest = export_root / "output.txt"
        store.materialize(digest, dest)
        assert dest.read_bytes() == data

    def test_materialize_sets_mtime(
        self, store: BlobStore, export_root: Path
    ) -> None:
        data = b"mtime test"
        digest = store.add_bytes(data)
        dest = export_root / "with_mtime.txt"
        target_mtime = 1_000_000.0  # well in the past
        store.materialize(digest, dest, mtime=target_mtime)
        actual = dest.stat().st_mtime
        assert abs(actual - target_mtime) < 2.0

    def test_materialize_no_mtime_leaves_mtime_recent(
        self, store: BlobStore, export_root: Path
    ) -> None:
        data = b"no mtime"
        digest = store.add_bytes(data)
        dest = export_root / "no_mtime.txt"
        before = time.time() - 1
        store.materialize(digest, dest)
        assert dest.stat().st_mtime >= before

    def test_materialize_raises_key_error_for_absent(
        self, store: BlobStore, export_root: Path
    ) -> None:
        with pytest.raises(KeyError):
            store.materialize("d" * 64, export_root / "ghost.txt")

    def test_materialize_raises_for_dest_outside_root(
        self, store: BlobStore, export_root: Path, tmp_path: Path
    ) -> None:
        data = b"escape attempt"
        digest = store.add_bytes(data)
        # export_root is tmp_path/root; tmp_path itself is outside it.
        outside = tmp_path / "escape.txt"
        with pytest.raises(ValueError, match="escapes root"):
            store.materialize(digest, outside)

    def test_materialize_via_tmp_then_replace(
        self, store: BlobStore, export_root: Path
    ) -> None:
        """dest is created atomically — no half-written file is visible."""
        data = b"atomic materialize"
        digest = store.add_bytes(data)
        dest = export_root / "subdir" / "result.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        store.materialize(digest, dest)
        assert dest.read_bytes() == data

        # tmp dir has no leftover after materialize
        tmp_dir = export_root / ".conex" / "tmp"
        leftover = [f for f in tmp_dir.iterdir() if f.name.startswith("blob-")]
        assert leftover == []

    def test_materialize_overwrites_existing(
        self, store: BlobStore, export_root: Path
    ) -> None:
        dest = export_root / "existing.txt"
        dest.write_bytes(b"old content")
        data = b"new content"
        digest = store.add_bytes(data)
        store.materialize(digest, dest)
        assert dest.read_bytes() == data

    def test_materialize_no_mtime_ignores_blob_mtime(
        self, store: BlobStore, export_root: Path
    ) -> None:
        """materialize(mtime=None) must NOT inherit the blob file's mtime.

        Regression for the copy2-semantics bug: shutil.copy2 preserved the
        blob's stored mtime onto the destination.  shutil.copyfile (content
        only) must yield a fresh mtime even when the blob was aged on disk.
        """
        data = b"stale blob test"
        digest = store.add_bytes(data)

        # Age the blob file to a time far in the past.
        blob_file = store.path(digest)
        ancient = 1_000_000.0  # year ~1982
        os.utime(blob_file, (ancient, ancient))
        assert blob_file.stat().st_mtime == pytest.approx(ancient, abs=2.0)

        dest = export_root / "no_mtime_stale_blob.txt"
        recent_floor = time.time() - 1
        store.materialize(digest, dest)

        dest_mtime = dest.stat().st_mtime
        assert dest_mtime >= recent_floor, (
            f"dest mtime {dest_mtime} should be recent (>= {recent_floor}), "
            f"not the blob's stale mtime {ancient}"
        )


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------


class TestGarbageCollection:
    def test_gc_removes_unreferenced_blobs(
        self, store: BlobStore, export_root: Path
    ) -> None:
        d1 = store.add_bytes(b"keep me")
        d2 = store.add_bytes(b"remove me")
        removed = store.gc(keep={d1})
        assert removed == 1
        assert store.has(d1)
        assert not store.has(d2)

    def test_gc_keeps_all_when_all_referenced(self, store: BlobStore) -> None:
        d1 = store.add_bytes(b"alpha")
        d2 = store.add_bytes(b"beta")
        removed = store.gc(keep={d1, d2})
        assert removed == 0
        assert store.has(d1)
        assert store.has(d2)

    def test_gc_refuses_empty_keep_when_blobs_exist(self, store: BlobStore) -> None:
        """An empty keep set almost always means state failed to load; refuse to
        wipe every blob (the only crash-safe copy of the export content)."""
        d1 = store.add_bytes(b"one")
        d2 = store.add_bytes(b"two")
        with pytest.warns(UserWarning, match="empty keep set"):
            removed = store.gc(keep=set())
        assert removed == 0
        assert store.has(d1)
        assert store.has(d2)

    def test_gc_empty_store_returns_zero(self, store: BlobStore) -> None:
        assert store.gc(keep=set()) == 0

    def test_gc_removes_empty_fanout_dirs(
        self, store: BlobStore, export_root: Path
    ) -> None:
        digest = store.add_bytes(b"ephemeral")
        fanout = export_root / ".conex" / "blobs" / digest[:2]
        assert fanout.exists()
        # Non-empty keep that does not cover this blob → it is GC'd and the
        # now-empty fan-out directory is removed.
        store.gc(keep={"0" * 64})
        assert not fanout.exists()

    def test_gc_keep_set_with_absent_digest_is_no_op(
        self, store: BlobStore
    ) -> None:
        """Digests in keep that do not exist in the store are silently ignored."""
        d1 = store.add_bytes(b"real blob")
        removed = store.gc(keep={d1, "e" * 64})
        assert removed == 0

    def test_gc_partial_keep(self, store: BlobStore) -> None:
        """Only the blobs NOT in keep are removed."""
        blobs = [store.add_bytes(bytes([i]) * 32) for i in range(5)]
        keep = set(blobs[:3])
        removed = store.gc(keep=keep)
        assert removed == 2
        for d in blobs[:3]:
            assert store.has(d)
        for d in blobs[3:]:
            assert not store.has(d)


# ---------------------------------------------------------------------------
# Directory creation (lazy init)
# ---------------------------------------------------------------------------


class TestLazyDirectoryCreation:
    def test_dirs_created_on_first_write(
        self, store: BlobStore, export_root: Path
    ) -> None:
        blobs_dir = export_root / ".conex" / "blobs"
        tmp_dir = export_root / ".conex" / "tmp"
        assert not blobs_dir.exists()
        assert not tmp_dir.exists()
        store.add_bytes(b"trigger creation")
        assert blobs_dir.exists()
        assert tmp_dir.exists()

    def test_tmp_dir_not_cleared_between_calls(
        self, store: BlobStore, export_root: Path
    ) -> None:
        """The store must NEVER clear .conex/tmp (the CLI owns that — I4)."""
        tmp_dir = export_root / ".conex" / "tmp"
        store.add_bytes(b"first")
        sentinel = tmp_dir / "sentinel.txt"
        sentinel.write_text("do not delete me")
        store.add_bytes(b"second")
        assert sentinel.exists(), "store must not clear .conex/tmp"
