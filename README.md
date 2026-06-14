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

Before export, the tool prints the resolved config source, auth mode, API mode, site URL, output directory, and preflight checks. If auth, gateway routing, space resolution, page listing, attachment listing, or output writability fails, export stops before writing output.

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

Attachment downloads are best-effort: a transient network failure (timeout, dropped connection) or a `404` for an upstream-deleted attachment prints a `Warning:` to stderr and leaves the previously committed copy in place — the export still exits `0`. Check stderr after bulk runs if you need attachment completeness.

The **first** export of a pre-existing export re-downloads its attachments once, to upgrade the attachment manifest (`.media/.versions.json`) to the current format. Transient warnings during that one-time pass are expected; subsequent exports skip unchanged attachments. (conex re-downloads rather than trusting an older manifest that can't prove a file is still the current attachment.)

## Git Versioning

Exports are automatically versioned with git. After each export, only Confluence-sourced files are committed. If you've made local edits to previously exported files, those are captured in a separate commit first. Locally created files are never auto-committed.

Git versioning is enabled by default and requires git to be installed. If git is not available, the export proceeds normally with a warning. Disable with `--no-git`.
