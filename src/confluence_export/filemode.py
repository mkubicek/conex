"""File mode helpers for atomic replacements."""

from __future__ import annotations

import os
import threading
from pathlib import Path

_UMASK_LOCK = threading.Lock()


def default_file_mode() -> int:
    """Return the normal create-file mode after applying the process umask."""
    with _UMASK_LOCK:
        umask = os.umask(0)
        os.umask(umask)
    return 0o666 & ~umask


def replacement_mode(path: Path, *, default_mode: int | None = None) -> int:
    """Mode to apply before atomically replacing ``path``."""
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        return default_file_mode() if default_mode is None else default_mode
