# Review — CME transformation tests

## Findings

- Scope stayed focused on markdown transformations, not CME CLI/config/API tests.
- Added CME-derived tests for emoji img tags, unicode whitespace, template placeholder escaping, anchor normalization, and PlantUML.
- CI remains green by marking two product-scope gaps as xfail: wiki-link mode and rendered-HTML PlantUML lookup via `editor2`.
- Converter changes are post/pre-processing helpers around the existing markdownify pipeline; no broad architecture rewrite.
- Gap report added at `docs/cme-markdown-transformation-gaps.md`.

## Validation

- `uv run --extra dev pytest tests/test_cme_transform_parity.py -q` → 45 passed, 2 xfailed.
- `uv run --extra dev pytest -q` → 319 passed, 2 xfailed.

## Notes

- The Claude worker partially completed the implementation but hung without final output; Sihly inspected, completed missing documentation, and ran validation directly.
- Follow-up product decision: whether conex should ever support configurable wiki/ADO link modes, or keep that as CME territory.
