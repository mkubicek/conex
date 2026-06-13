"""Tests for conex.convert.macros — all macro handlers and EMOTICON_MAP.

Coverage strategy:
- Per-handler unit tests with handcrafted storage-XML fixtures.
- Param-in-nested-macro shapes: verify outer handler's params are NOT stolen
  from nested macros (#45 regression class).
- Fidelity checks: for code, panel/info, status, expand, emoticon, and jira,
  run the OLD v1 converter (confluence_export.converter) and assert v2 output
  matches on the agreed sub-set.  Deliberate divergences are documented inline.

DELIBERATE DIVERGENCES from v1 documented in this file:
  (D1/D2 were retired: toc/children/pagetree now emit the v1-style visible
   ``[Confluence dynamic content: NAME (params)]`` placeholder — matching the
   oracle and preserving parameter context — instead of an invisible comment
   that lost the referenced page/params.)
  D3: anchor emits nothing (None → decompose); v1 emits italic
      ``[Confluence dynamic content: anchor (anchor=…)]``.
  D4: profile output is name-only (v2 resolve_user returns str, not a dict
      with email); v1 appended ``(email)`` when the resolver returned one.
  D5: profile "user:ID" fallback (v2) vs. bare "user123" (v1 when no
      resolver and ID echoed directly through).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Import v2 convert API
# ---------------------------------------------------------------------------
from conex.convert import ConvertContext, MediaRefs, convert_page
from conex.convert.macros import EMOTICON_MAP
from conex.convert.registry import HANDLERS, Macro, parse_macro
from conex.models import Attachment, Page, PageVersion, Space
from conex.paths import plan_attachment_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page(**kw: object) -> Page:
    defaults: dict = dict(
        id="p1",
        title="T",
        space_id="s1",
        status="current",
        version=PageVersion(number=1, created_at="2025-01-01T00:00:00Z"),
    )
    defaults.update(kw)
    return Page(**defaults)  # type: ignore[arg-type]


def _make_space(**kw: object) -> Space:
    defaults: dict = dict(id="s1", key="TEST", name="Test Space")
    defaults.update(kw)
    return Space(**defaults)  # type: ignore[arg-type]


def _make_att(
    *,
    id: str = "a1",
    title: str = "file.pdf",
    media_type: str = "application/pdf",
) -> Attachment:
    return Attachment(id=id, title=title, media_type=media_type)


def _make_ctx(
    page: Page | None = None,
    space: Space | None = None,
    attachments: list[Attachment] | None = None,
    *,
    media_enabled: bool = True,
    media_available: set[str] | None = None,
    resolve_user: Callable[[str], str] | None = None,
    rendered_drawio: dict[str, str] | None = None,
) -> ConvertContext:
    p = page or _make_page()
    s = space or _make_space()
    atts = attachments or []
    return ConvertContext(
        page=p,
        space=s,
        site_url="https://example.atlassian.net",
        attachments=atts,
        media=MediaRefs.from_attachments(atts),
        rendered_drawio=rendered_drawio or {},
        resolve_user=resolve_user or (lambda aid: aid),
        media_enabled=media_enabled,
        media_available=media_available if media_available is not None else set(),
    )


def _run(storage_xml: str, ctx: ConvertContext | None = None) -> str:
    """Run v2 convert_page and return the body without the H1 title line."""
    if ctx is None:
        ctx = _make_ctx()
    result = convert_page(storage_xml, ctx)
    # Strip the leading H1 title and any blank lines before content
    lines = result.split("\n")
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            start = i + 1
            break
    return "\n".join(lines[start:]).strip()


def _macro_tag(xml: str) -> Tag:
    """Parse a snippet and return the first ac:structured-macro Tag."""
    soup = BeautifulSoup(xml, "html.parser")
    el = soup.find("ac:structured-macro")
    assert isinstance(el, Tag)
    return el


# ---------------------------------------------------------------------------
# EMOTICON_MAP
# ---------------------------------------------------------------------------


class TestEmoticonMap:
    def test_map_is_non_empty(self) -> None:
        """EMOTICON_MAP must be populated (stub gate lifted)."""
        assert EMOTICON_MAP, "EMOTICON_MAP is empty"

    def test_core_entries_present(self) -> None:
        """Every v1 emoticon name must be present."""
        required = [
            "tick", "cross", "warning", "information", "plus", "minus",
            "question", "light-on", "light-off", "yellow-star", "red-star",
            "green-star", "blue-star", "heart", "thumbs-up", "thumbs-down",
            "smile", "sad", "cheeky", "laugh", "wink",
        ]
        for name in required:
            assert name in EMOTICON_MAP, f"EMOTICON_MAP missing: {name!r}"

    def test_atlassian_shortnames_present(self) -> None:
        for name in ("check_mark", "cross_mark", "info"):
            assert name in EMOTICON_MAP, f"EMOTICON_MAP missing shortname: {name!r}"

    def test_all_values_are_strings(self) -> None:
        for k, v in EMOTICON_MAP.items():
            assert isinstance(v, str) and v, f"Empty/non-string value for {k!r}"


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


class TestHandlerRegistration:
    """All spec'd macros must be registered."""

    REQUIRED = [
        "code", "panel", "info", "note", "warning", "tip", "expand",
        "status", "jira", "toc", "view-file", "viewpdf", "viewppt", "viewxls",
        "drawio", "inc-drawio", "drawio-sketch", "profile", "profile-picture",
        "anchor", "excerpt", "section", "column", "children", "pagetree",
        "attachments", "multimedia", "widget",
    ]

    def test_all_registered(self) -> None:
        for name in self.REQUIRED:
            assert name in HANDLERS, f"Handler not registered: {name!r}"


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------


class TestCodeMacro:
    def test_basic_python(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body><![CDATA[x = 1]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "```" in out
        assert "x = 1" in out

    def test_no_language(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[hello]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "hello" in out

    def test_empty_body(self) -> None:
        xml = '<ac:structured-macro ac:name="code"></ac:structured-macro>'
        out = _run(xml)
        # Empty code block or nothing — must not crash
        assert isinstance(out, str)

    def test_nested_macro_param_not_stolen(self) -> None:
        """An inner macro's language param must not be stolen by the outer code."""
        xml = (
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[outer]]></ac:plain-text-body>"
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">java</ac:parameter>'
            "<ac:plain-text-body><![CDATA[inner]]></ac:plain-text-body>"
            "</ac:structured-macro>"
            "</ac:structured-macro>"
        )
        # Outer code block must not absorb inner's language param
        macro = parse_macro(_macro_tag(xml))
        assert macro.params.get("language", "") == "", (
            "Outer code macro stole nested macro's language param"
        )


# ---------------------------------------------------------------------------
# panel / info / note / warning / tip
# ---------------------------------------------------------------------------


class TestPanelMacros:
    @pytest.mark.parametrize(
        "macro_name, default_title",
        [
            ("panel", "Panel"),
            ("info", "Info"),
            ("note", "Note"),
            ("warning", "Warning"),
            ("tip", "Tip"),
        ],
    )
    def test_renders_blockquote_with_title(self, macro_name: str, default_title: str) -> None:
        xml = (
            f'<ac:structured-macro ac:name="{macro_name}">'
            "<ac:rich-text-body><p>Body text</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert default_title in out
        assert "Body text" in out
        assert ">" in out  # blockquote prefix

    def test_custom_title_via_param(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="title">My Panel</ac:parameter>'
            "<ac:rich-text-body><p>Content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "My Panel" in out
        assert "Content" in out

    def test_nested_macro_body_not_stolen(self) -> None:
        """A panel's inner macro body must not be stolen by the panel's param extractor."""
        xml = (
            '<ac:structured-macro ac:name="info">'
            '<ac:parameter ac:name="title">Outer</ac:parameter>'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">DONE</ac:parameter>'
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        # Inner status macro must survive and render as bold text
        assert "DONE" in out
        # Outer panel title must be correct
        assert "Outer" in out

    def test_panel_nested_code_macro_survives(self) -> None:
        """A code macro nested in a panel body must not be dropped."""
        xml = (
            '<ac:structured-macro ac:name="note">'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[nested code]]></ac:plain-text-body>"
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "nested code" in out


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


class TestExpandMacro:
    def test_renders_h4_title(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">Show More</ac:parameter>'
            "<ac:rich-text-body><p>Content here</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "Show More" in out
        assert "Content here" in out
        # h4 renders as #### in ATX markdown
        assert "####" in out

    def test_default_title_when_no_param(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="expand">'
            "<ac:rich-text-body><p>Details content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "Details" in out
        assert "Details content" in out

    def test_nested_macro_survives_expand(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">T</ac:parameter>'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">DONE</ac:parameter>'
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "DONE" in out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatusMacro:
    def test_renders_bold(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">DONE</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "DONE" in out
        assert "**" in out  # bold markers

    def test_empty_title_removed(self) -> None:
        xml = '<ac:structured-macro ac:name="status"></ac:structured-macro>'
        out = _run(xml)
        assert "status" not in out.lower()

    def test_param_not_stolen_from_nested(self) -> None:
        """Outer status must not steal title from a nested macro."""
        xml = (
            '<ac:structured-macro ac:name="panel">'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">NESTED_STATUS</ac:parameter>'
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        macro = parse_macro(_macro_tag(xml))
        # Panel's params should have no "title" key from the nested status
        assert "title" not in macro.params


# ---------------------------------------------------------------------------
# jira
# ---------------------------------------------------------------------------


class TestJiraMacro:
    def test_renders_inline_code(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">PROJ-123</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "PROJ-123" in out
        assert "`" in out  # inline code

    def test_no_key_removed(self) -> None:
        xml = '<ac:structured-macro ac:name="jira"></ac:structured-macro>'
        out = _run(xml)
        assert "jira" not in out.lower()


# ---------------------------------------------------------------------------
# toc
# ---------------------------------------------------------------------------


class TestTocMacro:
    def test_emits_visible_placeholder(self) -> None:
        """toc emits the v1-style visible dynamic-content placeholder (parity)."""
        xml = '<ac:structured-macro ac:name="toc"/>'
        out = _run(xml)
        assert "[Confluence dynamic content: toc]" in out
        assert "<!-- macro:" not in out  # no leaked raw comment


# ---------------------------------------------------------------------------
# view-file / viewpdf / viewppt / viewxls
# ---------------------------------------------------------------------------


class TestViewFileMacros:
    @pytest.mark.parametrize(
        "macro_name", ["view-file", "viewpdf", "viewppt", "viewxls"]
    )
    def test_link_when_available(self, macro_name: str) -> None:
        att = _make_att(title="doc.pdf")
        ctx = _make_ctx(
            attachments=[att],
            media_available={"doc.pdf"},
        )
        xml = (
            f'<ac:structured-macro ac:name="{macro_name}">'
            '<ac:parameter ac:name="name">doc.pdf</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert ".media/doc.pdf" in out
        assert "doc.pdf" in out

    @pytest.mark.parametrize(
        "macro_name", ["view-file", "viewpdf", "viewppt", "viewxls"]
    )
    def test_missing_note_when_not_available(self, macro_name: str) -> None:
        att = _make_att(title="doc.pdf")
        ctx = _make_ctx(attachments=[att], media_available=set())
        xml = (
            f'<ac:structured-macro ac:name="{macro_name}">'
            '<ac:parameter ac:name="name">doc.pdf</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert "Missing attachment" in out

    def test_ri_attachment_takes_precedence_over_name_param(self) -> None:
        att = _make_att(title="real.pdf")
        ctx = _make_ctx(attachments=[att], media_available={"real.pdf"})
        xml = (
            '<ac:structured-macro ac:name="view-file">'
            '<ac:parameter ac:name="name">wrong.pdf</ac:parameter>'
            '<ac:rich-text-body><ri:attachment ri:filename="real.pdf"/></ac:rich-text-body>'
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert "real.pdf" in out

    def test_no_filename_removed(self) -> None:
        xml = '<ac:structured-macro ac:name="view-file"></ac:structured-macro>'
        out = _run(xml)
        assert "view-file" not in out.lower()


# ---------------------------------------------------------------------------
# drawio / inc-drawio
# ---------------------------------------------------------------------------


class TestDrawioMacro:
    def test_not_rendered_emits_note(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">mydiagram</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "not rendered" in out.lower()
        assert "mydiagram.drawio" in out

    def test_rendered_emits_img(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">mydiagram</ac:parameter>'
            "</ac:structured-macro>"
        )
        ctx = _make_ctx(
            rendered_drawio={"mydiagram": "mydiagram.png"},
            media_available=set(),
        )
        out = _run(xml, ctx)
        assert "mydiagram.png" in out
        assert "![" in out or "img" in out.lower() or ".media/mydiagram.png" in out

    def test_rendered_png_matched_by_attachment_title_case_insensitive(self) -> None:
        """diagramName differs in case/whitespace from the .drawio attachment
        title; build keys rendered_drawio by the title, so the lookup must match
        the attachment title tolerantly and emit the <img>, not the note."""
        att = _make_att(title="My Diagram.drawio", media_type="application/x-drawio")
        ctx = _make_ctx(
            attachments=[att],
            rendered_drawio={"My Diagram.drawio": "My_Diagram.png"},
            media_available={"My_Diagram.png"},
        )
        out = _run(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">my diagram</ac:parameter>'
            "</ac:structured-macro>",
            ctx,
        )
        assert "My_Diagram.png" in out
        assert "not rendered" not in out.lower()

    def test_source_link_only_when_source_on_disk(self) -> None:
        """F5 dead-source-link rule: source link appears only when .drawio is available."""
        att = _make_att(title="mydiagram.drawio", media_type="application/x-drawio")
        ctx_no_source = _make_ctx(
            attachments=[att],
            rendered_drawio={"mydiagram": "mydiagram.png"},
            media_available=set(),  # source NOT on disk
        )
        out_no_source = _run(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">mydiagram</ac:parameter>'
            "</ac:structured-macro>",
            ctx_no_source,
        )
        assert "Draw.io source" not in out_no_source

        ctx_with_source = _make_ctx(
            attachments=[att],
            rendered_drawio={"mydiagram": "mydiagram.png"},
            media_available={"mydiagram.drawio"},  # source IS on disk
        )
        out_with_source = _run(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">mydiagram</ac:parameter>'
            "</ac:structured-macro>",
            ctx_with_source,
        )
        assert "Draw.io source" in out_with_source

    def test_inc_drawio_same_as_drawio(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="inc-drawio">'
            '<ac:parameter ac:name="diagramName">diagram2</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "diagram2.drawio" in out

    def test_extension_tolerant_name_lookup(self) -> None:
        """diagramName without .drawio extension should still find the attachment."""
        att = _make_att(title="mydiagram.drawio", media_type="application/x-drawio")
        ctx = _make_ctx(
            attachments=[att],
            rendered_drawio={"mydiagram.drawio": "mydiagram.png"},
            media_available={"mydiagram.drawio"},
        )
        xml = (
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">mydiagram</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert "Draw.io source" in out


# ---------------------------------------------------------------------------
# drawio-sketch
# ---------------------------------------------------------------------------


class TestDrawioSketchMacro:
    def test_no_attachment_emits_sketch_note(self) -> None:
        xml = '<ac:structured-macro ac:name="drawio-sketch"/>'
        out = _run(xml)
        assert "Draw.io sketch" in out
        assert "not rendered" not in out.lower()

    def test_attachment_backed_renders_like_drawio(self) -> None:
        att = _make_att(title="sketch.drawio", media_type="application/x-drawio")
        ctx = _make_ctx(
            attachments=[att],
            rendered_drawio={"sketch.drawio": "sketch.png"},
            media_available={"sketch.drawio"},
        )
        xml = (
            '<ac:structured-macro ac:name="drawio-sketch">'
            "<ac:rich-text-body>"
            '<ri:attachment ri:filename="sketch.drawio"/>'
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert "sketch.png" in out


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


class TestProfileMacro:
    def test_renders_as_list_item(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="profile">'
            '<ri:user ri:account-id="user123"/>'
            "</ac:structured-macro>"
        )
        ctx = _make_ctx(resolve_user=lambda aid: "Alice Smith")
        out = _run(xml, ctx)
        assert "Alice Smith" in out
        assert "*" in out  # markdown list

    def test_unknown_user_fallback(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="profile">'
            '<ri:user ri:account-id="user999"/>'
            "</ac:structured-macro>"
        )
        # Resolver returns the id unchanged (no display name found)
        ctx = _make_ctx(resolve_user=lambda aid: aid)
        out = _run(xml, ctx)
        # D5: v2 formats as "user:ID"; v1 echoed the raw id
        assert "user:user999" in out

    def test_no_user_produces_unknown(self) -> None:
        xml = '<ac:structured-macro ac:name="profile"></ac:structured-macro>'
        out = _run(xml)
        assert "Unknown user" in out

    def test_divergence_no_email_appended(self) -> None:
        """D4: v2 never appends email; v1 did when resolver returned email."""
        xml = (
            '<ac:structured-macro ac:name="profile">'
            '<ri:user ri:account-id="uid"/>'
            "</ac:structured-macro>"
        )
        ctx = _make_ctx(resolve_user=lambda aid: "Bob")
        out = _run(xml, ctx)
        assert "@" not in out  # no email address


# ---------------------------------------------------------------------------
# profile-picture
# ---------------------------------------------------------------------------


class TestProfilePictureMacro:
    def test_resolved_span_unwraps(self) -> None:
        """pre-pass places @mention span; handler unwraps it."""
        xml = (
            '<ac:structured-macro ac:name="profile-picture">'
            '<ri:user ri:account-id="user123"/>'
            "</ac:structured-macro>"
        )
        ctx = _make_ctx(resolve_user=lambda aid: "Alice")
        out = _run(xml, ctx)
        assert "@Alice" in out

    def test_no_user_dropped(self) -> None:
        xml = '<ac:structured-macro ac:name="profile-picture"></ac:structured-macro>'
        out = _run(xml)
        assert "profile-picture" not in out.lower()


# ---------------------------------------------------------------------------
# anchor
# ---------------------------------------------------------------------------


class TestAnchorMacro:
    def test_anchor_dropped(self) -> None:
        """D3: anchor emits nothing; v1 emitted a dynamic-content placeholder."""
        xml = (
            '<ac:structured-macro ac:name="anchor">'
            '<ac:parameter ac:name="anchor">my-target</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        # Param value must not appear in output
        assert "my-target" not in out
        assert "Confluence dynamic" not in out

    def test_anchor_removed_inline(self) -> None:
        """Anchor mid-paragraph must not break surrounding content."""
        xml = (
            "<p>Hello"
            '<ac:structured-macro ac:name="anchor">'
            '<ac:parameter ac:name="anchor">here</ac:parameter>'
            "</ac:structured-macro>"
            "World</p>"
        )
        out = _run(xml)
        assert "Hello" in out
        assert "World" in out
        assert "anchor" not in out.lower()


# ---------------------------------------------------------------------------
# excerpt / section / column
# ---------------------------------------------------------------------------


class TestUnwrapMacros:
    @pytest.mark.parametrize("macro_name", ["excerpt", "section", "column"])
    def test_body_content_preserved(self, macro_name: str) -> None:
        xml = (
            f'<ac:structured-macro ac:name="{macro_name}">'
            "<ac:rich-text-body><p>Keep this</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "Keep this" in out

    def test_section_column_nested(self) -> None:
        """Section wrapping a column: both unwrap, content survives."""
        xml = (
            '<ac:structured-macro ac:name="section">'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="column">'
            "<ac:rich-text-body><p>col</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "col" in out


# ---------------------------------------------------------------------------
# children / pagetree
# ---------------------------------------------------------------------------


class TestCommentPlaceholderMacros:
    def test_children_visible_placeholder(self) -> None:
        """children emits the v1-style visible placeholder (parity)."""
        xml = '<ac:structured-macro ac:name="children"/>'
        out = _run(xml)
        assert "[Confluence dynamic content: children]" in out
        assert "<!-- macro:" not in out

    def test_pagetree_visible_placeholder(self) -> None:
        """pagetree emits the v1-style visible placeholder (parity)."""
        xml = '<ac:structured-macro ac:name="pagetree"/>'
        out = _run(xml)
        assert "[Confluence dynamic content: pagetree]" in out
        assert "<!-- macro:" not in out

    def test_children_pagetree_emit_visible_placeholder(self) -> None:
        """children/pagetree now match v1's visible dynamic-content placeholder."""
        for name in ("children", "pagetree"):
            xml = f'<ac:structured-macro ac:name="{name}"/>'
            out = _run(xml)
            assert f"[Confluence dynamic content: {name}]" in out


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


class TestAttachmentsMacro:
    def test_lists_available_attachments(self) -> None:
        att1 = _make_att(id="a1", title="file1.pdf")
        att2 = _make_att(id="a2", title="file2.png", media_type="image/png")
        ctx = _make_ctx(
            attachments=[att1, att2],
            media_available={"file1.pdf", "file2.png"},
        )
        xml = '<ac:structured-macro ac:name="attachments"/>'
        out = _run(xml, ctx)
        assert "file1.pdf" in out
        assert "file2.png" in out
        assert ".media/" in out

    def test_missing_attachment_note(self) -> None:
        att = _make_att(title="missing.pdf")
        ctx = _make_ctx(attachments=[att], media_available=set())
        xml = '<ac:structured-macro ac:name="attachments"/>'
        out = _run(xml, ctx)
        assert "Missing attachment" in out
        assert "missing.pdf" in out

    def test_empty_attachments_list_produces_nothing(self) -> None:
        ctx = _make_ctx(attachments=[], media_available=set())
        xml = '<ac:structured-macro ac:name="attachments"/>'
        out = _run(xml, ctx)
        # Empty: no list content emitted
        assert "Missing" not in out
        assert "media" not in out


# ---------------------------------------------------------------------------
# multimedia / widget
# ---------------------------------------------------------------------------


class TestMultimediaWidget:
    def test_multimedia_available_link(self) -> None:
        att = _make_att(title="video.mp4", media_type="video/mp4")
        ctx = _make_ctx(attachments=[att], media_available={"video.mp4"})
        xml = (
            '<ac:structured-macro ac:name="multimedia">'
            '<ac:parameter ac:name="name">video.mp4</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert "video.mp4" in out
        assert ".media/" in out

    def test_multimedia_missing_note(self) -> None:
        att = _make_att(title="video.mp4", media_type="video/mp4")
        ctx = _make_ctx(attachments=[att], media_available=set())
        xml = (
            '<ac:structured-macro ac:name="multimedia">'
            '<ac:parameter ac:name="name">video.mp4</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml, ctx)
        assert "Missing attachment" in out

    def test_widget_with_url(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="widget">'
            '<ac:parameter ac:name="url">https://example.com/embed</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "https://example.com/embed" in out

    def test_multimedia_url_only_emits_placeholder(self) -> None:
        """A url-only multimedia embed (YouTube/Vimeo, no attachment) must emit a
        visible dynamic-content placeholder, not be silently dropped (v1 parity)."""
        xml = (
            '<ac:structured-macro ac:name="multimedia">'
            '<ac:parameter ac:name="url">https://youtu.be/abc</ac:parameter>'
            "</ac:structured-macro>"
        )
        out = _run(xml)
        assert "[Confluence dynamic content: multimedia" in out

    def test_widget_no_url_visible_placeholder(self) -> None:
        xml = '<ac:structured-macro ac:name="widget"/>'
        out = _run(xml)
        assert "[Confluence dynamic content: widget]" in out
        assert "<!-- macro:" not in out


# ---------------------------------------------------------------------------
# Nested macro — param/body not stolen (#45)
# ---------------------------------------------------------------------------


class TestNoParamSteal:
    """parse_macro with recursive=False must not steal nested macro content."""

    def test_nested_code_lang_not_stolen(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="title">Outer</ac:parameter>'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">java</ac:parameter>'
            "<ac:plain-text-body><![CDATA[code]]></ac:plain-text-body>"
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xml, "html.parser")
        outer_el = soup.find("ac:structured-macro", attrs={"ac:name": "panel"})
        assert isinstance(outer_el, Tag)
        outer = parse_macro(outer_el)
        # Outer params contain only "title"
        assert set(outer.params.keys()) == {"title"}, (
            f"Outer panel stole nested params: {outer.params}"
        )

    def test_nested_body_not_stolen(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">Outer</ac:parameter>'
            "<ac:rich-text-body>"
            "<p>Outer content</p>"
            '<ac:structured-macro ac:name="info">'
            "<ac:rich-text-body><p>Inner content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xml, "html.parser")
        outer_el = soup.find("ac:structured-macro", attrs={"ac:name": "expand"})
        assert isinstance(outer_el, Tag)
        outer = parse_macro(outer_el)
        # rich_body is the direct child only
        assert outer.rich_body is not None
        # The inner ac:rich-text-body is nested, not the outer's plain_body
        inner_bodies = outer.rich_body.find_all("ac:rich-text-body")
        assert len(inner_bodies) == 1  # inner's body is inside the outer's body


# ---------------------------------------------------------------------------
# Fidelity checks against v1 oracle
# ---------------------------------------------------------------------------


def _v1_body(storage_xml: str, **kw: object) -> str:
    """Run the v1 converter and return the markdown body (stripped of frontmatter)."""
    # Import v1 only in tests — never in production conex code
    import sys as _sys
    import importlib

    # Ensure v1 src is on the path (worktree has both packages under src/)
    worktree_src = Path(__file__).parent.parent / "src"
    if str(worktree_src) not in _sys.path:
        _sys.path.insert(0, str(worktree_src))

    from confluence_export.types import Page as V1Page, Attachment as V1Att, Version as V1Ver
    from confluence_export.converter import convert_page as v1_convert

    page = V1Page(id="p1", title="T", body_storage=storage_xml)
    result = v1_convert(page, "https://ex.com", "TEST", "path", **kw)
    # Strip frontmatter
    parts = result.split("---\n\n", 1)
    body = parts[1] if len(parts) > 1 else result
    # Strip title H1 and trailing newline
    lines = body.split("\n")
    content_lines = [l for i, l in enumerate(lines) if not (i == 0 and l.startswith("# "))]
    return "\n".join(content_lines).strip()


class TestFidelityVsV1:
    """Assert v2 output matches v1 for the agreed subset of macros.

    Divergences are documented with D-tags referencing the header comment.
    """

    def test_code_no_lang(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[hello world]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"code (no lang) mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_code_with_lang(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body><![CDATA[x = 1\nprint(x)]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        # Both should contain the code; markdownify drops the language class
        assert "x = 1" in v2
        assert "print(x)" in v2
        # The fences are identical
        assert v2 == v1, f"code (with lang) mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_info_panel(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="info">'
            "<ac:rich-text-body><p>Body text</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"info panel mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_panel_with_title(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="title">Custom Title</ac:parameter>'
            "<ac:rich-text-body><p>Panel body</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"panel title mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_status_macro(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">IN PROGRESS</ac:parameter>'
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"status mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_expand_macro(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">Show Details</ac:parameter>'
            "<ac:rich-text-body><p>Expanded content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"expand mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_jira_macro(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">TICKET-42</ac:parameter>'
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"jira mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_emoticon_tick(self) -> None:
        xml = "<p>Done <ac:emoticon ac:name=\"tick\"/></p>"
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert "✅" in v2
        assert v2 == v1, f"emoticon tick mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_emoticon_thumbs_up(self) -> None:
        xml = "<p>Nice <ac:emoticon ac:name=\"thumbs-up\"/></p>"
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert EMOTICON_MAP["thumbs-up"] in v2
        assert v2 == v1, f"emoticon thumbs-up mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_drawio_not_rendered(self) -> None:
        xml = (
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">mydiagram</ac:parameter>'
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"drawio (not rendered) mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_drawio_sketch_no_attachment(self) -> None:
        xml = '<ac:structured-macro ac:name="drawio-sketch"/>'
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert v2 == v1, f"drawio-sketch mismatch:\nv1={v1!r}\nv2={v2!r}"

    def test_toc_parity(self) -> None:
        """toc now matches v1: both emit the visible dynamic-content placeholder."""
        xml = '<ac:structured-macro ac:name="toc"/>'
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert "Confluence dynamic content: toc" in v1
        assert "Confluence dynamic content: toc" in v2

    def test_anchor_divergence(self) -> None:
        """D3: v2 drops anchor entirely; v1 emits a dynamic-content placeholder."""
        xml = (
            '<ac:structured-macro ac:name="anchor">'
            '<ac:parameter ac:name="anchor">my-target</ac:parameter>'
            "</ac:structured-macro>"
        )
        v1 = _v1_body(xml)
        v2 = _run(xml)
        assert "Confluence dynamic content" in v1
        # v2: param value must not appear
        assert "my-target" not in v2
        assert v2 != v1  # divergence confirmed


# ---------------------------------------------------------------------------
# Emoticon in-pipeline tests (pass 5 runs on top of pre-pass)
# ---------------------------------------------------------------------------


class TestEmoticonPipeline:
    def test_tick_substituted(self) -> None:
        out = _run("<p><ac:emoticon ac:name=\"tick\"/></p>")
        assert EMOTICON_MAP["tick"] in out
        assert "ac:emoticon" not in out

    def test_thumbs_down_substituted(self) -> None:
        out = _run("<p><ac:emoticon ac:name=\"thumbs-down\"/></p>")
        assert EMOTICON_MAP["thumbs-down"] in out

    def test_unknown_emoticon_removed(self) -> None:
        out = _run("<p><ac:emoticon ac:name=\"zzz-no-such-thing\"/></p>")
        assert "ac:emoticon" not in out

    def test_emoji_shortname_fallback(self) -> None:
        """ac:emoji-shortname attribute lookup (v1 parity)."""
        out = _run('<p><ac:emoticon ac:emoji-shortname=":check_mark:"/></p>')
        assert EMOTICON_MAP["check_mark"] in out

    def test_emoticon_in_task_body_resolved(self) -> None:
        """Emoticons must be substituted BEFORE task list get_text() (#43-class)."""
        xml = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            '<ac:task-body>Ship it <ac:emoticon ac:name="thumbs-up"/></ac:task-body>'
            "</ac:task></ac:task-list>"
        )
        out = _run(xml)
        assert "Ship it" in out
        assert "ac:emoticon" not in out
        assert EMOTICON_MAP["thumbs-up"] in out
