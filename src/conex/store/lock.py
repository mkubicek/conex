"""Exclusive advisory file-lock for the export root.

POSIX-only: uses ``fcntl.flock`` (Linux, macOS).  Windows is not supported.

Invariant (I5): every state-mutating command holds an exclusive flock on
``<root>/.conex/lock`` for its entire duration.  A second runner fails
immediately with a clear message — never waits.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Type

from conex.errors import LockHeldError, StateError


class ExportLock:
    """Exclusive advisory flock on ``<root>/.conex/lock`` for the whole run.

    Context manager.  Non-blocking acquire; on contention raises
    ``LockHeldError('another conex run holds <path>; wait or remove if stale')``.

    The lock file and its parent directory are created if they do not exist.
    The file descriptor is kept open for the lifetime of the context so the
    kernel keeps the lock held.

    Invariant: only one ``ExportLock`` at a time per export root; nesting is
    not permitted and will raise immediately.
    """

    def __init__(self, root: Path) -> None:
        self._lock_path: Path = root / ".conex" / "lock"
        self._fd: int | None = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "ExportLock":
        # Refuse to lock through a symlinked .conex: opening the lock file via a
        # planted symlink would create the lock on a different inode, so it would
        # not protect the intended export root (and would follow the symlink off
        # the root).  Abort before creating or opening anything.
        conex_dir = self._lock_path.parent
        if conex_dir.is_symlink():
            raise StateError(
                f"{conex_dir} is a symlink; refusing to lock through it "
                "(it would redirect conex's state outside the export root)"
            )
        conex_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockHeldError(
                f"another conex run holds {self._lock_path}; "
                "wait for it to finish (the lock releases automatically when "
                "that run exits — do not delete the lock file)"
            ) from None
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
