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
from dataclasses import dataclass
from pathlib import Path

# Maximum length of a single path segment (directory or file stem). Matches
# converter.MAX_FILENAME_LEN, kept independent here so this module has no
# dependency on the HTML→markdown converter.
MAX_FILENAME_LEN = 100
RESERVED_ATTACHMENT_NAMES = {".versions.json"}


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


def _with_suffix_token(
    name: str,
    token: str,
    max_len: int = MAX_FILENAME_LEN,
    retry: int = 0,
) -> str:
    """Append a collision token before the extension without exceeding max_len."""
    suffix = "".join(Path(name).suffixes)
    stem = name[: -len(suffix)] if suffix else name
    suffix_extra = f"-{retry}" if retry else ""
    token_len = max(1, 16 - len(suffix_extra))
    token_part = safe_component(token, fallback="2", max_len=token_len)
    marker = f"-{token_part}{suffix_extra}"
    stem_len = max_len - len(suffix) - len(marker)
    if stem_len <= 0:
        stem_len = max_len - len(marker)
        suffix = ""
    stem = stem[:stem_len].rstrip("-._") or "attachment"
    return f"{stem}{marker}{suffix}"


def is_safe_component(name: str) -> bool:
    """True if ``name`` is a single, in-place path component (no traversal)."""
    name = str(name)
    return bool(name) and (
        name not in {".", ".."}
        and not name.startswith((".", "-"))
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
    if is_safe_component(name) and nfc_casefold(name) not in RESERVED_ATTACHMENT_NAMES:
        return name
    return safe_component(name)


def attachment_identity(attachment: object) -> str:
    """Stable best-effort identity for attachments without an API id."""
    download_link = str(getattr(attachment, "download_link", "") or "")
    download_link = download_link.split("?", 1)[0].split("#", 1)[0]
    return "\x1f".join([
        download_link,
        str(getattr(attachment, "page_id", "") or ""),
        str(getattr(attachment, "media_type", "") or ""),
        str(getattr(attachment, "created_at", "") or ""),
        str(getattr(attachment, "webui", "") or ""),
    ])


def drawio_render_name(source_name: str, token: str, reserved_names: set[str]) -> str:
    """Allocate a safe rendered-PNG filename for a draw.io source attachment."""
    source = safe_attachment_name(source_name)
    default_name = Path(source).with_suffix(".drawio.png").name
    candidate = default_name
    reserved = {nfc_casefold(name) for name in reserved_names}
    retry = 0
    while nfc_casefold(candidate) in reserved:
        candidate = _with_suffix_token(default_name, f"{token}-render", retry=retry)
        retry += 1
    return candidate


@dataclass(frozen=True)
class AttachmentNamePlan:
    """Stable per-page mapping from Confluence attachments to local filenames."""

    by_id: dict[str, str]
    by_object: dict[int, str]
    by_title: dict[str, str]
    by_folded_title: dict[str, str]

    def for_attachment(self, attachment: object) -> str:
        att_id = str(getattr(attachment, "id", "") or "")
        title = str(getattr(attachment, "title", "") or "")
        if att_id and att_id in self.by_id:
            return self.by_id[att_id]
        object_key = id(attachment)
        if object_key in self.by_object:
            return self.by_object[object_key]
        return self.by_title.get(title, safe_attachment_name(title))

    def for_reference(self, title: str, attachment_id: str | None = None) -> str:
        if attachment_id and attachment_id in self.by_id:
            return self.by_id[attachment_id]
        if title in self.by_title:
            return self.by_title[title]
        folded = nfc_casefold(title)
        if folded in self.by_folded_title:
            return self.by_folded_title[folded]
        return safe_attachment_name(title)


def plan_attachment_names(attachments: list[object]) -> AttachmentNamePlan:
    """Allocate unique safe local filenames for one page's attachments."""
    by_id: dict[str, str] = {}
    by_object: dict[int, str] = {}
    by_title: dict[str, str] = {}
    by_folded_title: dict[str, str] = {}
    groups: dict[str, list[tuple[tuple[object, ...], str, str, str, str, int]]] = {}
    seen_ids: set[str] = set()

    for index, att in enumerate(attachments):
        att_id = str(getattr(att, "id", "") or "")
        if att_id and att_id in seen_ids:
            continue
        if att_id:
            seen_ids.add(att_id)
        title = str(getattr(att, "title", "") or "")
        created_at = str(getattr(att, "created_at", "") or "")
        identity = attachment_identity(att)
        base = safe_attachment_name(title)
        object_key = id(att)
        owner = att_id or f"{title}\0{identity}\0{index}"
        sort_key = (created_at, nfc_casefold(title), att_id, identity, index)
        groups.setdefault(nfc_casefold(base), []).append(
            (sort_key, owner, att_id, title, base, object_key)
        )

    taken: dict[str, str] = {}
    assignments: dict[str, tuple[str, str, str, int]] = {}

    for base_key, entries in groups.items():
        ordered = sorted(entries, key=lambda entry: entry[0])
        _, owner, att_id, title, base, object_key = ordered[0]
        taken[base_key] = owner
        assignments[owner] = (att_id, title, base, object_key)

    reserved_base_keys = set(groups)
    remaining = [
        (base_key, entry)
        for base_key, entries in groups.items()
        for entry in sorted(entries, key=lambda item: item[0])[1:]
    ]

    for _base_key, entry in sorted(remaining, key=lambda item: (item[0], item[1][0])):
        _sort_key, owner, att_id, title, base, object_key = entry
        token = att_id or title or "attachment"
        retry = 0
        while True:
            candidate = _with_suffix_token(base, token, retry=retry)
            collision_key = nfc_casefold(candidate)
            if collision_key not in taken and collision_key not in reserved_base_keys:
                break
            retry += 1

        taken[nfc_casefold(candidate)] = owner
        assignments[owner] = (att_id, title, candidate, object_key)

    for att_id, title, candidate, object_key in assignments.values():
        if att_id:
            by_id[att_id] = candidate
        else:
            by_object[object_key] = candidate
        by_title.setdefault(title, candidate)

    folded_titles: dict[str, list[str]] = {}
    for _att_id, title, _candidate, _object_key in assignments.values():
        folded_titles.setdefault(nfc_casefold(title), []).append(title)
    for folded, titles in folded_titles.items():
        if len(titles) == 1:
            by_folded_title[folded] = by_title[titles[0]]

    return AttachmentNamePlan(
        by_id=by_id,
        by_object=by_object,
        by_title=by_title,
        by_folded_title=by_folded_title,
    )


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
    leaf = base / component
    if leaf.is_symlink():
        raise ValueError(f"refusing to use symlinked path component: {component!r}")
    candidate = leaf.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # pragma: no cover
        raise ValueError(f"path component escapes base directory: {component!r}") from exc
    return candidate
