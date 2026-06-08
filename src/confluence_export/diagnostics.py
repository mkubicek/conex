"""End-of-run export diagnostics.

Best-effort export warnings (an attachment whose binary is gone, a draw.io render
that produced nothing, a page that failed to convert) are printed inline to stderr
as they happen, which is hard to skim after a bulk run. ``WarningCollector`` records
them by category so the export command can print one grouped summary at the end —
no log scraping to answer "did anything degrade, and how much?". Thread-safe because
attachment downloads record from a worker pool.
"""

from __future__ import annotations

import threading


class WarningCollector:
    """Thread-safe tally of best-effort warnings, keyed by a short category string."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def record(self, category: str) -> None:
        with self._lock:
            self._counts[category] = self._counts.get(category, 0) + 1

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    @property
    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())


def format_warning_summary(counts: dict[str, int]) -> str:
    """One-line grouped summary, most-frequent first, e.g.
    ``14 warning(s): attachment unavailable (HTTP 404) ×12, empty draw.io diagram ×2``.
    Empty when there were no warnings."""
    if not counts:
        return ""
    total = sum(counts.values())
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    body = ", ".join(f"{cat} ×{n}" for cat, n in items)
    return f"{total} warning(s): {body}"
