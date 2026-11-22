# conex

[![CI](https://github.com/mkubicek/conex/actions/workflows/ci.yml/badge.svg)](https://github.com/mkubicek/conex/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/mkubicek/conex/branch/main/graph/badge.svg)](https://codecov.io/gh/mkubicek/conex)

Export Confluence Cloud spaces as LLM-ready markdown — fast, read-only, and
git-versioned.

```bash
conex export SPACEKEY -o ./output
```

```
output/
└── Space-Name/
    ├── Space-Name.md
    ├── .workspace/
    ├── Page-A/
    │   ├── Page-A.md
    │   ├── .workspace/
    │   ├── Child-Page/
    │   │   ├── Child-Page.md
    │   │   ├── .workspace/
    │   │   └── .media/
    │   │       ├── diagram.drawio
    │   │       ├── diagram.drawio.png
    │   │       └── screenshot.png
    │   └── Another-Child/
    │       └── Another-Child.md
    └── Page-B/
        ├── Page-B.md
        └── .media/
            └── report.pdf
```

Pages become markdown files. Attachments land in `.media/` folders next to their page. The folder hierarchy mirrors the Confluence page tree.

## Workspace

Each page directory includes a `.workspace/` folder where you can store preparation files (scripts, notes, aggregation data) that are useful when working with the exported content but should not go to Confluence. Workspace files persist across re-exports.

```bash
# Example: a script that summarizes a page's attachments
output/Space-Name/Page-A/.workspace/summarize.py

# Example: notes for an AI coding agent session preparing content
output/Space-Name/Page-A/.workspace/draft-notes.md
```

Both `.workspace/` and `.media/` use a dot-prefix to avoid name collisions with Confluence pages. Since each page title becomes a directory name, a page titled "workspace" or "media" would clash with a non-prefixed directory. The dot-prefix is safe because page titles are sanitized to only contain word characters, spaces, and hyphens, so no page can produce a directory name starting with a dot.

### When a page is moved or renamed in Confluence

A page's on-disk path follows its position in the Confluence tree, so reparenting or renaming a page changes where it is exported. On the next full export, conex rewrites the page's markdown at its new path (git's rename detection keeps `git log --follow` history across the move) and re-downloads its `.media/` there.

Your `.workspace/` prep files are **deliberately not moved automatically.** Auto-carrying them would mean reconciling the filesystem against git's index on every export to serve a rare event — a fragile trade conex does not make. Instead, when a page with workspace content moves, conex leaves the `.workspace/` at the old path untouched and prints a note telling you the new location, e.g.:

```
Note: "Page-A" moved to 'New-Parent/Page-A'; your prep files at
'Old-Parent/Page-A/.workspace' do not move automatically — relocate them if
you still need them.
```

Move the folder yourself if you still want those files. (An empty, auto-created `.workspace/` carries nothing and is cleaned up silently.)

One caveat for `--no-media`: a normal export re-downloads a moved page's `.media/` at its new path, but `--no-media` does not. So if a page moves during a `--no-media` export, its cached attachments are dropped (conex prints a note); re-run a full export with media to restore them.

## Install

```bash
uv pip install -e .   # or: pip install -e .
```

## Platform support

**macOS is the only currently supported platform** — it's what conex is
developed and tested on. On macOS (APFS), a materialized attachment and its
content-addressed cache entry share storage via copy-on-write clones, so the
two copies cost ~1x on disk instead of 2x.

**Linux** is expected to work — the run-lock (`fcntl.flock`) and copy-on-write
dedup (`FICLONE` on btrfs/XFS/ZFS) are native there — but is currently untested
and unsupported. On a filesystem without reflink (e.g. ext4) attachment storage
falls back to a plain copy (~2x); correctness is unaffected.

**Windows is not supported.** The blockers below are tracked so support is
possible later — none is fundamental, but all must be addressed:

- **Run-lock uses `fcntl`** (`src/conex/store/lock.py`). The import runs at
  module load and `conex.store` imports the lock unconditionally, so
  `import conex.store` fails outright on Windows. A port needs a guarded import
  plus `msvcrt.locking` (or a portable `filelock`).
- **Reserved filenames.** Name sanitization (`src/conex/paths.py`) does not
  reject Windows device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
  `LPT1`–`LPT9`) or strip trailing dots/spaces, which Windows forbids or
  silently rewrites — either would corrupt the on-disk layout and the
  `state.json` path mapping.
- **`MAX_PATH` (260 chars).** Filenames are capped at 100 chars, but a deep page
  tree can still produce a full path over Windows' default limit; a port should
  opt into long paths (the `\\?\` prefix / a manifest opt-in).
- **No reflink on NTFS.** Copy-on-write dedup works only on ReFS / Windows 11
  Dev Drives; on default NTFS attachment storage falls back to a full copy (no
  on-disk dedup).
- **Directory fsync.** Atomic-write durability fsyncs the containing directory
  (`src/conex/paths.py`), which behaves differently on Windows; it degrades
  silently today but the durability story would need revisiting.

## Setup

```bash
conex configure
```

Stores your site URL and credentials to `~/.config/confluence-export/config.json`. You can also use env vars (`CONFLUENCE_SITE_URL`, `CONFLUENCE_BASE_URL`, `CONFLUENCE_API_BASE_URL`, `CONFLUENCE_CLOUD_ID`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`, `CONFLUENCE_PAT`, `CONFLUENCE_COOKIE`, `CONFLUENCE_AUTH_TYPE`) or CLI flags (`--site-url`, `--base-url`, `--api-base-url`, `--cloud-id`, `--email`, `--api-token`, `--cookie`, `--auth-type`, `-v`/`--verbose`).

Use a local config when an export directory should override the global default:

```bash
conex configure --local ./docs
```

Local configs are discovered from the output directory upward as `.conex/config.json`. Export git commits never stage files under `.conex/`.

If you don't have an API token, you can authenticate with a browser cookie instead. Copy the `Cookie` header from DevTools (F12 > Network tab > any `/wiki/` request) and pass it with `--cookie`:

```bash
conex --base-url https://example.atlassian.net --cookie 'tenant.session.token=...' export SPACEKEY -o ./output
```

Cookie authentication uses Confluence's legacy REST read endpoints because Confluence Cloud REST v2 rejects browser session cookies. Use the normal site URL (`https://example.atlassian.net`), not the OAuth gateway URL. Cookie auth is an explicit mode and reports `API mode: Confluence REST v1 compatibility`.

## Required permissions

Basic Auth (email + API token) and unscoped Personal Access Tokens use the underlying user's full permissions — no further setup needed. If you provision a [scoped API token](https://developer.atlassian.com/cloud/confluence/scopes-for-oauth-2-3LO-and-forge-apps/) or use OAuth 2.0 / Forge, grant these five granular read scopes:

```
read:space:confluence
read:page:confluence
read:folder:confluence
read:attachment:confluence
read:user:confluence
```

`read:user:confluence` is only needed to render `@mentions` and `profile` macros with display names instead of opaque Atlassian account IDs in the exported markdown. If your token can't grant it, pass `--no-author-lookup` to skip user resolution; mentions fall back to the raw account ID. The other four scopes are required.

For classic OAuth 2.0 (3LO) tokens, the equivalent set is `read:confluence-space.summary`, `read:confluence-content.all`, `readonly:content.attachment:confluence`, and `read:confluence-user`.

Scoped tokens must be addressed via the OAuth gateway URL (`https://api.atlassian.com/ex/confluence/{cloudId}/...`) rather than the site URL. The tool detects this automatically: it keeps the saved `site_url` as the user-facing Atlassian URL, resolves or uses the cached `cloud_id`, and derives the gateway `api_base_url` at runtime. No manual configuration needed in the common case; if cloud ID lookup is blocked, provide `--cloud-id` or `--api-base-url`.

Before export, the tool prints the resolved config source, auth mode, API mode, site URL, output directory, and preflight checks. If auth, gateway routing, space resolution, space-wide page listing, or output writability fails, export stops before writing output.

In non-interactive runs, commands never prompt for credentials. Run `conex configure` or pass explicit flags/env vars before invoking export from automation.

## Commands

```bash
conex spaces                              # list accessible spaces
conex tree SPACEKEY                       # show page hierarchy
conex find SPACEKEY "query"               # search pages by title
conex export SPACEKEY -o ./output         # export full space (refreshes from Confluence)
conex export SPACEKEY --path /Sub/Tree    # export a subtree
conex export SPACEKEY --no-children       # export a single page only
conex export SPACEKEY --include-archived  # also export archived pages (under _archived/)
conex export SPACEKEY --cached            # reuse the last cache, skip the refresh
conex export SPACEKEY --include-html      # save raw HTML next to the markdown
conex export SPACEKEY --no-media          # skip attachments
conex export SPACEKEY --no-drawio-render  # skip draw.io -> PNG rendering
conex export SPACEKEY --no-git            # skip git versioning
conex export SPACEKEY --no-author-lookup  # skip Confluence user lookup
conex diff SPACEKEY ./output              # compare export vs. live (read-only)
conex diff SPACEKEY ./output --path /Sub  # diff a subtree
conex refresh SPACEKEY                    # force-refresh cache
```

By default, `export` and `diff` **refresh from Confluence**. Pass `--cached` to `export` (or run `refresh` first) to reuse the last fetched cache without hitting the API.

Archived pages are skipped by default. `--include-archived` (on both `export` and `diff`) exports them under an `_archived/` subtree. A normal export that omits archived pages **preserves** a prior `--include-archived` export's `_archived/` content rather than pruning it.

### Attachments

By default, an attachment failure prints a warning, preserves any last-good copy and lets the export finish with exit code `0`. Re-run a live export to retry missing content.

### Strict completeness and JSON reports

```bash
conex export SPACEKEY -o ./output --strict --report ./runs/export.json
```

Use either flag independently. `--strict` finishes the build and returns **3** if requested page or attachment content is missing, stale or unverified. `--report PATH` atomically replaces a UTF-8 JSON file on each run and creates its parent directory if needed. A path beside the export works for both local runs and CI.

| Outcome | Default exit | Strict exit | Report status |
|---|---:|---:|---|
| Requested content exported | 0 | 0 | `complete` |
| Build finished with a content failure | 0 | 3 | `partial` |
| Export aborted, e.g. authentication or lock failure | 1 | 1 | `failed` |

Version 1 reports contain `space_key`, the requested `scope`, build `counts` (files written and pages skipped, moved and pruned), and a sorted `issues` array. A failed export has null counts and an error type. A report destination error exits 1. For concurrent runs, use a separate report path per invocation.

An attachment download failure identifies both the page and attachment. A listing failure identifies the page and leaves `attachment_id` null because the missing attachment count is unknown.

```json
{"code": "attachment_download", "page_id": "p1", "attachment_id": "a1"}
```

Completeness follows the selected subtree and `--no-children`. With `--no-media`, attachment failures are outside the requested scope. Page-body fetch failures and pages carried forward after an unsuccessful verification still count. Optional author names, Git commits and availability of the draw.io renderer do not affect content status. File checks verify presence and recorded versions without hashing user-edited output. A cached run assesses the saved snapshot. For an older snapshot with warnings that cannot be attributed to specific content, the report records `legacy_snapshot_unverified`; a live retry refreshes that evidence.

### Credential-free fixture demo

From the repository root, run the real export command against an in-memory space with one runbook and one attachment. The fixture adapter uses local data throughout. Use a fresh directory so an earlier successful download cannot satisfy the simulated outage.

```bash
demo_dir=$(mktemp -d)
uv run python -m examples.export_fixture --fail-download DEMO -o "$demo_dir/out" --report "$demo_dir/default.json"
uv run python -m examples.export_fixture --fail-download DEMO -o "$demo_dir/out" --strict --report "$demo_dir/strict.json"
echo "$?"     # 3
uv run python -m examples.export_fixture DEMO -o "$demo_dir/out" --strict --report "$demo_dir/retry.json"
cat "$demo_dir/retry.json"
```

The first two reports both identify attachment `a1` on page `p1`. The first command exits 0. The strict retry after recovery exits 0, writes `checklist.txt` into the runbook’s `.media/` folder, and reports `complete` with an empty issue list.

## Git Versioning

Exports are automatically versioned with git. After each export, only Confluence-sourced files are committed. If you've made local edits to previously exported files, those are captured in a separate commit first. Locally created files are never auto-committed.

Git versioning is enabled by default and requires git to be installed. If git is not available, the export proceeds normally with a warning. Disable with `--no-git`.
