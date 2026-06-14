"""State and snapshot stores for conex v2.

Contracts:
- AttachmentState, PageState, ExportState, Snapshot inherit NullTolerantModel:
  explicit null on any field is silently replaced by the field default.
  No field is required; every model is constructible from an empty dict.
- StateStore / SnapshotStore each wrap a single JSON file under .conex/.
  load() returns None on a missing file AND on ANY ValidationError or
  JSONDecodeError, writing a warning to stderr.
  save() is atomic: it writes to .conex/tmp/<name>.json.tmp then promotes
  via os.replace — a crash at any point leaves the previous file intact.
- Stores create .conex/ (and .conex/tmp/) if absent; they NEVER clear
  .conex/tmp (the CLI owns that, per I4).
- Snapshot.space defaults to Space() — the field carries a model default,
  not a required argument.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

from conex.models import Attachment, Folder, NullTolerantModel, Page, Space
from conex.paths import durable_replace


# ---------------------------------------------------------------------------
# Store models
# ---------------------------------------------------------------------------


class AttachmentState(NullTolerantModel):
    """On-disk record for a single attachment owned by a page.

    Invariants:
    - blob == "" means the download failed; the attachment is known but
      not materialized.
    - file is the filename within the page's .media/ directory (not a
      full path).
    """

    version: int = 0
    file: str = ""
    blob: str = ""
    size: int = 0


class PageState(NullTolerantModel):
    """On-disk record for a single page in the export tree.

    Invariants:
    - dir and file are POSIX relpaths from the export root.
    - html == "" when --include-html was not active for this page.
    - fingerprint is the sha256 hex over build inputs (see build.py);
      "" means the page predates fingerprinting and will be rewritten.
    - attachments keys are attachment IDs (str).
    """

    dir: str = ""
    file: str = ""
    html: str = ""
    title: str = ""
    version: int = 0
    status: str = "current"
    fingerprint: str = ""
    attachments: dict[str, AttachmentState] = {}
    # Derived media filenames written into the page's .media/ that are NOT
    # attachments (currently batch-rendered drawio PNGs).  Recorded so they are
    # part of the page's owned-path set and the reconciliation deletes the old
    # copy when the page moves or is pruned.
    rendered_media: list[str] = []


class ExportState(NullTolerantModel):
    """The .conex/state.json content: what conex owns on disk.

    Invariants:
    - schema_version == 1 for this release; a higher value on load
      triggers None + warning (future-proofing handled by ValidationError
      path, though a dedicated check may be added in a later wave).
    - pages keys are page IDs (str).
    - folders maps folder_id -> dir relpath; used only for prune.
    - updated_at is an ISO-8601 string set by build.py, not by the store.
    """

    schema_version: int = 1
    space_key: str = ""
    space_id: str = ""
    updated_at: str = ""
    converter_version: int = 0
    pages: dict[str, PageState] = {}
    folders: dict[str, str] = {}


class Snapshot(NullTolerantModel):
    """The .conex/snapshot.json content: a point-in-time fetch from Confluence.

    Invariants:
    - body_storage on every Page in .pages is ALWAYS "" here; actual
      bodies are stored as blobs keyed by page_id in body_blobs.
    - attachment_blobs keys are "{att_id}@{version}".
    - derived_blobs keys are
      "drawio-png:v{DRAWIO_RENDER_VERSION}:{xml_digest}".
    - attachments_complete == False means a fetch failed; build must not
      delete existing media files for that run.
    - include_archived records what was ACTUALLY fetched (False when the
      API dialect cannot return archived pages), not what was requested.
    - warnings records human-readable pull-stage failures (a body, attachment
      listing, or download that failed) so the CLI summary/recap can surface
      partial-fetch loss that would otherwise only appear inline on stderr.
    - space defaults to Space() so that a completely empty dict round-trips
      without error.
    """

    schema_version: int = 1
    space: Space = Space()
    fetched_at: str = ""
    include_archived: bool = False
    attachments_complete: bool = True
    pages: list[Page] = []
    folders: list[Folder] = []
    body_blobs: dict[str, str] = {}
    attachments: dict[str, list[Attachment]] = {}
    attachment_blobs: dict[str, str] = {}
    derived_blobs: dict[str, str] = {}
    users: dict[str, str] = {}
    warnings: list[str] = []


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def _ensure_dirs(path: Path) -> None:
    """Create the parent directory (and .conex/tmp) if they do not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = path.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)


def _atomic_save(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically via .conex/tmp.

    The temporary file lives under path.parent/tmp/ so that os.replace
    is guaranteed to cross no filesystem boundary (invariant I4).
    """
    _ensure_dirs(path)
    tmp_dir = path.parent / "tmp"
    tmp_path = tmp_dir / (path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp_path.write_text(payload, encoding="utf-8")
    # Crash-durable: fsync the temp file and the parent dir around os.replace
    # so the I6 "a crash leaves the previous file intact" invariant holds even
    # across power loss, not just against concurrent observers.
    durable_replace(tmp_path, path)


def _load_json(path: Path) -> dict | None:
    """Load JSON from *path*; return None (with stderr warning) on any error.

    Returns None for:
    - file not found (no warning — expected cold-start case)
    - any JSONDecodeError (corrupt file)
    - any other OSError
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"conex: corrupt JSON in {path}: {exc} — treating as absent",
            stacklevel=4,
        )
        return None
    except OSError as exc:
        warnings.warn(
            f"conex: cannot read {path}: {exc} — treating as absent",
            stacklevel=4,
        )
        return None


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class StateStore:
    """Persistent store for ExportState at <root>/.conex/state.json.

    Invariants:
    - load() returns None on a missing file OR on ANY ValidationError /
      JSONDecodeError; it always warns to stderr in the error cases.
    - save() is atomic (I6): write to .conex/tmp/state.json.tmp, then
      os.replace to .conex/state.json.
    - The store creates .conex/ and .conex/tmp/ if absent; it NEVER
      clears .conex/tmp (I4).
    """

    def __init__(self, root: Path) -> None:
        self._path = root / ".conex" / "state.json"

    def load(self) -> ExportState | None:
        """Return the persisted ExportState, or None on missing/corrupt data."""
        raw = _load_json(self._path)
        if raw is None:
            return None
        try:
            return ExportState.model_validate(raw)
        # Intentionally broad: ANY load error is treated as "absent", which
        # triggers a full re-export (the data-safe direction).  ValidationError
        # is a subclass of Exception, so the single clause covers it.
        except Exception as exc:
            print(
                f"conex: invalid state in {self._path}: {exc} — treating as absent",
                file=sys.stderr,
            )
            return None

    def save(self, state: ExportState) -> None:
        """Atomically persist *state* to disk."""
        _atomic_save(self._path, state.model_dump())


class SnapshotStore:
    """Persistent store for Snapshot at <root>/.conex/snapshot.json.

    Same load/save contract as StateStore.
    """

    def __init__(self, root: Path) -> None:
        self._path = root / ".conex" / "snapshot.json"

    def load(self) -> Snapshot | None:
        """Return the persisted Snapshot, or None on missing/corrupt data."""
        raw = _load_json(self._path)
        if raw is None:
            return None
        try:
            return Snapshot.model_validate(raw)
        # Intentionally broad: ANY load error is treated as "absent" (data-safe
        # re-export).  ValidationError is a subclass of Exception.
        except Exception as exc:
            print(
                f"conex: invalid snapshot in {self._path}: {exc} — treating as absent",
                file=sys.stderr,
            )
            return None

    def save(self, snapshot: Snapshot) -> None:
        """Atomically persist *snapshot* to disk."""
        _atomic_save(self._path, snapshot.model_dump())
