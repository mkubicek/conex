"""Filesystem-safety helpers for untrusted API-controlled names.

Confluence attachment titles are untrusted input: a title like
``../../../etc/cron.d/evil`` or an absolute path must never be used verbatim as
a filesystem path. This module owns the conversion from a display string to a
safe single path component (:func:`safe_component` / :func:`safe_attachment_name`)
and the defence-in-depth containment assert (:func:`resolve_within`).

Harvested from the ``rewrite/robust-core-wt`` experiment, kept deliberately
minimal: it only covers what the attachment-write path needs.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Maximum length of a single path segment (directory or file stem). Matches
# converter.MAX_FILENAME_LEN, kept independent here so this module has no
# dependency on the HTML→markdown converter.
MAX_FILENAME_LEN = 100


def nfc(s: str) -> str:
    """Normalize to Unicode NFC — the form git stores on macOS with
    core.precomposeunicode, and the canonical form a normalizing filesystem
    (APFS) folds equivalent byte strings to."""
    return unicodedata.normalize("NFC", s)


def nfc_casefold(s: str) -> str:
    """NFC + casefold: the identity a case-insensitive, normalizing filesystem
    and git fold together. The shared fold for collision keys (layout) and
    stale-file comparison (git)."""
    return nfc(s).casefold()


def safe_component(
    value: object,
    *,
    fallback: str = "attachment",
    max_len: int = MAX_FILENAME_LEN,
) -> str:
    """Return one filesystem-safe path component for untrusted text.

    Path separators and control characters are stripped, traversal/absolute
    tokens are neutralized, and a leading ``.``/``-`` is prefixed with ``_`` so
    the result can never be a dotfile or look like a command-line option.
    Filesystem-legal punctuation common in attachment names (spaces, parens,
    dots, hyphens) is preserved so benign names round-trip unchanged.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"[^\w\s().-]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"-{2,}", "-", text).strip(" -")
    if not text or text in {".", ".."}:
        return fallback
    if text.startswith((".", "-")):
        text = f"_{text.lstrip('.')}"
    if len(text) > max_len:
        text = _truncate_component(text, max_len)
    return text or fallback


def _truncate_component(name: str, max_len: int) -> str:
    """Truncate ``name`` to ``max_len`` while preserving its extension."""
    suffix = "".join(Path(name).suffixes)
    if suffix and len(suffix) < max_len // 2:
        stem_len = max_len - len(suffix)
        return (name[: -len(suffix)][:stem_len].rstrip("-._") + suffix) or name[:max_len]
    return name[:max_len].rstrip("-._") or name[:max_len]


def is_safe_component(name: str) -> bool:
    """True if ``name`` is a single, in-place path component (no traversal)."""
    name = str(name)
    return bool(name) and (
        name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
        and Path(name).name == name
    )


def safe_attachment_name(title: object) -> str:
    """The on-disk filename for an attachment titled ``title``.

    Keeps the raw title when it is already a safe single component, so existing
    markdown links and ``.versions.json`` manifests (both keyed on the raw
    filename) keep resolving. Only titles that would escape ``.media/`` — path
    separators, ``..``, absolute paths, control characters — are sanitized.
    """
    name = str(title or "")
    if is_safe_component(name):
        return name
    return safe_component(name)


def resolve_within(base: Path, component: str) -> Path:
    """Resolve ``base / component`` and fail if it would escape ``base``.

    Rejects any component carrying a path separator, a ``.``/``..`` token, or a
    name that does not round-trip through :class:`Path` — then asserts the
    resolved path is still inside ``base``. The single write-site choke point.
    """
    component = str(component)
    if not is_safe_component(component):
        raise ValueError(f"unsafe path component: {component!r}")
    root = base.resolve()
    candidate = (base / component).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path component escapes base directory: {component!r}") from exc
    return candidate
