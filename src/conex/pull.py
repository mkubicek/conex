"""pull.py — fetch Confluence space data into a Snapshot + BlobStore.

Contracts:
- pull() never touches the output tree outside .conex/ (I4-discipline: all
  writes go through BlobStore under .conex/blobs or .conex/tmp).
- Attachment downloads are best-effort: a failure writes a warning to stderr,
  records the attachment WITHOUT a blob entry, and sets
  snapshot.attachments_complete = False.  pull() never raises for download
  failures.
- Incremental skip: an (att_id, version) already in prev.attachment_blobs
  AND whose digest is present in blobs is not re-downloaded.
- derived_blobs from prev are carried forward verbatim (content-digest-keyed;
  they cannot go stale because their key encodes the source digest).
- Author prefetch: page and attachment version author_ids are resolved in
  parallel (one thread per unique id) and stored in snapshot.users.  When
  opts.author_lookup is False the lookup is skipped entirely and users == {}.
- include_archived=True with api.returns_archived==False: warn to stderr,
  record snapshot.include_archived = False (I3 depends on this truth).
- The snapshot is saved atomically via SnapshotStore at the end.
"""

from __future__ import annotations

import datetime
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from conex.api import ConfluenceAPI
from conex.models import Attachment, Page
from conex.store.blobs import BlobStore
from conex.store.state import Snapshot, SnapshotStore

if TYPE_CHECKING:
    pass


@dataclass
class PullOptions:
    """Options controlling what pull() fetches and how.

    include_archived: request archived pages from the API.
    fetch_media: download attachment binaries (set False for metadata-only).
    author_lookup: resolve author display names via the API.
    workers: size of the shared attachment-download thread pool.
    """

    include_archived: bool = False
    fetch_media: bool = True
    author_lookup: bool = True
    workers: int = 8


def pull(
    api: ConfluenceAPI,
    space_key: str,
    root: Path,
    blobs: BlobStore,
    prev: Snapshot | None,
    opts: PullOptions,
) -> Snapshot:
    """Fetch space data and materialise a Snapshot.

    Algorithm:
    1. Resolve the space via api.get_space(space_key).
    2. List folders and pages in parallel.
    3. Fetch missing page bodies (body_storage == "") in parallel; store each
       body as a blob in body_blobs keyed by page_id.
    4. Fetch attachment lists per page sequentially (network-light; listing
       only, no binaries yet).
    5. Download attachment binaries via a shared worker pool (opts.workers).
       Incremental: skip (att_id, version) already in prev.attachment_blobs
       when blobs.has(digest).  Failure -> stderr warning + no blob entry +
       attachments_complete = False.
    6. Carry prev.derived_blobs forward verbatim.
    7. Prefetch author display names for unique author_ids (page + attachment
       versions) in parallel; skip when opts.author_lookup is False.
    8. Save snapshot atomically via SnapshotStore.

    Returns the new Snapshot.

    Raises ApiError / AuthError for space-resolution or listing failures
    (these are not best-effort).  Never raises for individual attachment
    download failures.
    """
    # ------------------------------------------------------------------
    # 1. Archived-mode compatibility check
    # ------------------------------------------------------------------
    effective_archived = opts.include_archived
    if opts.include_archived and not api.returns_archived:
        print(
            "conex: warning: include_archived=True requested but this auth mode "
            "cannot list archived pages; fetching current pages only.",
            file=sys.stderr,
        )
        effective_archived = False

    # ------------------------------------------------------------------
    # 2. Resolve space
    # ------------------------------------------------------------------
    space = api.get_space(space_key)

    # ------------------------------------------------------------------
    # 3. List folders and pages (parallel)
    # ------------------------------------------------------------------
    with ThreadPoolExecutor(max_workers=2) as executor:
        folders_future = executor.submit(api.get_folders, space.id)
        pages_future = executor.submit(
            api.get_pages, space.id, space_key, effective_archived
        )
        folders = folders_future.result()
        pages = pages_future.result()

    # ------------------------------------------------------------------
    # 4. Fetch missing page bodies in parallel
    # ------------------------------------------------------------------
    body_blobs: dict[str, str] = {}
    pages_needing_body = [p for p in pages if p.body_storage == ""]
    pages_with_body = [p for p in pages if p.body_storage != ""]

    # Store bodies that came inline
    for page in pages_with_body:
        digest = blobs.add_bytes(page.body_storage.encode("utf-8"))
        body_blobs[page.id] = digest

    if pages_needing_body:
        def _fetch_body(page: Page) -> tuple[str, str]:
            # Best-effort: a single page whose body cannot be fetched (404 for a
            # page deleted mid-run, retry-exhausted 5xx) must NOT abort the whole
            # export.  But a transient failure must NOT blank an already-exported
            # page either: carry the prev body blob forward when we have one, so
            # the page's fingerprint is unchanged and the last-good markdown is
            # preserved.  Only a page with NO prior body falls back to empty.
            try:
                body = api.get_page_body(page.id)
            except Exception as exc:
                print(
                    f"conex: warning: failed to fetch body for page "
                    f"'{page.title}' ({page.id}): {exc}",
                    file=sys.stderr,
                )
                prev_digest = prev.body_blobs.get(page.id) if prev is not None else None
                if prev_digest and blobs.has(prev_digest):
                    return page.id, prev_digest
                body = ""
            digest = blobs.add_bytes(body.encode("utf-8"))
            return page.id, digest

        with ThreadPoolExecutor(max_workers=opts.workers) as executor:
            futures = {
                executor.submit(_fetch_body, p): p for p in pages_needing_body
            }
            for future in as_completed(futures):
                page_id, digest = future.result()
                body_blobs[page_id] = digest

    # Clear body_storage from pages before storing in snapshot (spec: bodies
    # live in body_blobs, never in page.body_storage on the snapshot).
    stripped_pages = [
        page.model_copy(update={"body_storage": ""}) for page in pages
    ]

    # ------------------------------------------------------------------
    # 5. Fetch attachment lists per page
    # ------------------------------------------------------------------
    attachments: dict[str, list[Attachment]] = {}
    listing_complete = True
    for page in pages:
        try:
            atts = api.get_attachments(page.id)
        except Exception as exc:
            # Best-effort: a single page's attachment listing failing must not
            # abort the whole export.  Warn, mark the snapshot incomplete (so the
            # build never treats the partial listing as authoritative and prunes
            # existing media), and continue.
            print(
                f"conex: warning: failed to list attachments for page "
                f"'{page.title}' ({page.id}): {exc}",
                file=sys.stderr,
            )
            listing_complete = False
            continue
        if atts:
            attachments[page.id] = atts

    # ------------------------------------------------------------------
    # 6. Download attachment binaries
    # ------------------------------------------------------------------
    attachment_blobs: dict[str, str] = {}
    attachments_complete = listing_complete

    prev_att_blobs = prev.attachment_blobs if prev is not None else {}

    if opts.fetch_media:
        # Build a flat list of (page_id, attachment) pairs to process.
        work: list[tuple[str, Attachment]] = []
        for page_id, atts in attachments.items():
            for att in atts:
                work.append((page_id, att))

        def _download_one(item: tuple[str, Attachment]) -> tuple[str, str | None]:
            """Return (att_key, digest_or_None).  None means download failed."""
            _page_id, att = item
            att_key = f"{att.id}@{att.version.number}"
            # Incremental skip
            existing_digest = prev_att_blobs.get(att_key)
            if existing_digest and blobs.has(existing_digest):
                return att_key, existing_digest
            # Resolve absolute URL via the adapter (adapter owns URL construction).
            url = api.attachment_download_url(att)
            if not url:
                print(
                    f"conex: warning: no download URL for attachment '{att.title}'",
                    file=sys.stderr,
                )
                return att_key, None
            # Download
            try:
                resp = api.download(url)
                try:
                    # Ensure the urllib3 stream decodes Content-Encoding
                    # (gzip/deflate) so blobs store the decompressed bytes.
                    # requests constructs HTTPResponse with decode_content=False
                    # by default; setting it True here matches v1's
                    # resp.iter_content() behaviour (PORT v1 media._download_one).
                    if hasattr(resp.raw, "decode_content"):
                        resp.raw.decode_content = True
                    digest, _size = blobs.add_stream(resp.raw)
                finally:
                    resp.close()
                return att_key, digest
            except Exception as exc:
                print(
                    f"conex: warning: failed to download attachment "
                    f"'{att.title}': {exc}",
                    file=sys.stderr,
                )
                return att_key, None

        with ThreadPoolExecutor(max_workers=opts.workers) as executor:
            futures = {executor.submit(_download_one, item): item for item in work}
            for future in as_completed(futures):
                att_key, digest = future.result()
                if digest is not None:
                    attachment_blobs[att_key] = digest
                else:
                    attachments_complete = False
    else:
        # No media fetch this run (e.g. the diff path): carry prev's
        # attachment_blobs forward verbatim so the saved snapshot keeps
        # referencing the blobs already in the store.  Otherwise a diff would
        # persist attachment_blobs={}, and a later build's GC keep-set (derived
        # from the current snapshot) would delete those blobs out from under the
        # exported .media/ files.  Mirrors the derived_blobs carry-forward below.
        attachment_blobs.update(prev_att_blobs)

    # ------------------------------------------------------------------
    # 7. Carry forward derived_blobs from prev
    # ------------------------------------------------------------------
    derived_blobs: dict[str, str] = {}
    if prev is not None:
        derived_blobs.update(prev.derived_blobs)

    # ------------------------------------------------------------------
    # 8. Author prefetch
    # ------------------------------------------------------------------
    users: dict[str, str] = {}
    if opts.author_lookup:
        author_ids: set[str] = set()
        for page in pages:
            if page.version.author_id:
                author_ids.add(page.version.author_id)
        for atts in attachments.values():
            for att in atts:
                if att.version.author_id:
                    author_ids.add(att.version.author_id)

        if author_ids:
            def _lookup_user(account_id: str) -> tuple[str, str]:
                """Return (account_id, display_name); "" on any failure."""
                try:
                    name = api.get_user_display_name(account_id)
                    return account_id, name
                except Exception:
                    return account_id, ""

            with ThreadPoolExecutor(max_workers=opts.workers) as executor:
                futures_users = {
                    executor.submit(_lookup_user, aid): aid
                    for aid in author_ids
                }
                for future in as_completed(futures_users):
                    try:
                        account_id, name = future.result()
                    except Exception:
                        continue
                    if name:
                        users[account_id] = name

    # ------------------------------------------------------------------
    # 9. Assemble and save snapshot
    # ------------------------------------------------------------------
    # Sort all maps before persisting so the serialised snapshot.json is
    # byte-identical across runs regardless of concurrent-future completion
    # order (prevents spurious git churn on the committed snapshot file).
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    snapshot = Snapshot(
        space=space,
        fetched_at=fetched_at,
        include_archived=effective_archived,
        attachments_complete=attachments_complete,
        pages=stripped_pages,
        folders=folders,
        body_blobs=dict(sorted(body_blobs.items())),
        attachments=attachments,
        attachment_blobs=dict(sorted(attachment_blobs.items())),
        derived_blobs=dict(sorted(derived_blobs.items())),
        users=dict(sorted(users.items())),
    )

    store = SnapshotStore(root)
    store.save(snapshot)

    return snapshot
