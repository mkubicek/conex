"""Content-addressed blob store for conex v2.

Layout on disk::

    <root>/.conex/blobs/<aa>/<sha256-hex>

where ``aa`` is the first two hex characters of the digest (fan-out to
avoid directories with thousands of entries).

Invariants:
- I4: all temp files live under ``<root>/.conex/tmp/``; final files appear
  only via ``os.replace`` from that staging area.
- Blobs are immutable after promotion: the same digest always maps to the
  same bytes.  A digest that already exists in the store is a no-op dedup.
- Dest paths passed to :meth:`materialize` are checked with
  ``assert_within`` for root containment before any filesystem operation (I7).
- The store creates ``<root>/.conex/blobs/`` and ``<root>/.conex/tmp/``
  lazily on first use, but NEVER clears ``.conex/tmp/`` — the CLI owns that
  lifecycle (I4 / M6).
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
import warnings
from pathlib import Path
from typing import BinaryIO

from conex.paths import assert_within, clone_or_copy, durable_replace


class BlobStore:
    """Content-addressed store at ``<root>/.conex/blobs/<aa>/<sha256-hex>``.

    Writes stage into ``<root>/.conex/tmp`` and are promoted via
    ``os.replace``; a digest that already exists is silently deduped
    (promotion skipped).  Blobs are immutable after promotion.
    """

    def __init__(self, root: Path) -> None:
        """Initialise the store for the given export root directory.

        The root must be the *export* root (the directory that contains the
        user's Confluence pages).  The ``.conex/`` subtree is created lazily
        on first write.
        """
        self._root = root
        self._blobs_dir = root / ".conex" / "blobs"
        self._tmp_dir = root / ".conex" / "tmp"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create the store directories if they do not yet exist."""
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, digest: str) -> Path:
        """Return the canonical on-disk path for *digest* (may not exist)."""
        return self._blobs_dir / digest[:2] / digest

    def _stage_path(self) -> Path:
        """Return a unique path under ``.conex/tmp/`` for a staging write."""
        self._ensure_dirs()
        return self._tmp_dir / f"blob-{uuid.uuid4().hex}"

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add_stream(self, fp: BinaryIO, max_bytes: int | None = None) -> tuple[str, int]:
        """Read *fp* to a staging file, compute the SHA-256 digest, and promote.

        Returns ``(digest, size)`` where *size* is the total byte count read.
        The stream is consumed fully; the caller is responsible for closing it.

        If *max_bytes* is given and the stream exceeds it, the partial staging
        file is removed and ``ValueError`` is raised.  This bounds an endpoint
        that streams without a ``Content-Length`` (or lies about it) so a buggy
        or hostile server cannot fill the disk while the export lock is held.

        Invariant: if the digest already exists the staging file is removed
        without being promoted, so the store never has duplicate entries.
        """
        self._ensure_dirs()
        stage = self._stage_path()
        h = hashlib.sha256()
        size = 0
        try:
            with stage.open("wb") as out:
                for chunk in iter(lambda: fp.read(65536), b""):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ValueError(
                            f"stream exceeds {max_bytes} byte cap (read {size}+)"
                        )
                    h.update(chunk)
                    out.write(chunk)
            digest = h.hexdigest()
            dest = self._blob_path(digest)
            if dest.exists():
                stage.unlink(missing_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                durable_replace(stage, dest)
        except Exception:
            stage.unlink(missing_ok=True)
            raise
        return digest, size

    def add_bytes(self, data: bytes) -> str:
        """Store *data* and return its SHA-256 hex digest.

        Deduplicates: if a blob with the same digest already exists, the
        staging write is skipped and the existing digest is returned.
        """
        digest, _ = self.add_stream(io.BytesIO(data))
        return digest

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def has(self, digest: str) -> bool:
        """Return *True* if a blob with *digest* exists in the store."""
        return self._blob_path(digest).exists()

    def path(self, digest: str) -> Path:
        """Return the on-disk :class:`~pathlib.Path` for *digest*.

        Raises :class:`KeyError` if the blob is not in the store.
        """
        p = self._blob_path(digest)
        if not p.exists():
            raise KeyError(digest)
        return p

    def read_bytes(self, digest: str) -> bytes:
        """Return the raw bytes stored under *digest*.

        Raises :class:`KeyError` if the blob is not in the store.
        """
        return self.path(digest).read_bytes()

    # ------------------------------------------------------------------
    # Materialize
    # ------------------------------------------------------------------

    def materialize(
        self,
        digest: str,
        dest: Path,
        mtime: float | None = None,
    ) -> None:
        """Copy the blob for *digest* to *dest* via ``.conex/tmp`` + ``os.replace``.

        *dest* is containment-checked against the export root via
        :func:`~conex.paths.assert_within` before any filesystem operation.
        The parent directory of *dest* must already exist.

        If *mtime* is given, ``os.utime`` is called on *dest* after promotion
        (sets both access time and modification time to *mtime*).

        Raises :class:`KeyError` if the blob is not in the store.
        Raises :class:`ValueError` if *dest* would escape the export root.
        """
        src = self.path(digest)  # raises KeyError if absent

        # Defence-in-depth containment: dest (a full path) must resolve inside
        # the export root, rejecting absolute/.. paths and symlinked ancestors.
        assert_within(self._root, dest)

        self._ensure_dirs()
        stage = self._stage_path()
        try:
            # Reflink the blob into the staging file where the filesystem allows
            # it (APFS/btrfs/XFS), so the materialized .media copy shares storage
            # with the blob (~1x on disk) instead of duplicating the bytes.
            clone_or_copy(src, stage)
            if mtime is not None:
                os.utime(stage, (mtime, mtime))
            else:
                # A CoW clone (clonefile) copies the blob's mtime; reset to "now"
                # so a stale blob's mtime never leaks onto the materialized file
                # (regression guard against the old copy2 age-gate bug).
                os.utime(stage, None)
            dest.parent.mkdir(parents=True, exist_ok=True)
            durable_replace(stage, dest)
        except Exception:
            stage.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Garbage collection
    # ------------------------------------------------------------------

    def gc(self, keep: set[str]) -> int:
        """Remove blobs whose digests are NOT in *keep*.

        Returns the count of blobs removed.  Blob directories (fan-out
        ``<aa>/``) that become empty are also removed.  Blobs in *keep*
        that do not exist in the store are silently ignored.

        Invariant: never removes a blob in *keep*, even if the caller
        passes an incomplete set.  An empty *keep* set is refused when the
        store holds blobs: that almost always means the caller's state failed
        to load (and was treated as absent), and wiping every blob would
        destroy the only crash-safe copy of the export's content.
        """
        if not self._blobs_dir.exists():
            return 0

        if not keep and any(
            f.is_file()
            for d in self._blobs_dir.iterdir() if d.is_dir()
            for f in d.iterdir()
        ):
            warnings.warn(
                "conex: refusing blob GC with an empty keep set while blobs "
                "exist (likely unreadable state) — keeping all blobs",
                stacklevel=2,
            )
            return 0

        removed = 0
        for fanout_dir in sorted(self._blobs_dir.iterdir()):
            if not fanout_dir.is_dir():
                continue
            for blob_file in sorted(fanout_dir.iterdir()):
                if blob_file.name not in keep:
                    blob_file.unlink(missing_ok=True)
                    removed += 1
            # Remove empty fan-out directory.
            try:
                fanout_dir.rmdir()
            except OSError:
                pass  # not empty — that is fine
        return removed
