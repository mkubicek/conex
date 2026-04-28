We are in ~/repos/conex on feature branch feature/cme-transform-tests-*. Milan asked: "takeover the tests of confluence-markdown-exporter and apply them on conex; generate a list of markdown transformation gaps where confluence-markdown-exporter is arguably better."

Context:
- CME cloned at /tmp/cme-compare.
- Relevant CME tests: /tmp/cme-compare/tests/unit/test_confluence.py, test_emoticon_conversion.py, test_nbsp_fix.py, test_template_placeholders.py, test_plantuml_conversion.py.
- Conex converter: src/confluence_export/converter.py.
- Existing tests: tests/test_converter.py, tests/test_converter_extra.py, tests/test_drawio*.py.

Task:
1. Add a focused CME-derived parity test file in conex, e.g. tests/test_cme_transform_parity.py.
2. Port transformation tests, adapting to conex APIs. Include exact/source notes in comments/docstrings.
3. For behavior conex already supports or can support with small robust changes, implement the minimal converter fix.
4. For behavior that is larger/not currently supported, mark tests xfail with clear reason. Do not make CI red just to document gaps.
5. Add a markdown gap report, e.g. docs/cme-markdown-transformation-gaps.md, listing where CME is better and whether each gap is now tested, xfailed, or fixed.
6. Keep this narrow: no CME CLI/config/API tests, no full architecture rewrite.
7. Run `uv run --extra dev pytest tests/test_cme_transform_parity.py -q` and then full `uv run --extra dev pytest -q`.
8. Update .dev-loop/PLAN.md with what changed.

Simplicity/robustness: minimal regex/helpers are fine; avoid a new abstraction layer. Preserve conex's LLM-first frontmatter/page-tree/git model.
