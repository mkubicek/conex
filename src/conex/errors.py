"""Exception hierarchy for conex v2.

All exceptions are subclasses of ConexError; the CLI prints str(e) and exits
with code 1 on any ConexError.
"""

from __future__ import annotations


class ConexError(Exception):
    """Base exception for all conex errors."""


class ConfigError(ConexError):
    """Raised for missing or invalid configuration."""


class AuthError(ConexError):
    """Raised when Confluence returns 401 or 403."""


class ApiError(ConexError):
    """Raised for unexpected HTTP responses from the Confluence API.

    Invariant: status is the HTTP status code if available, else None.
    """

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class LockHeldError(ConexError):
    """Raised when the exclusive run-lock is already held by another process.

    The message names the lock path and the remedy.
    """


class GitError(ConexError):
    """Raised when a git subprocess call fails."""
