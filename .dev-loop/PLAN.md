# Dev Loop Plan — CME transformation tests

## Goal
Port confluence-markdown-exporter markdown transformation behavior checks into conex on a feature branch, without copying CME's unrelated CLI/config/API tests.

## Subproblems

1. Add CME-derived transformation parity tests — DONE
   - Added `tests/test_cme_transform_parity.py` covering anchor slugging, rendered emoji images, unicode whitespace, template placeholder escaping, and PlantUML storage macros.
   - Validation: `uv run --extra dev pytest tests/test_cme_transform_parity.py -q` → 45 passed, 2 xfailed.
   - Not doing: full CME CLI/config/auth parity.

2. Keep CI green while documenting gaps — DONE
   - Added `docs/cme-markdown-transformation-gaps.md`.
   - Gaps are explicit xfails: wiki-style links and rendered-HTML PlantUML via `editor2`.
   - Validation: full `uv run --extra dev pytest -q` → 319 passed, 2 xfailed.
   - Not doing: broad exporter rewrite.

3. Review and final verification — DONE
   - Review recorded in `.dev-loop/REVIEW.md`.
   - Diff is focused on converter parity helpers, CME-derived tests, and the gap report.
