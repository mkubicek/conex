# conex v2 — from-scratch rewrite specification

This document is the binding contract for the v2 rewrite. Workers implement
EXACTLY these interfaces; deviations require a spec change, not an ad-hoc fix.
The old package lives at `src/confluence_export/` in this worktree — read it
for PORT items (marked **PORT**), never import from it.

## Why v2 exists (design thesis)

v1 records no durable state of its own: every run re-infers "what does conex
own, and where did it come from" by scanning the filesystem, the git index, and
per-file frontmatter. That inference layer (protection.py, provenance.py,
reconcile.py, the prune/restore half of git.py, the media snapshot/rollback/
sweep machinery — ~1,500 lines) is where v1's worst bugs lived.

v2 inverts this: **record provenance at write time** in one state store, and
make the build a **pure function** of fetched data. Three root decisions:

1. **State manifest** `.conex/state.json` — the single source of truth for
   "what conex owns". Prune = set difference on page IDs. Moves = lookups.
   `.workspace/` auto-carries on moves (safe now: we KNOW the old dir is page
   X's because the state says so). Incremental skip falls out for free.
2. **Pull/build split** — `pull` syncs raw data (bodies + attachments) into a
   content-addressed blob store + a snapshot file; `build` deterministically
   materializes the output tree from (snapshot, blobs, options). A
   half-finished download never gets promoted; a crashed build is simply
   re-run. There are NO per-page rollback transactions, NO sweeps, NO
   age gates.
3. **Typed API boundary** — pydantic models where `null == absent ==
   default`, enforced by one validator on the base class. The v1/v2 API
   dialects are two thin adapters producing the same models. The "#47 null
   class" is structurally unrepresentable.

Non-goals: byte-compatibility with v1 exports; reading/migrating v1 state
(.media/.versions.json manifests are ignored); the v1 `_archived` dirname
heuristics; lxml (deferred — keep `html.parser` parity with v1 output).

## Hard invariants (every reviewer checks these)

- **I1 — Ownership by ID:** conex only ever deletes a path it recorded in
  state under a page ID (generated artifacts: the `.md`, `.html`, `.media/`).
  It NEVER deletes or stages `.workspace/` content or any unrecorded file.
- **I2 — Zero-pages guard:** a build whose desired page set is empty while
  the previous state is non-empty performs NO prune and does NOT erase
  state; it warns and keeps everything (auth failures must not nuke exports).
- **I3 — Archived preservation:** a run whose snapshot did not include
  archived pages (`snapshot.include_archived == False`) never prunes a page
  whose previous state records `status == "archived"`.
- **I4 — All temp files live under `.conex/tmp/`**, which is cleared at the
  start of each locked run. Nothing temp-named is ever created next to user-
  visible files. Final files appear only via `os.replace` from `.conex/tmp/`.
- **I5 — Single writer:** every state-mutating command holds an exclusive
  flock on `.conex/lock` for its duration; a second runner fails fast with a
  clear message.
- **I6 — State is written atomically, once, at the end of a successful
  build.** A crash at any point leaves the previous state intact and the
  next run converges (build is deterministic).
- **I7 — Untrusted names:** attachment titles and page titles are untrusted.
  Every path that incorporates one goes through `paths.py` sanitization and
  `resolve_within()` before any filesystem operation (**PORT** v1's S1
  posture from `confluence_export/paths.py`).
- **I8 — Git stages exactly the build's delta** (written + deleted paths),
  chunked to respect argv limits. Never `git add -A`/`-u` for export commits.
  `.conex/` is gitignored. User-modified tracked files are committed
  separately BEFORE the export commit (port v1 behavior).

## Environment (for every worker)

- Worktree root: `/Users/mkubicek/repos/conex/.claude/worktrees/conex-v2-rewrite`
- New code: `src/conex/`, new tests: `tests_v2/`. NEVER modify
  `src/confluence_export/` or `tests/`.
- Run tests:
  `cd <root> && PYTHONPATH=<root>/src /Users/mkubicek/repos/conex/.venv/bin/pytest tests_v2/<your_file> -q`
  (PYTHONPATH is REQUIRED — the venv's editable install points at main's src.)
- Python ≥3.11. Deps available: pydantic v2, requests, beautifulsoup4,
  markdownify, pyyaml, pytest. NO lxml.
- Style: full type hints, docstrings state invariants/contracts (match the
  old package's docstring quality). No comments narrating what code does.

## Package layout

```
src/conex/
  __init__.py        __version__ = "2.0.0a0"
  __main__.py        # python -m conex
  errors.py          # exception hierarchy
  models.py          # API-boundary pydantic models
  paths.py           # sanitization, safe names, resolve_within   (PORT)
  http.py            # retry/backoff/429 session wrapper          (PORT)
  config.py          # auth/dialect resolution, configure          (PORT)
  api/__init__.py    # ConfluenceAPI protocol + make_api(config)
  api/v2.py          # Cloud v2 (+ gateway) adapter
  api/v1.py          # cookie/legacy v1 adapter
  store/__init__.py
  store/state.py     # ExportState/PageState/Snapshot models + StateStore + SnapshotStore
  store/blobs.py     # content-addressed BlobStore
  store/lock.py      # ExportLock flock
  layout.py          # page tree -> path plan                      (PORT allocator)
  convert/__init__.py  # convert_page, build_frontmatter, CONVERTER_VERSION
  convert/registry.py  # Macro dataclass, parse_macro, handler registry
  convert/macros.py    # all macro handlers + emoticon map         (PORT semantics)
  convert/render.py    # storage-XHTML -> markdown pipeline
  pull.py            # API -> snapshot + blobs
  build.py           # snapshot + blobs + prev state -> output tree + new state
  gitio.py           # thin git layer
  drawio.py          # preview-first + batch render fallback       (PORT knowledge)
  cli.py             # argparse CLI
tests_v2/
  conftest.py + test_<module>.py per module + test_e2e.py
```

## errors.py

```python
class ConexError(Exception): ...          # base; CLI prints str(e), exit 1
class ConfigError(ConexError): ...
class AuthError(ConexError): ...
class ApiError(ConexError):               # carries status: int | None, url: str
class LockHeldError(ConexError): ...      # message names the lock path + remedy
class GitError(ConexError): ...
```

## models.py — typed API boundary

Pydantic v2. Base class:

```python
class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def _null_means_default(cls, v, info):
        """An explicit JSON null is treated as an absent key: the field
        default applies. Kills the v1 '#47' crash class structurally."""
        # if v is None: return field default / default_factory result
        # ints arriving for str fields (v1 numeric ids): str(v)
```

Rules: every field has a default; `id`-like fields are `str` and coerce
`int -> str`; nested models default to their empty instance. `frozen=True`.

CONTRACT NOTES:
- `frozen=True` guards attribute REASSIGNMENT only. No `ApiModel` may carry
  a mutable collection field (lists/dicts live on Snapshot/State, which are
  non-frozen and owned by build). Never mutate a model's nested data.
- COOKIE_V1 cannot report folder parents (v1 REST `ancestors` are always
  pages); folder-parented pages surface as roots in that dialect.

```python
class PageVersion(ApiModel):
    number: int = 0
    created_at: str = ""        # ISO 8601 as given by the API
    message: str = ""
    author_id: str = ""

class Space(ApiModel):
    id: str = ""
    key: str = ""
    name: str = ""
    homepage_id: str = ""

class Folder(ApiModel):
    id: str = ""
    title: str = ""
    parent_id: str = ""
    position: int = 0

class Page(ApiModel):
    id: str = ""
    title: str = ""
    space_id: str = ""
    parent_id: str = ""         # page OR folder id; "" for space root
    parent_type: str = ""       # "page" | "folder" | ""
    position: int = 0
    status: str = "current"     # "current" | "archived"
    body_storage: str = ""      # storage-format XHTML; may be "" pre-body-fetch
    version: PageVersion = PageVersion()
    web_url: str = ""           # absolute URL for frontmatter

class Attachment(ApiModel):
    id: str = ""
    title: str = ""
    media_type: str = ""
    file_size: int = 0
    page_id: str = ""
    download_url: str = ""      # as given (usually site-relative)
    version: PageVersion = PageVersion()
```

Tests must include: explicit-null for EVERY field of EVERY model (round-trip
through `model_dump()` too), int ids, missing keys, junk nested shapes
(`{"version": None}`, `{"version": {}}`).

## paths.py — **PORT** from `confluence_export/paths.py`

Port verbatim-in-spirit (drop what only the old media manifests used):
`sanitize_filename` (page-title -> dir/file segment: word chars, space,
hyphen; 100-char cap), `safe_attachment_name` / `safe_component`
(neutralize `../`, separators, control chars, leading dots),
`resolve_within(base, name)` (defence-in-depth containment assert),
`nfc()`, `nfc_casefold()` folds, `truncate_with_suffix`, and
`plan_attachment_names` (the per-page collision-safe name plan).
Port the corresponding tests' INTENT (rewrite, don't copy, the test file).
NEVER swap the two sanitizers: page dirs/files use `sanitize_filename`;
attachment files use `safe_attachment_name`/`plan_attachment_names`.

## http.py — **PORT** retry core from `confluence_export/client.py`

```python
@dataclass
class HttpStats: requests: int; retries: int; rate_limit_sleep_s: float

class Http:
    def __init__(self, *, auth_headers: dict[str, str], timeout: float = 30.0,
                 max_retries: int = 3): ...
    def get_json(self, url: str, params: dict | None = None) -> Any
    def get_stream(self, url: str) -> requests.Response   # caller closes
```

Port: exponential backoff on 5xx/connection errors; cross-thread shared 429
window (`Retry-After` honored, capped 300s, default 60s on junk) so one 429
backs off the whole pool; exhausted 429 raises typed `ApiError` with status
429 (v1 issue #46 behavior); stats counters. Thread-safe (used by 8-worker
pools). 401/403 raise `AuthError`; 404 raises `ApiError(status=404)`.
`get_stream` honors the same shared 429 window and CLOSES the response
before any retry/raise (don't leak pooled connections — v1 `_get_raw`).

## config.py — **PORT** from `confluence_export/config.py`

Same user-facing behavior as v1: global config
`~/.config/confluence-export/config.json`, local `.conex/config.json`
discovered from output dir upward, env vars (`CONFLUENCE_*`), CLI flag
overrides, auth modes (api-token basic / PAT bearer / cookie), dialect
resolution (CLOUD_V2 / GATEWAY_V2 for scoped tokens via cloud-id gateway /
COOKIE_V1), `configure` + `configure --local` flows, "never prompt in
non-interactive runs". Produces:

```python
@dataclass(frozen=True)
class ResolvedConfig:
    site_url: str; api_base_url: str; auth_headers: dict[str, str]
    dialect: Dialect           # enum: CLOUD_V2, GATEWAY_V2, COOKIE_V1
    email: str = ""; verbose: bool = False
    source_description: str = ""   # for the preflight banner
```

Trim freely (v1 is 811 lines; target well under that) but keep every auth
mode working and the same env var names. The credential secret-file perms
(0600) behavior stays.

## api/ — dialect adapters

```python
class ConfluenceAPI(Protocol):
    returns_archived: bool      # False for COOKIE_V1 (current-only listing)
    def get_space(self, key: str) -> Space
    def get_pages(self, space_id: str, space_key: str,
                  include_archived: bool) -> list[Page]
        # bodies included when the dialect supports it in-listing (v2
        # body-format=storage); else body_storage == "" and pull fetches.
    def get_page_body(self, page_id: str) -> str
    def get_folders(self, space_id: str) -> list[Folder]
    def get_attachments(self, page_id: str) -> list[Attachment]
    def get_user_display_name(self, account_id: str) -> str   # "" if unknown
    def download(self, url: str) -> requests.Response          # streamed
def make_api(cfg: ResolvedConfig) -> ConfluenceAPI
```

**PORT endpoint knowledge from `confluence_export/client.py`** — these facts
are doc-verified and battle-tested; replicate them:
- v2: `/wiki/api/v2/spaces?keys=`, `/wiki/api/v2/spaces/{id}/pages?
  body-format=storage` (cursor pagination via `_links.next`), `/wiki/api/v2/
  spaces/{id}/folders`, `/wiki/api/v2/pages/{id}/attachments`.
- v1 (cookie): `/wiki/rest/api/...` offset pagination, separate
  status=current listing only (`returns_archived = False`), nested
  `body.storage.value`, numeric ids, `child/attachment` listing, v1 user
  lookup; downloads always via v1 `/wiki/download/...` URLs.
- Gateway: same v2 surface addressed via
  `https://api.atlassian.com/ex/confluence/{cloudId}`.
- Pagination envelopes may be malformed: a None `results`/`_links` must not
  crash. These guards live in ONE shared paginator helper
  (`data.get("results") or []`, `(data.get("_links") or {}).get("next")`).
  NOTE: v1 (cookie) pagination ALSO follows `_links.next` in this codebase
  (**PORT** `_paginate_offset` — do NOT implement start/limit arithmetic).
- **Downloads — the adapter owns absolute URLs.** `download(url)` is fed by
  the adapter, which prefers the constructed
  `/wiki/rest/api/content/{page_id}/child/attachment/{att_id}/download`
  endpoint resolved against `cfg.api_base_url` (works on site AND gateway —
  **PORT** v1 `media._download_one`'s strategy), falling back to
  `Attachment.download_url` resolved against `api_base_url` (prefix `/wiki`
  when missing). `pull` never builds URLs.
Adapters return MODELS ONLY; no raw dicts escape this layer.

## store/lock.py

```python
class ExportLock:
    """Exclusive advisory flock on <root>/.conex/lock for the whole run.
    Context manager. Non-blocking acquire; on contention raises
    LockHeldError('another conex run holds <path>; wait or remove if stale')."""
```

fcntl.flock (POSIX-only is fine; document it).

## store/blobs.py

```python
class BlobStore:
    """Content-addressed store at <root>/.conex/blobs/<aa>/<sha256-hex>.
    Writes stage into <root>/.conex/tmp and promote via os.replace; a digest
    that already exists is deduped (promotion skipped). Immutable after
    promote."""
    def __init__(self, root: Path)             # export root
    def add_stream(self, fp) -> tuple[str, int]   # (digest, size)
    def add_bytes(self, data: bytes) -> str
    def has(self, digest: str) -> bool
    def path(self, digest: str) -> Path           # raises KeyError if absent
    def read_bytes(self, digest: str) -> bytes
    def materialize(self, digest: str, dest: Path,
                    mtime: float | None = None) -> None
        # copy via .conex/tmp + os.replace(dest); sets mtime if given;
        # resolve_within-checked dest
    def gc(self, keep: set[str]) -> int           # returns count removed
```

## store/state.py

```python
class AttachmentState(BaseModel):
    version: int = 0
    file: str = ""                     # filename within the page's .media/
    blob: str = ""                     # sha256; "" if download failed
    size: int = 0

class PageState(BaseModel):
    dir: str = ""                      # POSIX relpath of page dir from root
    file: str = ""                     # POSIX relpath of the .md file
    html: str = ""                     # relpath of --include-html artifact, "" if none
    title: str = ""
    version: int = 0
    status: str = "current"            # "current" | "archived"
    fingerprint: str = ""              # see build.py
    attachments: dict[str, AttachmentState] = {}   # by attachment id

class ExportState(BaseModel):
    schema_version: int = 1
    space_key: str = ""; space_id: str = ""
    updated_at: str = ""               # ISO, set by caller
    converter_version: int = 0
    pages: dict[str, PageState] = {}   # by page id
    folders: dict[str, str] = {}       # folder_id -> dir relpath (for prune)

class Snapshot(BaseModel):
    schema_version: int = 1
    space: Space; fetched_at: str = ""
    include_archived: bool = False
    attachments_complete: bool = True
    pages: list[Page] = []             # body_storage EMPTIED here;
    folders: list[Folder] = []
    body_blobs: dict[str, str] = {}    # page_id -> body blob digest
    attachments: dict[str, list[Attachment]] = {}   # page_id -> atts
    attachment_blobs: dict[str, str] = {}  # f"{att_id}@{version}" -> digest
    derived_blobs: dict[str, str] = {}     # f"drawio-png:v{DRAWIO_RENDER_VERSION}:{xml_digest}" -> digest
    users: dict[str, str] = {}             # account_id -> display name

class StateStore:    # <root>/.conex/state.json
    def load(self) -> ExportState | None    # None on missing; on ANY
        # ValidationError/JSONDecodeError: warn to stderr, return None
    def save(self, state: ExportState) -> None   # atomic via .conex/tmp

class SnapshotStore: # <root>/.conex/snapshot.json — same load/save contract
```

`Snapshot.space` defaults to `Space()`. EVERY field of every store model has
a default, AND `load` additionally wraps `model_validate` in try/except
(warn + return None) — the wildcard validator alone cannot save a required
field from an explicit null, so there are none, and load never crashes.
Snapshot/State models are NOT frozen (build mutates copies) and share the
null-tolerant base validator via a mixin. Stores create `.conex/` dirs if
absent but NEVER clear `.conex/tmp` — the CLI owns that (see cli.py).

## layout.py — **PORT** allocator from `confluence_export/layout.py` + `tree.py`

```python
@dataclass(frozen=True)
class LayoutPlan:
    dirs: dict[str, PurePosixPath]      # page_id -> page DIR relpath
    files: dict[str, PurePosixPath]     # page_id -> .md file relpath
    order: list[str]                    # page ids, depth-first tree order

def plan_layout(space: Space, pages: list[Page], folders: list[Folder],
                *, subtree: str | None = None,
                no_children: bool = False) -> LayoutPlan
```

Port: tree build from parent ids with position sort; space root dir named
after the space (sanitized space name); page dir contains `<segment>.md`
with the same segment name; collision-safe allocation per parent
(NFC-casefold key, `-2`/`-3` suffixes, 100-char truncate-with-suffix);
archived pages under a synthetic `_archived/` root that participates in
collision allocation; a LIVE page whose parent is archived (and not
exported) surfaces as a root (v1 PR3 behavior).

**DELIBERATE DIVERGENCE from v1:** v1's `tree.py` ignores folders entirely
(a folder-parented page surfaces at the space root). v2 builds the tree over
pages AND folders: a folder is an internal node (a path segment dir, no
`.md`); a page with `parent_type == "folder"` nests under that folder node;
parents resolve by id across the merged page+folder set; a folder whose
parent is unknown surfaces as a root. Do NOT "port" v1's page-only logic.

**MIGRATION NOTE — space-root directory level (intentional divergence):** v2
roots every export at a `<Space-Name>/` directory (`out/<Space-Name>/Home/...`),
whereas v1 rooted at the space homepage (`out/Home/...`). This is deliberate: it
lets several spaces export into one output tree without colliding, and keeps the
space identity visible on disk. Consequence for anyone re-exporting a v1 tree
with v2: every file moves down one level, so `git log --follow` continuity and
any path-based tooling break across the switch. To preserve history, start the
v2 export in a fresh output directory (or `git mv` the old tree under the new
space dir before the first v2 run).

`subtree` ("/A/B"): segments match RAW TITLES case-insensitively (**PORT**
v1 `find_node_by_path`, including first-match-wins among same-titled
siblings — accepted behavior, do not "fix"). `plan_layout` resolves the
subtree node and returns the plan restricted to it (+descendants unless
`no_children`); build derives the prune scope from the resolved node's
PLANNED (sanitized) dir — the title->dir handoff happens inside layout, so
build never matches on raw titles.

## convert/ — storage XHTML -> markdown

```python
CONVERTER_VERSION = 1   # bump invalidates incremental skip

class MediaRefs:
    """Per-page attachment-name resolver. Built by build.py from ONE
    AttachmentNamePlan per page (PORT v1 paths.plan_attachment_names) — the
    SAME plan that names the files on disk, so links can never desync from
    filenames. Storage XML references attachments by TITLE/filename, not id:
    resolution must support by-id AND by-title (incl. NFC-casefold title
    fallback — PORT v1 `for_reference` semantics)."""
    def filename_for_id(self, att_id: str) -> str | None
    def filename_for_title(self, title: str) -> str | None

@dataclass
class ConvertContext:
    page: Page; space: Space; site_url: str
    attachments: list[Attachment]
    media: MediaRefs
    rendered_drawio: dict[str, str]   # diagram name -> png filename in .media
    resolve_user: Callable[[str], str]  # account_id -> display name or ""
    media_enabled: bool = True
    media_available: set[str] = field(default_factory=set)
    # media_available = filenames build materialized or confirmed
    # present-and-owned THIS run for THIS page (blob digest matches the
    # planned attachment). NEVER a raw os.listdir of .media/.

def convert_page(body_storage: str, ctx: ConvertContext) -> str   # md body
def build_frontmatter(page: Page, space: Space, human_path: str,
                      site_url: str) -> str
```

Frontmatter: YAML with `title, page_id, space_key, path, url, last_modified,
version`, plus `status: archived` only when archived (v1 shape).

### registry.py — the #45-class killer

```python
@dataclass
class Macro:
    name: str
    element: Tag
    params: dict[str, str]    # DIRECT ac:parameter children ONLY
    rich_body: Tag | None     # DIRECT ac:rich-text-body child ONLY
    plain_body: str | None    # DIRECT ac:plain-text-body child ONLY

def parse_macro(element: Tag) -> Macro
    # THE ONLY place params/bodies are extracted. recursive=False
    # everywhere. A nested macro's params/body can never be stolen.

Replacement = Tag | NavigableString | str | None   # None = remove element
Handler = Callable[[Macro, ConvertContext], Replacement]
def register(name: str): ...      # decorator -> HANDLERS registry
def default_handler(m, ctx): ...  # unknown macro: if it has own body,
    # render the body; if bodyless but WRAPS other macros, unwrap in place;
    # else emit an html comment placeholder "<!-- macro: name -->".
```

### render.py pipeline (BeautifulSoup, `html.parser` — NOT lxml)

Ordered passes over the soup; **PORT the semantics** from
`confluence_export/converter.py` (read it carefully — its behavior is the
oracle, its STRUCTURE is not):
1. ADF decision/task lists (incl. v1's innermost-first nested-list lift and
   the checked/unchecked + DECIDED ✓ rendering; v1 issues #40/#43).
2. Macro dispatch: every `ac:structured-macro`/`ac:adf-extension` through
   `parse_macro` + registry.
3. Links: `ac:link` with `ri:page` (v1 PARITY: no cross-page path
   resolution — render the link text, with the Confluence URL when
   derivable from site_url + space/title; PORT v1 `_replace_ac_link`),
   `ri:attachment` (link into `.media/` via ctx.media, only if available),
   `ri:user` (mention via resolve_user), CDATA link bodies.
4. Images: `ac:image` + `ri:attachment`/`ri:url`; emit `<img>` only when the
   file is in `ctx.media_available`, else alt-text fallback.
5. Emoticons: `ac:emoticon` -> Unicode (**PORT the full map**).
6. Layout unwrap: `ac:layout`/`ac:layout-section`/`ac:layout-cell`,
   `ac:adf-node` generic unwrap AFTER the special-cased ADF nodes.
7. `time` datetime elements, inline status, task placeholders.
8. markdownify with v1's option set (heading style ATX, bullets, code
   handling); then whitespace normalization; ensure single H1 title.

Macro handlers to implement in macros.py (semantics = v1 oracle): code,
panel/info/note/warning/tip, expand, status, jira, toc (omit, leave
comment), view-file/viewpdf/viewppt/viewxls (link to media file), drawio +
drawio-sketch (image ref via ctx.rendered_drawio; dead-source-link rules),
profile/profile-picture (inline mention), anchor (drop), excerpt/section/
column (unwrap), children/pagetree (placeholder comment), attachments
(list of media links), multimedia/widget (link).

Tests: per-pass unit tests with handcrafted storage XML + a fidelity test
comparing selected v1-converter outputs (build small storage samples, run
OLD converter via `confluence_export.converter` to generate expectations in
the test, assert v2 matches on the agreed subset: headings, lists, code,
panel, status, links, images, emoticons, decision lists). Document any
DELIBERATE divergence in the test.

## pull.py

```python
@dataclass
class PullOptions:
    include_archived: bool = False
    fetch_media: bool = True
    author_lookup: bool = True
    workers: int = 8

def pull(api: ConfluenceAPI, space_key: str, root: Path,
         blobs: BlobStore, prev: Snapshot | None,
         opts: PullOptions) -> Snapshot
```

- Resolve space; list folders + pages (parallel body fetch for pages whose
  `body_storage == ""`); store each body as a blob (`body_blobs`).
- Attachments per page via an 8-worker pool. Downloads go through ONE shared
  worker pool; an `(att_id, version)` already in `prev.attachment_blobs`
  with `blobs.has(digest)` is skipped (incremental).
- Carry forward `derived_blobs` entries from prev (validity is keyed by
  content digest, so they never go stale).
- Best-effort downloads (v1 behavior): warning to stderr; the attachment is
  recorded WITHOUT a blob entry and `attachments_complete=False`.
- Author prefetch: collect unique `version.author_id`s + mention ids found
  cheaply? NO — keep it simple: prefetch authors of pages/attachments in
  parallel; mention ids resolve lazily at build time through a cached
  resolver (cache inside the build run). `users` map saved on the snapshot.
  With `build(api=None)` (offline/--cached), `resolve_user` reads ONLY
  `snapshot.users` and returns "" on a miss — never touches the network.
- `include_archived=True` with `api.returns_archived == False`: warn that
  archived pages cannot be listed in this auth mode; snapshot records
  `include_archived = False` (what was actually fetched — I3 depends on it).
- Save snapshot atomically. Pull does NOT touch the output tree.

## build.py — the heart

```python
@dataclass
class BuildOptions:
    include_html: bool = False
    media: bool = True
    render_drawio: bool = True
    author_lookup: bool = True
    subtree: str | None = None
    no_children: bool = False

@dataclass
class BuildResult:
    written: list[Path]       # absolute paths written/updated this run
    deleted: list[Path]       # paths removed this run (for git staging)
    skipped: int              # unchanged pages
    moved: list[tuple[str, str]]      # (old dir, new dir) relpaths
    warnings: list[str]

def build(root: Path, snapshot: Snapshot, blobs: BlobStore,
          prev: ExportState | None, opts: BuildOptions,
          api: ConfluenceAPI | None = None) -> tuple[BuildResult, ExportState]
```

Algorithm (single linear pass; document each step in the docstring):
1. `plan = plan_layout(...)` over snapshot pages/folders (+subtree filter).
   Bodies come from blobs: `blobs.read_bytes(snapshot.body_blobs[id])` —
   `Page.body_storage` is "" in the snapshot, never pass it to convert.
2. **Fingerprint** per page — EXACTLY these inputs, in this order:
   `sha256(version.number, CONVERTER_VERSION, include_html, media,
   render_drawio, sorted((att_id, att_version, planned_media_name)),
   body_blob_digest, sorted(derived png digests actually used))`.
   The drawio OUTPUT digests are included so a newly-succeeding render
   re-writes the page. `subtree`/`no_children` are scope, NOT content —
   they must NOT enter the fingerprint.
3. **Skip**: prev page with same id, same dir/file, same fingerprint, and
   the .md still exists on disk -> untouched (count into `skipped`); carry
   its PageState forward verbatim.
4. **Move**: same id, different planned dir: write the new dir's artifacts
   FIRST (step 5); only after the new .md landed, `os.rename` a non-empty
   old `.workspace/` to the new dir (collision: keep BOTH, rename incoming
   to `.workspace-from-<old-dir-leaf>`, warn; `EXDEV` -> copytree+rmtree).
   Idempotent: old dir absent / workspace already at target = move done.
   Then delete old generated artifacts (old .md, recorded .html, `.media/`),
   `rmdir` emptied parents (never remove non-empty dirs). Record in `moved`.
   A crash mid-move converges: the next build re-derives the same target.
5. **Write**: render markdown from body blob via convert (+frontmatter);
   write via `.conex/tmp` + `os.replace`. `--include-html` writes the raw
   storage body alongside (recorded in `PageState.html`). Build ONE
   AttachmentNamePlan per page (see MediaRefs) — it names the on-disk
   files AND feeds ctx.media. Materialize `.media/` from blobs; mtime:
   `datetime.fromisoformat(version.created_at)` -> epoch (3.11 handles the
   trailing Z); on parse failure leave mtime unset. (DELIBERATE DIVERGENCE:
   v1's copy2 preserved source mtime; v2 stamps the attachment's version
   time — do not port copy2 semantics.) With `opts.media == False`: do not
   materialize, do not delete existing media, carry prev attachment states.
   If `snapshot.attachments_complete is False`: same preserve semantics —
   never delete an existing `.media/` file this run; only add fetched ones
   (a partial listing must not look authoritative). drawio (when enabled):
   preview-first — use the `.png` sibling attachment when its version
   `created_at` timestamp >= the xml attachment's (timestampS, NOT version
   numbers — those aren't comparable across attachments); else batch-render
   misses ONCE per build via drawio.py into the blob store.
6. **Prune** (after all writes): for each prev page id NOT in plan:
   - I2 zero-pages guard: if the plan is empty and prev wasn't -> skip all
     pruning AND blob GC, warn, return prev state unchanged.
   - I3: skip if prev status archived and not `snapshot.include_archived`.
   - subtree scope: when `opts.subtree` is set, only prune prev pages whose
     recorded `dir` is inside the subtree's PLANNED root dir (from layout).
   - Delete the page's recorded artifacts only (file, recorded html,
     `.media/`); if a non-empty `.workspace/` remains, leave it + warn with
     the path; `rmdir` emptied dirs bottom-up.
   - Then folder dirs: for each prev `state.folders` dir not in the new
     plan, `rmdir` iff empty (non-empty = user content -> leave + warn).
7. Build new ExportState (pages actually on disk: skipped carry-forward +
   written + I2/I3 survivors with their prev entries; `folders` from plan),
   `converter_version = CONVERTER_VERSION`, save ONCE at the end (I6).
   State describes the TREE, not git — build saves it; a git failure after
   a good build is fine (the next commit picks the delta up).
8. Blob GC, only on a non-guarded run: `keep` = all `body_blobs` ∪
   `attachment_blobs` ∪ `derived_blobs` values of the CURRENT snapshot
   ∪ every blob digest in the NEW state (incl. I2/I3-preserved carry-over
   attachment states). Never delete a digest the current snapshot
   references. `blobs.gc(keep)` runs last.

Crash-safety argument (state in docstring): every artifact lands via
os.replace; state saves last; a crash anywhere re-runs to the same result.

## gitio.py — thin

```python
def ensure_repo(root: Path) -> bool          # init; set fallback
    # user.name/email ONLY inside the fresh-init branch (never clobber an
    # existing repo's identity — v1 behavior); ensure .gitignore has .conex/
def commit_user_changes(root: Path) -> bool  # PORT v1: stage TRACKED
    # modifications only (git add -u), then UNSTAGE any .conex/ paths
    # (PORT v1 _unstage_secret_configs — covers a force-added .conex);
    # commit "Local changes before export"; True if committed
def commit_export(root: Path, result: BuildResult, message: str) -> bool
    # git add -- <written, chunked>; git add -- <deleted, chunked> (records
    # deletions); commit; True if a commit was created (empty delta -> no
    # commit). Failures raise GitError; cli degrades to warning (v1 parity:
    # missing git binary -> warn and continue).
```

No ls-files scans, no prune logic, no restore logic, no fold logic. ~120
lines target.

## drawio.py — **PORT knowledge** from v1 `drawio.py` + backlog items 10/11

`DRAWIO_RENDER_VERSION = 1` (keys derived blobs; bump when render params
change). `find_drawio_pairs(attachments)`; preview-freshness by
`version.created_at` TIMESTAMP (not version numbers).
`render_batch(xml_blobs: dict[name, digest], blobs) -> dict[name, digest]`
under `.conex/tmp`: the REAL CLI invocation is
`drawio --export --format png --no-sandbox --output <out> <in>` (long
flags; `--no-sandbox` is load-bearing headless — PORT v1 drawio.py).
Folder input is UNVERIFIED: attempt one folder-mode invocation; if it
fails or produces nothing, fall back to a per-file loop with the same
flags. `shutil.which` cached; absent CLI -> return {} with one warning.
Never hand-roll a renderer.

## cli.py

Same command surface as v1 (`conex` entry point; v1's `confluence-export`
remains on the old package):
`configure`, `spaces`, `tree SPACE`, `find SPACE QUERY`,
`export SPACE -o DIR [--path P] [--no-children] [--include-archived]
[--cached] [--include-html] [--no-media] [--no-drawio-render] [--no-git]
[--no-author-lookup] [-v]`, `refresh SPACE -o DIR`, `diff SPACE -o DIR`.

`export` flow: resolve config -> preflight banner (config source, auth
mode, API mode, site, output dir) -> `ExportLock` -> clear `.conex/tmp`
(EXACTLY ONCE per locked command, here and only here — stores never clear
it; refresh/diff do the same after taking the lock)
-> (pull unless `--cached`; `--cached` with no snapshot = error) ->
`commit_user_changes` (if git) -> build -> `commit_export` -> summary line
(written/skipped/moved/pruned counts + warnings recap; exit 0 with warnings,
v1 parity). `diff`: pull to a snapshot (no lock on tree needed? it writes
snapshot/blobs -> takes the lock) then report add/change/move/delete vs
state, read-only for the tree. `refresh`: pull only.

## pyproject (wave 4, orchestrator does this)

Add `conex = "conex.cli:main"` script; add package `src/conex` to wheel;
add `pydantic>=2.7` dep; testpaths gains `tests_v2`.

## e2e tests (wave 4) — FakeConfluenceAPI

In-memory `ConfluenceAPI` impl with mutable space content. Scenarios (each
its own test): first export; idempotent re-run (zero writes, no commit);
title change -> move with `.workspace` carry; reparent -> move; upstream
delete -> prune + workspace left behind + note; archived: include then
plain re-run preserves `_archived` (I3); zero-pages guard (I2); lock
contention (I5); `--no-media` preserves media; attachment update
re-downloads; drawio preview-first (fake CLI absent); crash simulation:
build interrupted (monkeypatch to raise mid-walk) -> rerun converges, no
state corruption; `--cached` build offline; subtree export doesn't prune
outside scope; git log shape (user-changes commit before export commit).
