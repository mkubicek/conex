# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-06-14

First tagged release. `conex` is the second-generation, ground-up rewrite of the
Confluence exporter. The distribution is now named **`conex`** and installs the
`conex` CLI; the original `confluence-export` CLI is still bundled and unchanged.

Validated against a live Confluence Cloud portfolio (~16 spaces, ~3,500-page
spaces, ~61k files / 25 GB): **~7× faster** than v1, **exact page parity**, **zero
prose lost**, **zero broken media links**, clean exit on ~1,500 upstream 404s.

### Added
- **`conex` CLI** — complete rewrite under `src/conex/`. Strictly **read-only**
  against Confluence (HTTP GET only) and **never pushes git** (local commits only).
- Typed, null-tolerant API boundary with dialect adapters (Cloud v2, gateway,
  cookie v1).
- **`pull` → snapshot** (pure fetch) / **`build` → deterministic tree**, split
  over a content-addressed blob store (`.conex/state.json` + `snapshot.json` +
  blobs). Fingerprint-based incremental skip; idempotent re-runs.
- Folder hierarchy on the v2 API (discovered from the page set via `/folders/{id}`,
  cycle-safe), an exclusive run lock, and a blob GC.
- **draw.io**: diagrams conex renders itself are exported via the draw.io desktop
  CLI at a readability-driven scale `clamp(round(14 / smallest_font_px), 1, 3)`,
  hard-capped under ~12k px (draw.io's blank-PNG threshold); render cache keyed by
  render version, with stale renders reclaimed by GC.
- Resolves Confluence's `viewfile`/`viewppt`/attachments dynamic-content macros to
  real `.media/` links (v1 left dead placeholders).
- `conex --version`; `--allow-mass-delete` (see the safety valve below).

### Security & data safety
- Crash-durable atomic writes (`fsync` of file + parent dir around `os.replace`).
- Deletion containment: every delete is asserted inside the export root; symlink
  guards on `.conex`, page dirs, and `.media`; space-identity guard.
- **Large-deletion safety valve**: refuses to prune more than half of a
  non-trivial prior export in one run (e.g. a truncated API listing) unless
  `--allow-mass-delete` is passed.
- **Credential safety**: attachment download URLs must be same-origin https (an
  API-controlled foreign URL can no longer exfiltrate the token/cookie); a local
  `.conex` config cannot redirect a scoped token to another tenant; download size
  cap; folder-listing transient errors abort rather than silently restructure.
- Conversion hardening: user-controlled URLs (macros, `ri:url`) are scheme-
  allowlisted; page titles are sanitised before the H1 (no markdown/HTML injection).

### Fidelity & behaviour vs v1
- Conversion output is byte-identical to v1 for the large majority of pages;
  remaining diffs are cosmetic (whitespace, drawio image naming).
- Editor-cruft attachments (`~$*.xlsx`, `~*.drawio.tmp`) are filtered from the
  media tree, links, and frontmatter.
- Archived pages are **excluded by default** (v1 parity); `--include-archived`
  opts in under `_archived/`, and a prior archived export is preserved.
- Output layout roots at a `<Space-Name>/` directory (v1 rooted at the homepage).
  **Migration:** re-exporting a v1 tree relocates files one level down — start v2
  in a fresh directory to preserve `git log --follow` history. See `SPEC-V2.md`.
- `.media/.versions.json` per-page manifests are no longer written (state moved to
  `.conex/`).

### Known limitations
- Folder reconstruction under cookie/v1 auth is not implemented — that dialect
  warns that folder-parented pages flatten to the space root (use an API token
  for full hierarchy).
- The truncated-listing protection is the build-side mass-delete valve; pull does
  not yet emit an earlier truncation warning.
- The native `requests` socket transport has limited live mileage (the live runs
  routed bytes through a curl-backed adapter); everything above transport ran
  unmodified.

### Tests
2,035 passing (1,095 `tests_v2/` for conex + 940 `tests/` for the bundled
confluence-export); ~91% line coverage on `src/conex`.
