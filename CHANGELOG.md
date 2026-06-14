# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-06-14

First tagged release. `conex` is the second-generation, ground-up rewrite of the
Confluence exporter. It ships as the `conex` CLI; the original
`confluence-export` CLI is still bundled and unchanged for now.

### Added
- **`conex` CLI** — a complete rewrite under `src/conex/`. Strictly read-only
  against Confluence (HTTP GET only) and never pushes git.
- Typed, null-tolerant API boundary with dialect adapters (Cloud v2, gateway,
  cookie v1).
- `pull` → snapshot (pure fetch) / `build` → deterministic tree, split over a
  content-addressed blob store with `.conex/state.json` + `snapshot.json`.
- Folder hierarchy support on the v2 API (discovered from the page set), an
  exclusive run lock, crash-durable atomic writes (fsync), and a blob GC.
- draw.io diagrams that conex renders itself (no fresh Confluence preview) are
  rendered via the draw.io desktop CLI at a **readability-driven scale**:
  `scale = clamp(round(14 / smallest_font_px), 1, 3)`, hard-capped so output
  stays under ~12k px (draw.io's blank-PNG threshold). Tiny-font diagrams come
  out legible; normal ones stay 1x. The render cache is keyed by render version,
  and stale renders are reclaimed by GC.
- `conex --version`.

### Notes
- The distribution is now named `conex` (was `confluence-export`); both CLIs are
  installed from it.
- Output layout roots at a `<Space-Name>/` directory (v1 rooted at the
  homepage). Re-exporting a v1 tree relocates files — start v2 in a fresh
  directory to preserve `git log --follow` history. See `SPEC-V2.md`.
- Real-API coverage is still early. Known gaps tracked for follow-ups: a
  large-deletion safety valve for truncated listings, an explicit page-status
  filter (drafts/trashed), and folder reconstruction under cookie/v1 auth
  (currently warns instead).
