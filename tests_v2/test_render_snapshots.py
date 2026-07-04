"""Golden full-page snapshots for the storage-XHTML → markdown pipeline.

Each directory under fixtures/render/ is one fixture: input.html (storage
XHTML) + expected.md (the committed markdown body).  The test runs the full
8-pass convert_page pipeline and diffs against the snapshot, so a change to
any render stage that alters output for a whole page class fails with a
readable per-page diff instead of passing silently.

Regenerating after an intentional pipeline change:

    CONEX_UPDATE_SNAPSHOTS=1 uv run pytest tests_v2/test_render_snapshots.py

then review the expected.md diff like any other code change.

Snapshots cover the BODY only (convert_page output, no frontmatter) —
frontmatter shape is locked separately in test_convert_core.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conex.convert import ConvertContext, MediaRefs, convert_page
from conex.models import Attachment, Page, PageVersion, Space

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "render"
FIXTURE_DIRS = sorted(p for p in FIXTURES_DIR.iterdir() if (p / "input.html").is_file())

# Shared context: two attachments exist and are on disk, so fixtures can
# exercise both the available path (diagram.png, Report.pdf) and the
# missing-attachment fallback (any other filename).
_ATTACHMENTS = [
    Attachment(id="a1", title="diagram.png", media_type="image/png", file_size=100),
    Attachment(id="a2", title="Report.pdf", media_type="application/pdf", file_size=200),
]
_USERS = {"abc123": "Jane Doe"}


def _ctx(fixture_name: str) -> ConvertContext:
    title = fixture_name.replace("-", " ").title()
    page = Page(
        id="p1",
        title=title,
        space_id="s1",
        version=PageVersion(number=1, created_at="2025-01-01T12:00:00Z"),
        web_url="https://example.atlassian.net/wiki/spaces/TEST/pages/p1",
    )
    space = Space(id="s1", key="TEST", name="Test Space")
    return ConvertContext(
        page=page,
        space=space,
        site_url="https://example.atlassian.net",
        attachments=_ATTACHMENTS,
        media=MediaRefs.from_attachments(_ATTACHMENTS),
        rendered_drawio={},
        resolve_user=lambda aid: _USERS.get(aid, aid),
        media_enabled=True,
        media_available={a.title for a in _ATTACHMENTS},
    )


def test_fixture_corpus_is_nonempty() -> None:
    assert len(FIXTURE_DIRS) >= 6


@pytest.mark.parametrize("fixture_dir", FIXTURE_DIRS, ids=lambda d: d.name)
def test_render_snapshot(fixture_dir: Path) -> None:
    input_html = (fixture_dir / "input.html").read_text(encoding="utf-8")
    ctx = _ctx(fixture_dir.name)
    got = convert_page(input_html, ctx) + "\n"

    # Determinism: a second conversion of the same input must be identical.
    assert convert_page(input_html, _ctx(fixture_dir.name)) + "\n" == got

    expected_file = fixture_dir / "expected.md"
    if os.environ.get("CONEX_UPDATE_SNAPSHOTS") == "1":
        expected_file.write_text(got, encoding="utf-8")

    assert expected_file.is_file(), (
        f"missing snapshot {expected_file}; run with CONEX_UPDATE_SNAPSHOTS=1 to create it"
    )
    expected = expected_file.read_text(encoding="utf-8")
    assert got == expected, (
        f"render output changed for fixture '{fixture_dir.name}'.\n"
        "If intentional, regenerate with CONEX_UPDATE_SNAPSHOTS=1 and review the diff."
    )
