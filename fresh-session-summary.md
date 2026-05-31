# Fresh Session Summary

## Context

We are reviewing work around supporting moved Confluence pages or page trees during export, especially where exported Markdown pages have related sidecar directories such as `.workspace`, `.media`, `.conex`, and Git-tracked state.

The immediate review found that PR24 appears sound, but PR25 introduced correctness and scalability concerns:

- Relocated untracked `.workspace` content can inherit tracking from a deleted page path, leaving user workspace files exposed to later commits.
- The case-sensitivity probe uses a deterministic filename in the export root and can overwrite or delete a user file.
- Markdown discovery filters ignored sidecar directories only after recursive traversal has already descended into them, making full exports scale with `.media`, `.workspace`, `.conex`, or `.git` size.

## Architectural Concern

The feature request should be challenged holistically before continuing with local fixes. The question is whether supporting moved pages or moved page trees is actually feasible and worth supporting in this architecture, or whether the project should consciously decide not to support that situation.

If moved pages or moved page trees create too many ambiguous ownership and tracking cases, the better product behavior may be:

- Detect the situation.
- Refuse or pause the export before destructive or confusing changes happen.
- Warn the user clearly that moved pages or page trees are not supported safely.
- Ask the user to resolve the move manually, reset the export, or use a documented migration path.

## Decision Point

Now is the right time to reflect on whether the current path is internally coherent. If a clean architecture exists that supports moved pages or page trees without workarounds, without a growing pile of edge-case fixes, and with consistent Git and sidecar ownership semantics, then it may be worth implementing.

If no such architecture exists, we should stop treating this as a bug-fix exercise. The project should explicitly choose not to support this feature and instead implement detection plus a user-facing warning for moved pages or moved page trees.

## Suggested Next Session Goal

Do not start by patching the three PR25 issues in isolation. First, evaluate whether the moved-page / moved-page-tree feature can be represented with a simple, consistent model:

- What is the source of truth for page identity after a path changes?
- Which files and sidecar directories move with a page?
- How is Git tracking preserved, cleared, or intentionally quarantined?
- How are untracked user workspace files protected?
- Can discovery avoid traversing ignored sidecar trees by construction?
- Can filesystem behavior checks be isolated from user-visible paths?

Only implement the feature if the answers form a coherent architecture. Otherwise, implement safe detection and warning behavior instead.
