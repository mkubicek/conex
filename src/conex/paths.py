"""Filesystem-safety helpers for untrusted API-controlled names.

Confluence attachment titles and page titles are untrusted input: a title like
``../../../etc/cron.d/evil`` or an absolute path must never be used verbatim as
a filesystem path.  This module owns:

- :func:`sanitize_filename` — page title -> directory/file segment (word chars,
  spaces, hyphens; 100-char cap).  Used ONLY for page dirs and markdown stems.
- :func:`safe_attachment_name` / :func:`safe_component` — neutralize path
  separators, control characters, leading dots, traversal tokens.  Used ONLY for
  attachment files.
- :func:`plan_attachment_names` — per-page collision-safe :class:`AttachmentNamePlan`.
- :func:`resolve_within` — single-component containment assert (S1 posture).
- :func:`assert_within` — full-path containment assert; the choke point used
  before destructive filesystem operations (deletes).
- :func:`durable_replace` / :func:`fsync_file` / :func:`fsync_dir` —
  crash-durable atomic writes (fsync around ``os.replace``).
- :func:`nfc` / :func:`nfc_casefold` — canonical Unicode folds.
- :func:`truncate_with_suffix` — append a suffix without exceeding MAX_FILENAME_LEN.

Invariant: page dirs and .md file stems ALWAYS go through :func:`sanitize_filename`;
attachment files ALWAYS go through :func:`safe_attachment_name` /
:func:`plan_attachment_names`.  The two sanitizers are NOT interchangeable.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAX_FILENAME_LEN = 100


# ---------------------------------------------------------------------------
# Unicode normalization helpers
# ---------------------------------------------------------------------------

def nfc(s: str) -> str:
    """Normalize to Unicode NFC — the form git stores on macOS with
    core.precomposeunicode, and the canonical form a normalizing filesystem
    (APFS) folds equivalent byte strings to."""
    return unicodedata.normalize("NFC", s)


def nfc_casefold(s: str) -> str:
    """NFC + casefold: the identity a case-insensitive, normalizing filesystem
    and git fold together.  The shared collision key for layout segments and
    stale-file comparisons."""
    return nfc(s).casefold()


# ---------------------------------------------------------------------------
# sanitize_filename — page dirs / .md stems only
# ---------------------------------------------------------------------------

def sanitize_filename(title: str) -> str:
    """Convert a page title to a safe directory / file stem.

    Keeps word characters (\\w), spaces, and hyphens; collapses runs of
    separators to a single hyphen; strips leading/trailing hyphens; caps at
    MAX_FILENAME_LEN.  Returns ``"untitled"`` for titles that reduce to empty.

    This is the ONLY sanitizer for page-directory segments and markdown stems;
    never use it for attachment filenames.
    """
    name = re.sub(r"[^\w\s-]", "", title)
    name = re.sub(r"[-\s]+", "-", name)
    name = name.strip("-")
    if len(name) > MAX_FILENAME_LEN:
        name = name[:MAX_FILENAME_LEN].rstrip("-")
    return name or "untitled"


# ---------------------------------------------------------------------------
# truncate_with_suffix — layout collision suffixes
# ---------------------------------------------------------------------------

def truncate_with_suffix(segment: str, suffix: str) -> str:
    """Append ``suffix`` to ``segment`` without exceeding MAX_FILENAME_LEN.

    The base is truncated to ``MAX_FILENAME_LEN - len(suffix)`` chars first;
    trailing hyphens are stripped from the truncated base so the result is a
    valid sanitize_filename output.  Falls back to ``"untitled"`` if the
    truncated base is empty.
    """
    avail = MAX_FILENAME_LEN - len(suffix)
    truncated = segment[:avail].rstrip("-")
    if not truncated:
        truncated = "untitled"
    return f"{truncated}{suffix}"


# ---------------------------------------------------------------------------
# safe_component / safe_attachment_name — attachment files only
# ---------------------------------------------------------------------------

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

    Use this for attachment filenames only; page dirs use :func:`sanitize_filename`.
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
    """True iff ``name`` is a single, in-place path component with no traversal.

    A safe component has no path separators, is not ``.`` or ``..``, does not
    start with ``.`` or ``-``, contains no ASCII control characters, and
    round-trips through :class:`Path`.
    """
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

    Keeps the raw title when it is already a safe single component so existing
    markdown links keep resolving.  Only titles that would escape ``.media/``
    (path separators, ``..``, absolute paths, control characters) are
    sanitized via :func:`safe_component`.

    Use ONLY for attachment files; page dirs use :func:`sanitize_filename`.
    """
    name = str(title or "")
    if is_safe_component(name):
        return name
    return safe_component(name)


# ---------------------------------------------------------------------------
# resolve_within — defence-in-depth containment assert (S1)
# ---------------------------------------------------------------------------

def resolve_within(base: Path, component: str) -> Path:
    """Resolve ``base / component`` and assert it remains inside ``base``.

    Rejects any component carrying a path separator, a ``.``/``..`` token, or a
    name that does not round-trip through :class:`Path` — then asserts the
    resolved path is still inside ``base``.  Refuses to follow a symlinked
    component.  For full multi-component paths use :func:`assert_within`.
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


# ---------------------------------------------------------------------------
# assert_within — containment assert for full (multi-component) paths
# ---------------------------------------------------------------------------

def assert_within(root: Path, path: Path) -> Path:
    """Resolve *path* and assert it stays inside *root*; return the resolved path.

    Unlike :func:`resolve_within` (which takes a single safe component), *path*
    may be a full multi-component path.  This is the containment choke point for
    destructive operations: a target that resolves outside *root* — via an
    absolute value, a ``..`` token, or a symlinked ancestor — raises
    ``ValueError`` and is never operated on.  A symlinked *path* whose target
    escapes the root is likewise rejected, because ``resolve`` follows it.
    """
    root_real = root.resolve()
    candidate = path.resolve()
    try:
        candidate.relative_to(root_real)
    except ValueError as exc:
        raise ValueError(f"path {str(path)!r} escapes root {str(root)!r}") from exc
    return candidate


# ---------------------------------------------------------------------------
# Crash-durable atomic write helpers
# ---------------------------------------------------------------------------

def fsync_file(path: Path) -> None:
    """Flush *path*'s contents to stable storage."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Flush *path* directory entry to stable storage (best effort).

    A successful ``os.replace`` is only crash-durable once the containing
    directory's metadata is fsynced.  Some platforms reject fsync on a
    directory fd; those failures are swallowed.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def durable_replace(tmp: Path, dest: Path) -> None:
    """``os.replace`` *tmp* onto *dest*, crash-durably.

    fsyncs *tmp* before the rename and *dest*'s parent directory after it.
    Plain ``os.replace`` guarantees atomicity only for concurrent observers,
    not that the bytes survive power loss; the fsyncs close that gap so the
    documented "a crash leaves a complete file" invariant actually holds.
    """
    fsync_file(tmp)
    os.replace(tmp, dest)
    fsync_dir(dest.parent)


# ---------------------------------------------------------------------------
# Noise (editor-cruft) attachment titles
# ---------------------------------------------------------------------------

def is_noise_attachment_title(title: str) -> bool:
    """True for well-known editor cruft uploaded as Confluence attachments.

    These should not be downloaded, materialized, or surfaced as user-facing
    links/frontmatter — they are transient artifacts, not content:

    - ``~$report.xlsx`` etc. — Microsoft Office lock/owner files.
    - ``~name.drawio.tmp`` — draw.io desktop autosave/temp files.

    Deliberately conservative (specific prefixes/suffixes) so a genuine
    attachment is never dropped.
    """
    t = title.strip()
    if t.startswith("~$"):
        return True
    if t.startswith("~") and t.endswith(".tmp"):
        return True
    return False


# ---------------------------------------------------------------------------
# Copy-on-write clone (reflink) with copy fallback
# ---------------------------------------------------------------------------

def clone_or_copy(src: Path, dst: Path) -> None:
    """Copy *src* to *dst*, preferring a copy-on-write clone (reflink).

    A reflink makes *dst* share *src*'s storage blocks until one of them is
    modified, so the two files cost ~1x on disk instead of 2x.  This lets the
    content-addressed blob and its materialized ``.media`` copy coexist without
    duplicating the bytes.  Used where the filesystem supports it:

    - macOS / APFS via ``clonefile``
    - Linux btrfs / XFS / ZFS via the ``FICLONE`` ioctl

    Falls back to a full byte copy on any platform or filesystem that can't
    clone (ext4, NTFS, a cross-device pair).  *dst* must not already exist
    (the clone primitives refuse an existing target).
    """
    if _reflink(src, dst):
        return
    shutil.copyfile(src, dst)


def _reflink(src: Path, dst: Path) -> bool:
    """Attempt a CoW clone of *src* to *dst*; return True on success."""
    if sys.platform == "darwin":
        return _clonefile_darwin(src, dst)
    if sys.platform.startswith("linux"):
        return _ficlone_linux(src, dst)
    return False


def _clonefile_darwin(src: Path, dst: Path) -> bool:
    """APFS clonefile(2). Returns False (caller falls back to copy) on any error."""
    import ctypes
    import ctypes.util

    libname = ctypes.util.find_library("System")
    if not libname:
        return False
    try:
        libc = ctypes.CDLL(libname, use_errno=True)
        clonefile = libc.clonefile
    except (OSError, AttributeError):
        return False
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    rc = clonefile(os.fsencode(str(src)), os.fsencode(str(dst)), 0)
    return rc == 0


def _ficlone_linux(src: Path, dst: Path) -> bool:
    """Linux FICLONE ioctl. Returns False (caller falls back to copy) on any error."""
    import fcntl  # lazy: keep this module importable on platforms without fcntl

    _FICLONE = 0x40049409  # _IOW(0x94, 9, int)
    try:
        with open(src, "rb") as s, open(dst, "wb") as d:
            fcntl.ioctl(d.fileno(), _FICLONE, s.fileno())
        return True
    except OSError:
        # A failed clone may leave an empty dst; remove it so the copy fallback
        # starts from a clean (non-existent) target.
        dst.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# AttachmentNamePlan — per-page collision-safe name allocation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttachmentNamePlan:
    """Stable per-page mapping from Confluence attachment ids/titles to local filenames.

    All four lookup dicts are populated once at construction time by
    :func:`plan_attachment_names` and are never mutated.  The ``for_reference``
    method implements the resolver used by the converter: id lookup first,
    then exact title, then NFC-casefold title, then a fresh sanitization of the
    raw title (safe even for titles not in the plan).
    """

    by_id: dict[str, str]
    by_title: dict[str, str]
    by_folded_title: dict[str, str]

    def for_reference(self, title: str, attachment_id: str | None = None) -> str:
        """Resolve ``title`` (and optional ``attachment_id``) to a local filename.

        Resolution order: attachment id → exact title → NFC-casefold title →
        fresh sanitization of the raw title.  The last fallback is safe even for
        titles not present in the plan (unknown/late attachments in storage XML).
        """
        if attachment_id and attachment_id in self.by_id:
            return self.by_id[attachment_id]
        if title in self.by_title:
            return self.by_title[title]
        folded = nfc_casefold(title)
        if folded in self.by_folded_title:
            return self.by_folded_title[folded]
        return safe_attachment_name(title)


def plan_attachment_names(attachments: list[object]) -> AttachmentNamePlan:
    """Allocate unique safe local filenames for one page's attachments.

    Deduplicates by attachment id (first-seen wins).  For each unique
    NFC-casefold base name, the earliest attachment (sorted by
    ``(created_at, nfc_casefold(title), att_id, index)``) keeps the bare
    sanitized name; later colliders get ``_with_suffix_token`` names that
    incorporate the attachment id (or title) so the result is stable across
    re-runs regardless of API listing order.

    ``by_folded_title`` is only populated for folded keys that map to exactly
    one title (unambiguous casefold); ambiguous folds are left out so the
    caller can fall back to exact-title or fresh sanitization.
    """
    by_id: dict[str, str] = {}
    by_title: dict[str, str] = {}
    by_folded_title: dict[str, str] = {}

    # --- Phase 1: collect entries, deduplicating by id ---
    seen_ids: set[str] = set()
    # groups: base_key -> list of (sort_key, owner, att_id, title, base)
    groups: dict[str, list[tuple[tuple, str, str, str, str]]] = {}

    for index, att in enumerate(attachments):
        att_id = str(getattr(att, "id", "") or "")
        if att_id and att_id in seen_ids:
            continue
        if att_id:
            seen_ids.add(att_id)
        title = str(getattr(att, "title", "") or "")
        created_at = str(getattr(att, "created_at", "") or "")
        # version attribute may be a PageVersion model (v2) or SimpleNamespace (tests)
        version_obj = getattr(att, "version", None)
        if version_obj is None:
            version_number = 0
            version_created = created_at
        elif isinstance(version_obj, str):
            version_number = 0
            version_created = version_obj
        else:
            version_number = int(getattr(version_obj, "number", 0) or 0)
            version_created = str(getattr(version_obj, "created_at", "") or created_at)

        base = safe_attachment_name(title)
        # Stable owner key: attachment id if present, else title+index
        owner = att_id if att_id else f"{title}\0{index}"
        sort_key = (version_created or created_at, nfc_casefold(title), att_id, str(index))
        groups.setdefault(nfc_casefold(base), []).append(
            (sort_key, owner, att_id, title, base)
        )

    # --- Phase 2: assign names ---
    # taken: casefold(name) -> owner
    taken: dict[str, str] = {}
    # assignments: owner -> (att_id, title, final_name)
    assignments: dict[str, tuple[str, str, str]] = {}

    for base_key, entries in groups.items():
        ordered = sorted(entries, key=lambda e: e[0])
        # First (earliest) entry gets the bare base name
        _, owner, att_id, title, base = ordered[0]
        taken[base_key] = owner
        assignments[owner] = (att_id, title, base)

    reserved_base_keys = set(groups)

    # Remaining entries (colliders) in stable sort order
    remaining = [
        (base_key, entry)
        for base_key, entries in groups.items()
        for entry in sorted(entries, key=lambda item: item[0])[1:]
    ]

    for _base_key, entry in sorted(remaining, key=lambda item: (item[0], item[1][0])):
        _sort_key, owner, att_id, title, base = entry
        token = att_id or title or "attachment"
        retry = 0
        while True:
            candidate = _with_suffix_token(base, token, retry=retry)
            collision_key = nfc_casefold(candidate)
            if collision_key not in taken and collision_key not in reserved_base_keys:
                break
            retry += 1
        taken[nfc_casefold(candidate)] = owner
        assignments[owner] = (att_id, title, candidate)

    # --- Phase 3: populate lookup dicts ---
    for att_id, title, final_name in assignments.values():
        if att_id:
            by_id[att_id] = final_name
        by_title.setdefault(title, final_name)

    # by_folded_title: only for unambiguous (1-to-1) title→folded mappings
    folded_titles: dict[str, list[str]] = {}
    for _att_id, title, _final_name in assignments.values():
        folded_titles.setdefault(nfc_casefold(title), []).append(title)
    for folded, titles in folded_titles.items():
        if len(titles) == 1:
            by_folded_title[folded] = by_title[titles[0]]

    return AttachmentNamePlan(
        by_id=by_id,
        by_title=by_title,
        by_folded_title=by_folded_title,
    )
