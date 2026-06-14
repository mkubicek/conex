"""Tests for conex.store.lock.ExportLock.

Coverage targets (per SPEC-V2.md):
- Successful acquire via context manager
- Lock file and its parent directory are created on acquire
- LockHeldError is raised on contention (second fd, same path)
- LockHeldError message names the lock path
- Lock is released on normal exit from the context manager
- Lock is released when the context manager exits via exception
- Lock contention via a subprocess holding the lock
- Nested acquire on the same path raises LockHeldError (non-reentrant)
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conex.errors import LockHeldError
from conex.store.lock import ExportLock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def export_root(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# Normal acquire / release
# ---------------------------------------------------------------------------


class TestNormalAcquire:
    def test_context_manager_succeeds(self, export_root: Path) -> None:
        with ExportLock(export_root) as lock:
            assert lock is not None

    def test_lock_file_and_dir_created(self, export_root: Path) -> None:
        lock_path = export_root / ".conex" / "lock"
        assert not lock_path.exists()
        with ExportLock(export_root):
            assert lock_path.exists()

    def test_lock_released_after_context(self, export_root: Path) -> None:
        with ExportLock(export_root):
            pass
        # After exit, a second lock on the same path must succeed.
        with ExportLock(export_root):
            pass

    def test_lock_released_on_exception(self, export_root: Path) -> None:
        try:
            with ExportLock(export_root):
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        # The lock must be released even though an exception escaped.
        with ExportLock(export_root):
            pass


# ---------------------------------------------------------------------------
# Contention via a second file descriptor
# ---------------------------------------------------------------------------


class TestContention:
    def test_second_lock_raises_lock_held_error(self, export_root: Path) -> None:
        with ExportLock(export_root):
            with pytest.raises(LockHeldError):
                with ExportLock(export_root):
                    pass

    def test_error_message_names_lock_path(self, export_root: Path) -> None:
        lock_path = str(export_root / ".conex" / "lock")
        with ExportLock(export_root):
            try:
                with ExportLock(export_root):
                    pass
            except LockHeldError as exc:
                assert lock_path in str(exc), (
                    f"LockHeldError message must name the lock path.\n"
                    f"Expected {lock_path!r} in {str(exc)!r}"
                )
            else:
                pytest.fail("expected LockHeldError was not raised")

    def test_contention_via_raw_fd(self, export_root: Path) -> None:
        """Use a raw fd to hold the lock instead of ExportLock itself."""
        lock_path = export_root / ".conex" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with pytest.raises(LockHeldError):
                with ExportLock(export_root):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ---------------------------------------------------------------------------
# Subprocess contention
# ---------------------------------------------------------------------------

# Python snippet run in a child subprocess to hold the ExportLock.
# It writes "1\n" to stdout when the lock is held, then waits for a line
# on stdin before releasing.  Using subprocess.Popen + os.environ ensures
# PYTHONPATH (set by the test runner) is inherited correctly on all platforms.
_LOCK_HOLDER_SCRIPT = """
import sys
from pathlib import Path
root = Path(sys.argv[1])
import conex.store.lock as _m
with _m.ExportLock(root):
    sys.stdout.write("1\\n")
    sys.stdout.flush()
    sys.stdin.readline()   # wait for release signal
"""


def _start_lock_holder(export_root: Path) -> subprocess.Popen:  # type: ignore[type-arg]
    """Launch a child process that holds the lock and returns when signalled."""
    return subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(export_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=os.environ,
    )


class TestSubprocessContention:
    def test_subprocess_holds_lock(self, export_root: Path) -> None:
        """A child process holds the lock; the parent must get LockHeldError."""
        proc = _start_lock_holder(export_root)
        try:
            assert proc.stdout is not None
            proc.stdout.readline()  # block until lock is held

            with pytest.raises(LockHeldError):
                with ExportLock(export_root):
                    pass
        finally:
            assert proc.stdin is not None
            proc.stdin.write(b"\n")
            proc.stdin.flush()
            proc.wait(timeout=5)

        assert proc.returncode == 0, "lock-holding child process failed"

    def test_lock_available_after_subprocess_exits(self, export_root: Path) -> None:
        """After the child releases the lock, the parent can re-acquire it."""
        proc = _start_lock_holder(export_root)
        assert proc.stdout is not None
        proc.stdout.readline()  # wait for lock held

        assert proc.stdin is not None
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        proc.wait(timeout=5)

        # Child has released the lock; parent can now acquire it.
        with ExportLock(export_root):
            pass  # should not raise


# ---------------------------------------------------------------------------
# Error message quality
# ---------------------------------------------------------------------------


class TestErrorMessage:
    def test_remedy_mentioned_in_message(self, export_root: Path) -> None:
        """The error message should contain a human-actionable remedy hint."""
        with ExportLock(export_root):
            try:
                with ExportLock(export_root):
                    pass
            except LockHeldError as exc:
                msg = str(exc).lower()
                # Must contain either "wait" or "remove" as a remedy hint.
                assert "wait" in msg or "remove" in msg, (
                    f"LockHeldError should suggest a remedy; got: {str(exc)!r}"
                )


# ---------------------------------------------------------------------------
# Symlink guard on .conex (H2)
# ---------------------------------------------------------------------------


class TestSymlinkGuard:
    def test_refuses_symlinked_conex_dir(self, tmp_path: Path) -> None:
        """A planted `.conex -> elsewhere` symlink must not be locked through —
        it would create the lock on a different inode (not protecting the real
        root) and could redirect state writes off the export tree."""
        from conex.errors import StateError

        root = tmp_path / "export"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (root / ".conex").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(StateError, match="symlink"):
            with ExportLock(root):
                pass
