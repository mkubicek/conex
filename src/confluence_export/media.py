"""Attachment download and media directory management."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from confluence_export.client import AuthenticationError, ConfluenceClient
from confluence_export.diagnostics import WarningCollector
from confluence_export.filemode import default_file_mode, replacement_mode
from confluence_export.paths import (
    attachment_identity,
    is_safe_component,
    nfc_casefold,
    plan_attachment_names,
    resolve_within,
    safe_attachment_name,
)
from confluence_export.types import Attachment

_VERSIONS_FILE = ".versions.json"
MEDIA_DIR_NAME = ".media"
# User preparation files attached to a page (scripts, notes). Preserved across
# re-exports and, when a page moves, deliberately left in place (never
# auto-relocated — issue #17, Option B); the user is told where the page went.
# Shared here so the exporter, reconciler, git prune, and frontmatter scan agree.
WORKSPACE_DIR_NAME = ".workspace"


def ensure_media_dir(page_dir: Path) -> Path:
    """Create and return the .media/ subdirectory for a page."""
    media_dir = page_dir / MEDIA_DIR_NAME
    if media_dir.is_symlink():
        raise ValueError(f"refusing to use symlinked media directory: {media_dir}")
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


# TODO(migration): Remove after 2027-01-01 — all users will have migrated by then
def migrate_media_dirs(root_dir: Path) -> list[tuple[Path, Path]]:
    """Rename legacy media/ directories to .media/ throughout an export tree.

    Only renames directories that contain .versions.json (the manifest created
    by download_attachments), which reliably identifies attachment directories
    vs. page directories that happen to be named "media".

    Returns list of (old_path, new_path) tuples for each renamed directory.
    """
    renamed: list[tuple[Path, Path]] = []
    # Prune heavy/irrelevant trees DURING traversal (P1): never descend git
    # internals, already-migrated .media, user .workspace, or local .conex. This
    # keeps the walk O(page dirs) instead of O(entire export tree, including
    # gigabytes of attachments) on every export, and is also a correctness guard
    # (a legacy "media/" inside .git is not ours to migrate).
    skip = {".git", MEDIA_DIR_NAME, WORKSPACE_DIR_NAME, ".conex"}
    for dirpath, dirnames, _filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if "media" not in dirnames:
            continue
        candidate = Path(dirpath) / "media"
        if not (candidate / _VERSIONS_FILE).exists():
            continue
        new_path = candidate.parent / MEDIA_DIR_NAME
        if new_path.exists():
            continue
        candidate.rename(new_path)
        renamed.append((candidate, new_path))
        # Don't descend into the just-renamed (now migrated) attachment dir.
        dirnames.remove("media")
    return renamed


# Prefixes of the dot-prefixed temp artifacts of the atomic-write/rollback
# machinery. The mkstemp/TemporaryDirectory creation sites and the sweep
# patterns below derive from the SAME constants, so renaming a prefix (or
# adding a new temp kind) cannot silently regress the sweep (#48).
DOWNLOAD_TMP_PREFIX = ".download-"
COPY_TMP_PREFIX = ".copy-"
VERSIONS_TMP_PREFIX = ".versions-"
ROLLBACK_TMP_PREFIX = ".rollback-"  # exporter.py snapshot_file
PRESERVE_TMP_PREFIX = ".preserve-"
DRAWIO_TMP_PREFIX = ".drawio-"  # drawio.py render temps land in the media dir

# A hard kill (SIGKILL / power loss) between create and cleanup can leave these
# behind; they are never staged or pruned (commit_export stages explicit paths,
# the stale-prune walks tracked files only), so the worst case is untracked
# litter the user notices. safe_attachment_name rejects leading-dot titles, so
# no NEW attachment can be allocated one of these names — and the live
# ".versions.json" matches none of the patterns. But LEGACY (pre-sanitizer)
# exports wrote raw titles to disk and _resolve_manifest_entry deliberately
# keeps honoring those names, so the sweep must spare anything the manifest
# still resolves (see sweep_stale_media_temps).
_TEMP_ARTIFACT_PATTERNS = (
    f"{DOWNLOAD_TMP_PREFIX}*.tmp",
    f"{COPY_TMP_PREFIX}*.tmp",
    f"{VERSIONS_TMP_PREFIX}*.tmp",
    f"{ROLLBACK_TMP_PREFIX}*.tmp",
    f"{PRESERVE_TMP_PREFIX}*",
    f"{DRAWIO_TMP_PREFIX}*.png",
)

# A matching artifact YOUNGER than this is treated as another live run's
# in-flight transaction file, not a crashed run's litter. Nothing enforces
# single-process exclusivity on an output tree (no lockfile), so without the
# age gate an overlapping export would sweep its peer's live .rollback-/
# .download- temps — turning best-effort cleanup into a cross-process
# data-loss race. Litter from a crash is still swept by any run starting at
# least this much later.
_TEMP_ARTIFACT_MIN_AGE_S = 3600.0


def sweep_stale_media_temps(media_dir: Path) -> None:
    """Best-effort removal of temp artifacts a hard-killed earlier run left in
    ``media_dir`` (#48). MUST run before the current run creates its own temps
    and rollback snapshots there. Only entries older than
    ``_TEMP_ARTIFACT_MIN_AGE_S`` are removed — a younger match may belong to a
    concurrently running export (see the constant's comment) — and never a
    name the version manifest still resolves (a legacy-named REAL attachment,
    see the patterns' comment)."""
    now = time.time()
    manifest_names = {nfc_casefold(n) for n in _load_versions(media_dir)}
    for pattern in _TEMP_ARTIFACT_PATTERNS:
        for stale in media_dir.glob(pattern):
            try:
                # Folded compare: a legacy manifest key's case/Unicode form
                # can differ from the on-disk name. Sparing too much keeps
                # litter; deleting too much destroys a committed attachment.
                if nfc_casefold(stale.name) in manifest_names:
                    continue
                if now - stale.lstat().st_mtime < _TEMP_ARTIFACT_MIN_AGE_S:
                    continue
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale, ignore_errors=True)
                elif (
                    stale.name.startswith(PRESERVE_TMP_PREFIX)
                    and not stale.is_symlink()
                ):
                    # .preserve-* snapshots are always DIRECTORIES
                    # (TemporaryDirectory); a regular file matching the one
                    # suffix-less pattern can only be a legacy-named real
                    # attachment — never ours to delete. (A symlink is still
                    # unlinked: conex never creates those here either, and
                    # unlinking cannot destroy the target's content.)
                    continue
                else:
                    stale.unlink()
            except OSError:
                continue


def _load_versions(media_dir: Path) -> dict:
    """Load the version manifest from a media directory."""
    p = media_dir / _VERSIONS_FILE
    if p.exists():
        try:
            with open(p) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}
    return {}


def _save_versions(media_dir: Path, versions: dict) -> None:
    """Save the version manifest to a media directory."""
    target = media_dir / _VERSIONS_FILE
    mode = replacement_mode(target)
    fd, tmp_name = tempfile.mkstemp(dir=media_dir, prefix=VERSIONS_TMP_PREFIX, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(versions, f, indent=2)
        tmp.chmod(mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover
            pass
        raise


def _resolve_manifest_entry(media_dir: Path, name: str) -> Path:
    """Resolve an existing manifest filename, including legacy dot/dash names."""
    name = str(name)
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).name != name
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
        or nfc_casefold(name) == nfc_casefold(_VERSIONS_FILE)
    ):
        raise ValueError(f"unsafe manifest path component: {name!r}")
    if is_safe_component(name):
        try:
            return resolve_within(media_dir, name)
        except OSError as exc:
            raise ValueError(f"unusable manifest path component: {name!r}") from exc
    root = media_dir.resolve()
    leaf = media_dir / name
    try:
        if leaf.is_symlink():
            raise ValueError(f"refusing to use symlinked manifest path component: {name!r}")
        candidate = leaf.resolve()
        candidate.relative_to(root)
    except OSError as exc:
        raise ValueError(f"unusable manifest path component: {name!r}") from exc
    except ValueError as exc:
        raise ValueError(f"manifest path component escapes base directory: {name!r}") from exc
    return candidate


def _manifest_version(record: object) -> int:
    if isinstance(record, dict):
        try:
            return int(record.get("version", 0))
        except (TypeError, ValueError):
            return 0
    try:
        return int(record)  # legacy manifest: {"file.png": 3}
    except (TypeError, ValueError):
        return 0


def _manifest_owner(record: object) -> str:
    return str(record.get("id", "") or "") if isinstance(record, dict) else ""


def _manifest_entry(att: Attachment) -> dict[str, object]:
    return {
        "version": att.version.number,
        "id": att.id,
        "title": att.title,
        "key": attachment_identity(att),
    }


def _structured_no_id_record_matches(record: object, att: Attachment) -> bool:
    return (
        isinstance(record, dict)
        and not _manifest_owner(record)
        and not att.id
        and str(record.get("key", "") or "") == attachment_identity(att)
    )


def _record_belongs_to_attachment(record: object, att: Attachment) -> bool:
    owner = _manifest_owner(record)
    if owner:
        return owner == att.id
    return _structured_no_id_record_matches(record, att)


def _collision_bases(attachments: list[Attachment]) -> set[str]:
    counts: dict[str, int] = {}
    for att in attachments:
        base = nfc_casefold(safe_attachment_name(att.title))
        counts[base] = counts.get(base, 0) + 1
    return {name for name, count in counts.items() if count > 1}


def _version_matches(
    versions: dict,
    name: str,
    att: Attachment,
    collision_bases: set[str],
) -> bool:
    record = versions.get(name)
    if _manifest_version(record) != att.version.number:
        return False
    owner = _manifest_owner(record)
    if owner:
        return owner == att.id
    if _structured_no_id_record_matches(record, att):
        return True
    if isinstance(record, dict):
        return False
    # A legacy ``{title: int}`` manifest (written by older conex) records no
    # attachment id, so it cannot prove the on-disk file is THIS attachment rather
    # than a same-titled, same-version one. We DELIBERATELY re-download instead of
    # trusting it (unlike older versions, which skipped on title+version alone).
    # This is one-time: the skip/download path rewrites the manifest in the current
    # id-bearing format, so the next export skips unchanged attachments normally.
    # Not a bug — see test_legacy_manifest_with_id_redownloads_before_claiming_owner.
    if att.id:
        return False
    return nfc_casefold(safe_attachment_name(att.title)) not in collision_bases


def _legacy_record_can_preserve(
    record: object,
    name: str,
    att: Attachment,
    collision_bases: set[str],
) -> bool:
    if isinstance(record, dict):
        return False
    if _manifest_owner(record):
        return False  # pragma: no cover
    if _manifest_version(record) <= 0:
        return False
    if name != safe_attachment_name(att.title):
        return False
    return nfc_casefold(safe_attachment_name(att.title)) not in collision_bases


def _legacy_record_can_migrate(
    record: object,
    old_name: str,
    name: str,
    att: Attachment,
    collision_bases: set[str],
) -> bool:
    if isinstance(record, dict):
        return False
    if _manifest_version(record) <= 0:
        return False
    if old_name != att.title:
        return False
    if name != safe_attachment_name(att.title):
        return False
    return nfc_casefold(safe_attachment_name(att.title)) not in collision_bases


def _unmanifested_file_can_preserve(
    record: object,
    name: str,
    att: Attachment,
    collision_bases: set[str],
) -> bool:
    if record is not None:
        return False
    if name != safe_attachment_name(att.title):
        return False
    return nfc_casefold(safe_attachment_name(att.title)) not in collision_bases


def _copy_media_file(src: Path, dest: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=COPY_TMP_PREFIX, suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover
            pass
        raise


def _same_existing_file(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _find_previous_owned_file(
    versions: dict,
    media_dir: Path,
    name: str,
    att: Attachment,
    collision_bases: set[str],
) -> tuple[str, Path, object] | None:
    for old_name, old_record in versions.items():
        if old_name == name:
            continue
        if not (
            _record_belongs_to_attachment(old_record, att)
            or _legacy_record_can_migrate(old_record, old_name, name, att, collision_bases)
        ):
            continue
        try:
            old_path = _resolve_manifest_entry(media_dir, old_name)
        except ValueError:
            continue
        if old_path.is_file():
            return old_name, old_path, old_record
    return None


def _record_still_points_to_attachment(
    versions: dict,
    old_name: str,
    name: str,
    att: Attachment,
    collision_bases: set[str],
) -> bool:
    record = versions.get(old_name)
    return (
        _record_belongs_to_attachment(record, att)
        or _legacy_record_can_migrate(record, old_name, name, att, collision_bases)
    )


def _snapshot_previous_owned_files(
    versions: dict,
    media_dir: Path,
    attachments: list[Attachment],
    name_plan,
    collision_bases: set[str],
) -> tuple[dict[int, tuple[str, Path, object, Path]], tempfile.TemporaryDirectory | None]:
    snapshots: dict[int, tuple[str, Path, object, Path]] = {}
    tmp_dir: tempfile.TemporaryDirectory | None = None
    try:
        for index, att in enumerate(attachments):
            name = name_plan.for_attachment(att)
            found = _find_previous_owned_file(versions, media_dir, name, att, collision_bases)
            if found is None:
                continue
            old_name, old_path, old_record = found
            if tmp_dir is None:
                tmp_dir = tempfile.TemporaryDirectory(dir=media_dir, prefix=PRESERVE_TMP_PREFIX)
            snapshot_path = Path(tmp_dir.name) / f"{index}"
            shutil.copy2(old_path, snapshot_path)
            snapshots[id(att)] = (old_name, snapshot_path, old_record, old_path)
    except Exception:
        # A raise here happens before the caller binds tmp_dir to its
        # try/finally, which would leave the .preserve- dir behind until GC (#48).
        if tmp_dir is not None:
            tmp_dir.cleanup()
        raise
    return snapshots, tmp_dir


def available_attachment_names(attachments: list[Attachment], media_dir: Path) -> set[str]:
    """Names that are safe for regenerated markdown to reference.

    A filename is available only when the file exists at the current plan and
    the manifest proves that path belongs to the same current attachment (or a
    non-colliding legacy record can still prove it by title/version). This keeps
    markdown from linking to stale bytes left at a planned name by another
    attachment.
    """
    versions = _load_versions(media_dir)
    name_plan = plan_attachment_names(attachments)
    collision_bases = _collision_bases(attachments)
    available: set[str] = set()
    for att in attachments:
        name = name_plan.for_attachment(att)
        try:
            dest = resolve_within(media_dir, name)
        except ValueError:
            continue
        record = versions.get(name)
        if not dest.is_file():
            continue
        if (
            _record_belongs_to_attachment(record, att)
            or _legacy_record_can_preserve(record, name, att, collision_bases)
            or (
                _unmanifested_file_can_preserve(record, name, att, collision_bases)
                and _find_previous_owned_file(
                    versions, media_dir, name, att, collision_bases
                ) is None
            )
        ):
            available.add(name)
    return available


def _record_download_warning(
    att: Attachment, exc: Exception, warnings: WarningCollector | None
) -> None:
    """Print a clear best-effort download warning and tally it by category. A 404 on
    the binary (the metadata listed the file, but its bytes are gone) is *unavailable
    content*, not an exporter endpoint bug — word it so the user doesn't misread it.
    A 401/403 is distinct: the metadata listed fine but the binary fetch was rejected,
    which is most often a token-type problem (granular/scoped API tokens are widely
    reported to fail attachment downloads even with the attachment scope granted), so
    point the user at the one fix rather than burying it as a generic failure."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError) and status == 404:
        category = "attachment unavailable (HTTP 404)"
        print(
            f"  Warning: attachment '{att.title}': metadata exists but its binary is "
            "unavailable (HTTP 404)",
            file=sys.stderr,
        )
    elif isinstance(exc, AuthenticationError):
        category = "attachment download forbidden (HTTP 401/403)"
        print(
            f"  Warning: HTTP {exc.status_code} downloading '{att.title}': its metadata "
            "is listed but the binary fetch was rejected. Granular/scoped API tokens are "
            "known to fail attachment downloads even with the attachment scope; if these "
            "persist across runs, use a classic (unscoped) API token.",
            file=sys.stderr,
        )
    elif isinstance(exc, requests.exceptions.Timeout):
        category = "attachment download timeout"
        print(f"  Warning: timed out downloading {att.title}: {exc}", file=sys.stderr)
    else:
        category = "attachment download failed"
        print(f"  Warning: failed to download {att.title}: {exc}", file=sys.stderr)
    if warnings is not None:
        warnings.record(category)


def download_attachments(
    client: ConfluenceClient,
    attachments: list[Attachment],
    media_dir: Path,
    skip_existing: bool = True,
    warnings: WarningCollector | None = None,
) -> list[Path]:
    """Download attachments to media_dir. Returns list of downloaded file paths.

    Skips files whose local version matches the API version when skip_existing=True.
    """
    versions = _load_versions(media_dir)
    name_plan = plan_attachment_names(attachments)
    collision_bases = _collision_bases(attachments)
    previous_snapshots, snapshot_tmp_dir = _snapshot_previous_owned_files(
        versions, media_dir, attachments, name_plan, collision_bases
    )
    downloaded: list[Path] = []
    to_download: list[tuple[Attachment, str, Path]] = []

    def copy_previous_owned_file(name: str, att: Attachment, dest: Path) -> Path | None:
        snapshot = previous_snapshots.get(id(att))
        if snapshot is not None:
            old_name, old_path, old_record, original_path = snapshot
            found = (old_name, old_path, old_record)
        else:
            found = _find_previous_owned_file(versions, media_dir, name, att, collision_bases)
            original_path = found[1] if found is not None else None
        if found is None:
            return None
        old_name, old_path, old_record = found
        if dest.exists():
            if original_path is not None and _same_existing_file(original_path, dest):
                if _record_still_points_to_attachment(
                    versions, old_name, name, att, collision_bases
                ):
                    versions.pop(old_name, None)
                versions[name] = old_record
                return dest
            record = versions.get(name)
            if (
                _record_belongs_to_attachment(record, att)
                or _legacy_record_can_preserve(record, name, att, collision_bases)
            ):
                return dest if dest.is_file() else None
            try:
                dest.unlink()
            except OSError:
                return None
        _copy_media_file(old_path, dest)
        if _record_still_points_to_attachment(
            versions, old_name, name, att, collision_bases
        ):
            versions.pop(old_name, None)
        versions[name] = old_record
        return dest

    def keep_existing(
        name: str,
        att: Attachment,
        dest: Path,
        *,
        allow_legacy: bool = False,
    ) -> None:
        record = versions.get(name)
        if _record_belongs_to_attachment(record, att) and dest.is_file():
            if dest not in downloaded:
                downloaded.append(dest)
            return
        if (
            allow_legacy
            and _legacy_record_can_preserve(record, name, att, collision_bases)
            and dest.is_file()
        ):
            if dest not in downloaded:
                downloaded.append(dest)
            return

        kept_path = copy_previous_owned_file(name, att, dest)
        if kept_path is not None:
            if kept_path not in downloaded:
                downloaded.append(kept_path)
            return
        if _unmanifested_file_can_preserve(record, name, att, collision_bases) and dest.is_file():
            if dest not in downloaded:
                downloaded.append(dest)
            return

    try:
        for att in attachments:
            # S1: an untrusted attachment title must never write outside .media/.
            # safe_attachment_name keeps benign titles verbatim (so existing links
            # and the manifest still resolve) and neutralizes only escaping ones;
            # resolve_within is the defence-in-depth assert at the write site.
            name = name_plan.for_attachment(att)
            dest = resolve_within(media_dir, name)
            if (
                skip_existing
                and dest.is_file()
                and att.version.number > 0
                and _version_matches(versions, name, att, collision_bases)
            ):
                downloaded.append(dest)
                versions[name] = _manifest_entry(att)
                continue
            if not att.download_link:
                keep_existing(name, att, dest, allow_legacy=True)
                print(f"  Warning: no download link for {att.title}", file=sys.stderr)
                continue
            copy_previous_owned_file(name, att, dest)
            to_download.append((att, name, dest))

        def _download_one(item: tuple[Attachment, str, Path]) -> Path:
            att, _name, dest = item
            # Prefer the v1 REST attachment-download endpoint over the legacy
            # `_links.download` path (`/wiki/download/attachments/...`). The REST
            # endpoint works on both the site URL and the OAuth gateway URL used
            # for scoped API tokens, whereas the legacy download path 401s through
            # the gateway. Fall back to the legacy path only when the cached
            # attachment has no page_id (very old caches written before this field
            # existed).
            if att.page_id and att.id:
                download_path = (
                    f"/wiki/rest/api/content/{att.page_id}"
                    f"/child/attachment/{att.id}/download"
                )
            else:
                download_path = att.download_link
                if not download_path.startswith("/wiki"):
                    download_path = f"/wiki{download_path}"
            mode = replacement_mode(dest, default_mode=download_default_mode)
            fd, tmp_name = tempfile.mkstemp(
                dir=dest.parent,
                prefix=DOWNLOAD_TMP_PREFIX,
                suffix=".tmp",
            )
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                client.download_attachment_to_file(download_path, str(tmp))
                tmp.chmod(mode)
                os.replace(tmp, dest)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover
                    pass
                raise
            return dest

        download_default_mode = default_file_mode() if to_download else None
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_download_one, item): item for item in to_download}
            for future in as_completed(futures):
                att, name, dest = futures[future]
                try:
                    downloaded.append(future.result())
                    versions[name] = _manifest_entry(att)
                except Exception as exc:
                    keep_existing(name, att, dest, allow_legacy=True)
                    _record_download_warning(att, exc, warnings)

        _save_versions(media_dir, versions)
        downloaded.append(media_dir / _VERSIONS_FILE)
    finally:
        if snapshot_tmp_dir is not None:
            snapshot_tmp_dir.cleanup()

    return downloaded


def materialize_existing_attachments(
    attachments: list[Attachment],
    media_dir: Path,
) -> list[Path]:
    """Copy/keep existing media into the current attachment name plan.

    Used by ``--no-media`` exports: no remote downloads are attempted, but local
    last-good files may still need to move to the current planned filename so
    regenerated markdown links resolve.
    """
    versions = _load_versions(media_dir)
    name_plan = plan_attachment_names(attachments)
    planned_names = {name_plan.for_attachment(att) for att in attachments}
    collision_bases = _collision_bases(attachments)
    previous_snapshots, snapshot_tmp_dir = _snapshot_previous_owned_files(
        versions, media_dir, attachments, name_plan, collision_bases
    )
    written: list[Path] = []
    kept_names: set[str] = set()
    conflicting_names: set[str] = set()
    changed = False

    try:
        for att in attachments:
            name = name_plan.for_attachment(att)
            dest = resolve_within(media_dir, name)
            record = versions.get(name)
            if (
                (_record_belongs_to_attachment(record, att)
                 or _legacy_record_can_preserve(record, name, att, collision_bases))
                and dest.is_file()
            ):
                kept_names.add(name)
                written.append(dest)
                continue
            if record is not None and dest.exists():
                conflicting_names.add(name)

            materialized = False
            snapshot = previous_snapshots.get(id(att))
            if snapshot is not None:
                old_name, old_path, old_record, original_path = snapshot
                previous_owned = (old_name, old_path, old_record)
            else:
                previous_owned = _find_previous_owned_file(
                    versions, media_dir, name, att, collision_bases
                )
                original_path = previous_owned[1] if previous_owned is not None else None
            if previous_owned is not None:
                old_name, old_path, old_record = previous_owned
                if dest.exists():
                    if original_path is not None and _same_existing_file(original_path, dest):
                        if _record_still_points_to_attachment(
                            versions, old_name, name, att, collision_bases
                        ):
                            versions.pop(old_name, None)
                        versions[name] = old_record
                        kept_names.add(name)
                        written.append(dest)
                        changed = True
                        materialized = True
                        continue
                    try:
                        dest.unlink()
                    except OSError:
                        old_path = None
                    else:
                        if not _record_belongs_to_attachment(versions.get(name), att):
                            versions.pop(name, None)
                        conflicting_names.discard(name)
                        changed = True
                if old_path is not None:
                    _copy_media_file(old_path, dest)
                    if _record_still_points_to_attachment(
                        versions, old_name, name, att, collision_bases
                    ):
                        versions.pop(old_name, None)
                    versions[name] = old_record
                    if old_name in planned_names:
                        conflicting_names.add(old_name)
                    kept_names.add(name)
                    written.append(dest)
                    changed = True
                    materialized = True

            if (
                not materialized
                and previous_owned is None
                and _unmanifested_file_can_preserve(record, name, att, collision_bases)
                and dest.is_file()
            ):
                kept_names.add(name)
                written.append(dest)

        for name in conflicting_names:
            if name in kept_names:
                continue
            try:
                dest = _resolve_manifest_entry(media_dir, name)
            except ValueError:  # pragma: no cover
                continue
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
                versions.pop(name, None)
                changed = True

        if changed:
            _save_versions(media_dir, versions)
        if (media_dir / _VERSIONS_FILE).exists():
            written.append(media_dir / _VERSIONS_FILE)
    finally:
        if snapshot_tmp_dir is not None:
            snapshot_tmp_dir.cleanup()
    return written
