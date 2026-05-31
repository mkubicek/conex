# PR-24 / PR-25 — Learnings + a holistic "are we on the right path?" reflection

> **How to use this doc.** It is written to be fed cold into a fresh session. It
> summarizes what was built for issues **#11** (collisions) and **#17** (moved /
> reparented pages), what every review round has found, *why* the findings keep
> coming, and — most importantly — asks whether the #17 feature is worth building
> the way we are building it, or at all. Read the "Holistic challenge" and
> "Architectural options" sections first; the rest is supporting evidence.
>
> **Date:** 2026-05-30. **Author of record:** working notes from the implementation
> + 4 internal review rounds + Codex external review.

---

## 1. What we set out to do

Two issues, one root cause (the on-disk layout was computed inline during the
write walk):

- **#11 — collisions.** Distinct page titles that sanitize to the same name
  silently overwrote each other (`mkdir(exist_ok)` + `write_text`); the second
  sibling won and the first's children merged into the wrong subtree.
- **#17 — moved pages.** When a page is reparented/renamed, its old on-disk path
  (markdown, dir, and the user's `.workspace/`) is orphaned. Goal: make a move
  **seamless** — the page follows to its new path, git history continues, and the
  user's `.workspace` (+ `.media`) ride along — with **zero user action** and
  **auto-healing of legacy exports** (no migration command).

Delivered as **two stacked PRs**:

| PR | Branch | Base | Commit | Scope | Status |
|----|--------|------|--------|-------|--------|
| **#24** | `feature/collision-allocator` | `main` | `29ce51b` | #11 collision-safe layout planner (pure, no I/O, runs in all modes) | **Sound** (see §4) |
| **#25** | `feature/move-reconcile` | `feature/collision-allocator` | `77be358` | #17 leaf-sidecar reconciler (relocate `.workspace`/`.media`, drop stale md, heal orphans, prune) | **Treadmill** (see §5) |

Tests at time of writing: **391 passing.**

---

## 2. The architecture as built (PR-25)

Stateless **plan → reconcile-before-write → write**, with **no persistent state
file**. The insight (validated): every `.md` carries `page_id` frontmatter, so the
**on-disk markdown tree *is* the manifest**; `diff.scan_export_dir` turns it into a
`page_id → path` map. Crash safety is idempotent recompute, not state repair.

The PR-25 reconciler, specifically:

1. Markdown is **disposable** — regenerated fresh at the planned path every full
   export. Git's content-similarity rename detection makes `git log --follow`
   cross the move from a plain **delete + add** (verified R100). **No `git mv`.**
2. Only the user's **`.workspace`** (and `.media`, to avoid re-download) are
   non-regenerable, so only *they* are physically relocated, as **leaf sidecars**:
   heal duplicates → **park** sidecars to `.conex/.relocate/<id>` → **place** at the
   new target (crash-adoptable) → drop stale markdown → rmdir-only prune.

This is genuinely elegant *in concept*. The trouble is entirely at the boundary
where it meets git and the filesystem (see §6).

---

## 3. Review history — the evidence

Issue #17's reconciler has been through **four review rounds**, each of which
found *new* real defects in the *previous* round's fixes:

| Round | Source | Real findings (sev) | Theme |
|-------|--------|---------------------|-------|
| 1 | internal design+verify | 5 (A–E): git-stage relocated sidecars (P1), case-only churn, at-target canonical, orphan quarantine, archived `_archived` divergence | git/FS reconciliation |
| 2 | internal verify | 3: `.media` loses tracking on move (P2), non-unique quarantine clobber (P3), file-occupant crash (P3) | git/FS reconciliation |
| 3 | internal verify (of round-2 fixes) | 3: `core.ignorecase` drift both directions (2×P2), new-path exemption strands a quarantined occupant (P3) | git/FS reconciliation |
| 4 | **Codex external** | 3: untracked-`.workspace`-onto-tracked-occupant gets committed (P2), case-probe clobbers a user file (P2), diff scan descends ignored trees (P2) | git/FS reconciliation + perf |

**~14 distinct, individually-correct fixes across 4 rounds, and round 4 still found
3 more.** Every wave clusters on the same seam. That is the signal.

---

## 4. PR-24 (#11) learnings — keep it, it's sound

- The **per-parent, casefold-aware collision allocator** that produces **one**
  segment used for *both* the directory leaf and the `.md` stem is correct and
  robust. Dir/stem can no longer desync.
- Stability comes from a total order `(position, id)` — `id` is a stable
  Confluence string, so suffix assignment (`-2`/`-3`) is reproducible across runs
  and machines → **zero steady-state churn**.
- It is a **pure function, runs in all export modes**, and has **no dependency on
  #17**. Codex: "PR24 appears sound."

**Learning: #11 is independent and should not be held hostage to the #17
architecture debate.** PR-24 can land on its own merits regardless of what we
decide about #17. The "stage A then B" decision was right; **B is where the cost
lives.**

---

## 5. PR-25 (#17) learnings — what we actually learned

1. **Markdown moves are free.** Plain delete+add + git rename detection already
   gives history continuity. The reconciler adds *nothing* for markdown. All the
   complexity is for carrying **`.workspace`/`.media`**.
2. **Disposability hierarchy:** markdown (regenerated) > `.media` (re-downloadable)
   > **`.workspace` (irreplaceable)**. *Only `.workspace` genuinely must be
   carried.* Everything else is convenience.
3. **`git config core.ignorecase` is not a reliable oracle.** It is fixed at
   init/clone time and drifts when a repo crosses filesystems (a Linux/CI clone
   opened on a Mac carries `ignorecase=false`). We had to switch to a **live FS
   probe** — and Codex then found the probe itself clobbers a user file.
4. **The "no-auto-track" model for `.workspace`** (never start tracking what the
   user chose not to commit) is a hard constraint that interacts badly with
   relocation: moving an untracked sidecar onto a path a deleted page *had* tracked
   produces a tracked-but-missing entry that the next safety commit can fill with
   the mover's private content (Codex P2 #1).
5. **`_remove_stale_files` blanket-skips `.workspace`**, so a quarantined/relocated
   workspace occupant is never pruned → index drift that has to be patched
   elsewhere.
6. **Every fix narrows one cell of a combinatorial table** — {tracked, untracked} ×
   {case-sensitive, insensitive} × {empty, dir-occupant, file-occupant,
   tracked-occupant} × {media, --no-media} × {crash-mid-move} — and the table is
   the problem, not any single cell.

---

## 6. First-principles diagnosis — *why* the findings never stop

The export directory layout is **derived from the (mutable) page tree**. A page's
sidecar **path is therefore a function of the page's position in the tree.** When
the tree changes, the derived path changes, and anything stored there must be
**physically relocated**.

The reconciler relocates with `shutil` — operations **git cannot observe** — and
then **retroactively patches git's index** to match. So we are perpetually
re-synchronizing **three views that do not naturally agree**:

1. the **filesystem** (where bytes physically are),
2. **git's index** (what is tracked, under which path *and case*),
3. the **desired plan** (where things should be).

Two-(really three-)sources-of-truth that must be manually reconciled, **on every
export**, to handle an event (a reparent) that is **rare**. That mismatch is the
generator of the edge-case stream. The fixes are correct; the *substrate* is
fighting us. **This is the classic "workaround orgy" the original plan explicitly
set out to avoid** — we avoided it for the *manifest* (frontmatter, no state file)
but reintroduced it at the *git/FS relocation boundary*.

**Sole reason the sidecar path is tree-derived:** we chose to co-locate
`X/.workspace` next to `X/X.md` for discoverability. That co-location is the *only*
thing forcing sidecars to move when the tree changes.

---

## 7. The holistic challenge (the actual decision)

Before writing fix #15, answer these honestly:

- **Is auto-carrying `.workspace` across a rare reparent worth *any* ongoing
  machinery** that runs on every export and has shown a four-round, still-open
  edge-case stream?
- **What does the user actually lose** if, on a move, `.workspace` does *not*
  auto-follow and we simply tell them? Markdown + history still follow for free.
  `.media` re-downloads. The only loss is they manually `mv` a folder they created
  — for an event that happens occasionally.
- **Is there a design where a move is a *non-event*** for the only data that can't
  be recomputed — no relocation, no parking, no quarantine, no case-at-move, no
  tracked/untracked-at-move? (Yes — option C below.)

The original directive was: *prefer an architectural change if it yields a clean
architecture instead of a workaround orgy that surfaces more issues later.* By that
own standard, **the current #17 path (option A) has become the workaround orgy.**

---

## 8. Architectural options

### Option A — Keep the sidecar-relocation reconciler (status quo)
Relocate `.workspace`/`.media` on move; patch git afterward.
- ✅ Sidecars stay visually adjacent to the page markdown (best discoverability).
- ✅ Already built; 391 tests green.
- ❌ Structurally fights git: 3-way reconciliation re-run every export.
- ❌ **4 review rounds, ~14 fixes, still 3 open P2s.** No evidence the stream has a
  bottom. Each new dimension (a new FS, a new flag) reopens it.
- ❌ Runs heavy machinery on every export to serve a rare event.

### Option B — Detect-and-warn (consciously *do not* auto-carry)
On move detection, do **not** relocate sidecars. Emit a clear one-line warning:
`page "X" moved A → B; your prep files at A/.workspace/ won't follow automatically —
move them if you still need them.` Markdown moves + history continuity happen for
free via git; `.media` re-downloads at the new path.
- ✅ **Smallest possible bug surface** — essentially none. No park/place/quarantine/
  case/tracking machinery at all.
- ✅ Honest and predictable; the user stays in control of their own files.
- ✅ Ships #11 now; reduces #17 to "write markdown at the right path (git handles
  the rename) + warn about sidecars + prune empties."
- ❌ Not "seamless" — the user does a rare manual `mv`.
- ❌ A stale `A/.workspace/` lingers until the user acts (mitigate: warn + optional
  `--heal` that just deletes/relocates on explicit request).

### Option C — Move the sidecar *storage* out of the page tree (page_id-keyed)
Store sidecars at a **stable, page_id-keyed location** (e.g.
`.conexsidecars/<page_id>/workspace/` and `.../media/`), **not** under the page's
tree path. Markdown stays tree-derived and disposable; sidecars are addressed by
the **stable key** and therefore **never move** when the tree changes.
- ✅ **A reparent is a non-event for sidecars.** Deletes the entire move/park/
  quarantine/case-at-move/tracked-at-move machinery — the whole §6 mismatch is gone
  because the sidecar path no longer depends on the tree.
- ✅ Holistically internally consistent: one stable address per page_id; git tracks
  it normally with no rename-on-move; #11 collisions can't touch it (page_id is
  unique).
- ✅ The reconciler shrinks to: "drop stale markdown (git already does) + prune."
- ❌ **UX cost:** prep files no longer sit next to the page's markdown; `<page_id>`
  is opaque. Mitigations (a generated index, or a symlink from `X/.workspace` →
  store) *reintroduce* tree-coupling and should be resisted, or kept read-only.
- ❌ Still a **rewrite** of PR-25 + a **one-time legacy migration**
  (`X/.workspace` → store) — but a migration with *no path arithmetic at move time*.
- ⚠️ Decide up front whether the store is git-tracked by default (probably yes for
  `.workspace`, no/optional for `.media`).

---

## 9. What's settled, and the open decision

**Settled (evidence-backed, not taste — treat these as conclusions):**

1. **Decouple and land #11.** PR-24 is sound and independent. Merge it on its own
   merits; do not let the #17 debate block it.
2. **Abandon Option A.** The four-round trend is the data: it is the wrong substrate
   for "seamless." Do **not** write the next fix on top of it.

**Open — decide this session, from scratch, B vs C:** the choice turns on a single
product judgement — **how much is genuinely-seamless auto-carry of `.workspace`
actually worth?** Both are internally consistent; they trade different things:

- **Option B (detect-and-warn)** buys the **smallest possible surface** and ships
  now, at the cost of not being seamless (the user does a rare manual `mv`).
- **Option C (page_id-keyed store)** buys **true seamlessness with no edge-case
  treadmill**, at the cost of a PR-25 rewrite + one-time legacy migration + sidecars
  no longer adjacent to their markdown (discoverability).

Weigh them fresh against the product's actual users and how often pages get
reparented — don't anchor on this doc's ordering. **Whichever way, write down the
conscious decision** (including, if B, that we *deliberately* do not auto-carry and
warn instead) so it isn't relitigated as a "bug" later.

The meta-point: **we are *not* on the right path with Option A.** The feature is
feasible; only B or C is internally consistent. A is a permanent maintenance tax.
Which of B or C is right is a deliberate product call, not a foregone one.

---

## DECISION (2026-05-30) — Land #11, take **Option B** for #17

After grounding the options in the actual code, the call is:

1. **#11 / PR-24 lands on its own merits, decoupled.** It is a pure function with
   no I/O and no dependency on the #17 machinery. Nothing in the move debate
   touches it.
2. **#17 is rebuilt as Option B (detect-and-warn).** *We deliberately do NOT
   auto-carry `.workspace` across a move.* On a detected move conex drops the
   disposable artifacts (markdown — git records the rename for free; `.media` —
   re-downloads at the new path) and, if the page's `.workspace` holds user
   content, **leaves it at the old path and prints a one-line note** telling the
   user where the page went so they can move their prep files if they still want
   them. An empty auto-created `.workspace` is just removed.

**Why B over C (the deciding lens — cost vs. event frequency):**

- **A** taxes *every export* (3-way reconciliation) for a *rare* event — settled
  as wrong.
- **C** taxes *every day of normal use*: sidecars move to an opaque,
  `page_id`-keyed store, so the prep files you touch constantly no longer sit
  next to the page's markdown. That adjacency *is* the `.workspace` feature
  (README). C makes the common interaction worse to make the rare event free,
  and the doc's own note says the obvious mitigations "reintroduce tree-coupling
  and should be resisted." C just relocates the tax from machinery to human
  navigation.
- **B** puts the cost *only on the rare event*: one manual `mv` when a page that
  *actually has prep files* is reparented — and even then only after a clear
  note.

Two facts tipped it:
- `.workspace` is **auto-created empty on every page**, so B's note is
  *rare-squared* — it fires only on (page reparented) ∩ (user put files in *that*
  page's `.workspace`). For the vast majority of pages there is nothing to carry
  and nothing to warn about.
- **B is reversible, C is not.** Identity is already `page_id`-keyed in
  frontmatter and the on-disk tree is already the manifest, so B forecloses
  nothing. If real usage later shows reparents are frequent *and* workspace usage
  is heavy *and* the manual `mv` hurts, C can be built then as a data-backed
  upgrade. C now bakes in opaque paths + a migration that is painful to walk back.

**What B deletes (the prize):** the entire §6 mismatch. `reconcile.py` collapses
from park → place → quarantine → case-at-move → git-index patching down to
detect → drop-stale → warn/remove-workspace → prune. Specifically:
`git.py:_stage_sidecar_relocations` is removed; the `relocations` plumbing
(`commit_export`, `_remove_stale_files`, `ExportResult`, CLI) is removed; Codex
**P2 #1** (untracked-onto-tracked) and **P2 #2** (case-probe clobber) disappear by
construction. **P2 #3** (scan descends ignored trees) is still fixed — the
manifest scan is needed under any option — by pruning `.workspace/.media/.conex/
.git` during traversal. The markdown-prune case-fold in `_remove_stale_files`
stays (it converges a re-cased title independent of moves — see the two
`test_recase…` tests) but its probe is made safe (`mkstemp`, no fixed
user-visible name).

**Not doing now (optional, additive later):** a `--heal` flag that, only on
explicit request, relocates a stale `.workspace`. Deliberately out of scope so no
relocation machinery runs silently on every export.

---

## 10. If we nonetheless keep Option A: the concrete debt still owed

(From Codex round 4 — all confirmed against the code. Listed so the doc is
actionable either way; but note these are three *more* cells of the §6 table, not a
bottom.)

- **[P2] `git.py:_stage_sidecar_relocations` early-`continue` on untracked old.**
  When an untracked `.workspace` moves onto a path a deleted page tracked,
  `_remove_stale_files` blanket-skips `.workspace` (never prunes the occupant's
  stale index entry) and `_stage_sidecar_relocations` returns early, so the mover's
  *private, untracked* content lands on the occupant's tracked path and is committed
  by the next pre-export safety commit (`git add -u .`). Fix: even when the old
  sidecar is untracked, **clear (de-index) any tracked entries under the new
  sidecar path** before placing the untracked content (`git rm --cached` the
  occupant), so untracked content cannot masquerade as a tracked modification.
- **[P2] `git.py:_fs_is_case_insensitive` probe uses a fixed, user-visible name.**
  `.conex-case-probe` in the export root: clobbers a real user file of that name,
  and a pre-existing uppercase `.conex-case-PROBE` makes a case-sensitive FS look
  insensitive. Fix: **exclusive-create a uniquely-named temp file**
  (`O_EXCL`, randomized suffix) and test the case-variant of *that* unique name.
- **[P2] `diff.py:scan_export_dir_grouped` rglob-then-filter.**
  `export_dir.rglob("*.md")` recursively descends `.media`/`.workspace`/`.conex`/
  `.git` and only filters via `_NON_PAGE_DIRS` *afterward*, so every full export
  walks the entire attachment/note/git trees. Fix: **prune those directory names
  during traversal** (`os.walk` with in-place `dirs[:] = [...]`) so they are never
  descended.

(Options B and C make #1 and #2 disappear entirely; #3 — efficient scanning — is
worth fixing under any option since the scan is needed to read the manifest.)

---

## 11. Appendix — reusable invariants / gotchas learned

- **Frontmatter `page_id` is the manifest.** No state file needed; idempotent
  recompute is the crash-recovery story. (Validated, keep.)
- **Git derives a rename from a plain delete+add** for content-similar markdown
  (R100). `git mv` buys nothing here. This is *why markdown moves are free* and why
  only sidecars need thought.
- **Never trust `git config core.ignorecase`** — it is set once and drifts across
  machines. Probe the live FS if you must fold case.
- **`Path.resolve()` does not canonicalize on-disk case** — string compares of
  resolved paths are case-sensitive even on a case-insensitive FS.
- **"No-auto-track" for `.workspace`** (don't track what the user didn't commit) is
  a real constraint; it is the source of the untracked-onto-tracked hazard.
- **`_remove_stale_files` blanket-skips `.workspace`** by design (preserve user
  notes), which means stale/relocated workspace entries are *not* self-healing
  through the prune path.
- **Disposability hierarchy** (markdown > media > workspace) is the lens that tells
  you *what actually needs an architecture* — only `.workspace`.
- **#11's allocator stability** rests on the `(position, id)` total order; keep that
  if any layout code is rewritten.
