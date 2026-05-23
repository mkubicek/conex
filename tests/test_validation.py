"""Tests for post-export markdown validation."""

from pathlib import Path

from confluence_export.types import Page, Version
from confluence_export.validation import validate_markdown


def _page():
    return Page(id="p1", title="Architecture", version=Version(number=1))


def _valid_markdown(body: str = "# Architecture\n\nContent") -> str:
    return (
        "---\n"
        "title: Architecture\n"
        "page_id: p1\n"
        "space_key: TEST\n"
        "path: /Architecture\n"
        "---\n\n"
        f"{body}\n"
    )


def test_drawio_sentinel_validation_error(tmp_path):
    md_path = tmp_path / "Architecture.md"
    diagnostics = validate_markdown(
        _valid_markdown("[drawio:arch]"),
        md_path,
        _page(),
    )
    assert any(d.code == "drawio_sentinel_leaked" for d in diagnostics)


def test_drawio_sentinel_in_code_is_allowed(tmp_path):
    md_path = tmp_path / "Architecture.md"
    diagnostics = validate_markdown(
        _valid_markdown("```text\n[drawio:arch]\n```\n\n`[drawio:inline]`"),
        md_path,
        _page(),
    )
    assert not any(d.code == "drawio_sentinel_leaked" for d in diagnostics)


def test_missing_generated_media_validation_error(tmp_path):
    md_path = tmp_path / "Architecture.md"
    missing = tmp_path / ".media" / "arch.drawio.png"
    diagnostics = validate_markdown(
        _valid_markdown("![arch](.media/arch.drawio.png)"),
        md_path,
        _page(),
        generated_media_paths={missing},
    )
    codes = {d.code for d in diagnostics}
    assert "missing_generated_media" in codes
    assert "missing_media" in codes


def test_missing_autolinked_media_validation_error(tmp_path):
    md_path = tmp_path / "Architecture.md"
    diagnostics = validate_markdown(
        _valid_markdown("<.media/arch.drawio>"),
        md_path,
        _page(),
    )
    assert any(d.code == "missing_media" for d in diagnostics)
    assert any(".media/arch.drawio" in d.message for d in diagnostics)


def test_invalid_frontmatter_validation_error(tmp_path):
    diagnostics = validate_markdown("# Architecture\n\nContent", tmp_path / "Architecture.md", _page())
    assert any(d.code == "invalid_frontmatter" for d in diagnostics)


def test_empty_markdown_validation_error(tmp_path):
    diagnostics = validate_markdown(
        (
            "---\n"
            "title: Architecture\n"
            "page_id: p1\n"
            "space_key: TEST\n"
            "path: /Architecture\n"
            "---\n\n"
        ),
        tmp_path / "Architecture.md",
        _page(),
    )
    assert any(d.code == "empty_markdown" for d in diagnostics)


def test_media_validation_allows_spaces_in_filenames(tmp_path):
    md_path = tmp_path / "Architecture.md"
    media = tmp_path / ".media"
    media.mkdir()
    (media / "team photo.png").write_bytes(b"png")
    diagnostics = validate_markdown(
        _valid_markdown("![team](.media/team photo.png)"),
        md_path,
        _page(),
    )
    assert diagnostics == []


def test_media_validation_allows_parentheses_in_filenames(tmp_path):
    md_path = tmp_path / "Architecture.md"
    media = tmp_path / ".media"
    media.mkdir()
    (media / "team (final).png").write_bytes(b"png")
    diagnostics = validate_markdown(
        _valid_markdown("![team](.media/team (final).png)"),
        md_path,
        _page(),
    )
    assert diagnostics == []
