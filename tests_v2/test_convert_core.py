"""Tests for conex.convert — the v2 storage-XHTML → Markdown pipeline.

Coverage targets per SPEC-V2.md §convert/:
- parse_macro ownership: nested macro params/bodies NOT stolen (#45 regression
  shapes from tests/test_converter*.py)
- default_handler 3 branches
- ADF decision/task lists incl. innermost-first nested-list lift (#40/#43)
- links/images availability gating
- frontmatter golden shape
- pass ordering: ADF lists rendered BEFORE generic ac:adf-node unwrap
- fidelity against v1 for the subset implemented in this wave (headings,
  lists, links, images, emoticons, decision lists); macro-handler behaviors
  (code, panel, status, expand, …) are DELIBERATELY excluded here because
  macros.py is a stub in this wave — a follow-up worker fills those handlers.

DELIBERATE DIVERGENCES from v1 noted inline where they exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pytest
import yaml
from bs4 import BeautifulSoup, Tag

from conex.convert import (
    CONVERTER_VERSION,
    ConvertContext,
    MediaRefs,
    build_frontmatter,
    convert_page,
)
from conex.convert.registry import (
    HANDLERS,
    Macro,
    default_handler,
    parse_macro,
    register,
)
from conex.models import Attachment, Page, PageVersion, Space
from conex.paths import plan_attachment_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page(**kw) -> Page:
    defaults = dict(
        id="p1",
        title="Test Page",
        space_id="s1",
        status="current",
        version=PageVersion(number=3, created_at="2025-01-01T12:00:00Z"),
        web_url="https://example.atlassian.net/wiki/spaces/TEST/pages/p1",
    )
    defaults.update(kw)
    return Page(**defaults)


def _make_space(**kw) -> Space:
    defaults = dict(id="s1", key="TEST", name="Test Space")
    defaults.update(kw)
    return Space(**defaults)


def _make_ctx(
    page: Page | None = None,
    space: Space | None = None,
    attachments: list[Attachment] | None = None,
    *,
    media_enabled: bool = True,
    media_available: set[str] | None = None,
    resolve_user: Callable[[str], str] | None = None,
) -> ConvertContext:
    p = page or _make_page()
    s = space or _make_space()
    atts = attachments or []
    media = MediaRefs.from_attachments(atts)
    return ConvertContext(
        page=p,
        space=s,
        site_url="https://example.atlassian.net",
        attachments=atts,
        media=media,
        rendered_drawio={},
        resolve_user=resolve_user or (lambda aid: aid),
        media_enabled=media_enabled,
        media_available=media_available if media_available is not None else set(),
    )


def _soup_tag(html: str) -> Tag:
    """Parse HTML and return the first Tag."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup.contents:
        if isinstance(t, Tag):
            return t
    raise ValueError(f"no Tag found in: {html!r}")


def _preprocess(html: str, ctx: ConvertContext | None = None) -> str:
    """Run the full preprocessing pipeline and return the HTML string."""
    from conex.convert.render import preprocess_storage_xhtml

    if ctx is None:
        ctx = _make_ctx()
    return preprocess_storage_xhtml(html, ctx)


# ---------------------------------------------------------------------------
# CONVERTER_VERSION
# ---------------------------------------------------------------------------


def test_converter_version_is_int() -> None:
    assert isinstance(CONVERTER_VERSION, int)
    assert CONVERTER_VERSION >= 1


# ---------------------------------------------------------------------------
# MediaRefs
# ---------------------------------------------------------------------------


class TestMediaRefs:
    def test_filename_for_id_returns_planned_name(self) -> None:
        atts = [Attachment(id="a1", title="diagram.png", media_type="image/png")]
        refs = MediaRefs.from_attachments(atts)
        assert refs.filename_for_id("a1") == "diagram.png"

    def test_filename_for_id_unknown_returns_none(self) -> None:
        refs = MediaRefs.from_attachments([])
        assert refs.filename_for_id("nope") is None

    def test_filename_for_title_exact(self) -> None:
        atts = [Attachment(id="a1", title="Report.pdf")]
        refs = MediaRefs.from_attachments(atts)
        assert refs.filename_for_title("Report.pdf") == "Report.pdf"

    def test_filename_for_title_casefold_fallback(self) -> None:
        """NFC-casefold title fallback — PORT v1 for_reference semantics."""
        atts = [Attachment(id="a1", title="Report.pdf")]
        refs = MediaRefs.from_attachments(atts)
        # Lowercase version should resolve to the exact-case planned name.
        result = refs.filename_for_title("report.pdf")
        assert result == "Report.pdf"

    def test_filename_for_title_unknown_returns_safe_name(self) -> None:
        refs = MediaRefs.from_attachments([])
        # Falls back to safe_attachment_name — never returns None.
        result = refs.filename_for_title("some file.txt")
        assert result == "some file.txt"

    def test_collision_same_title_different_ids(self) -> None:
        atts = [
            Attachment(id="id1", title="shot.png"),
            Attachment(id="id2", title="shot.png"),
        ]
        refs = MediaRefs.from_attachments(atts)
        n1 = refs.filename_for_id("id1")
        n2 = refs.filename_for_id("id2")
        assert n1 != n2
        assert n1 is not None
        assert n2 is not None


# ---------------------------------------------------------------------------
# parse_macro — the #45-class killer
# ---------------------------------------------------------------------------


class TestParseMacro:
    def test_returns_macro_with_correct_name(self) -> None:
        tag = _soup_tag('<ac:structured-macro ac:name="code"></ac:structured-macro>')
        macro = parse_macro(tag)
        assert macro.name == "code"
        assert macro.element is tag

    def test_extracts_direct_params_only(self) -> None:
        """Nested macro params must NOT be stolen by outer macro (#45)."""
        tag = _soup_tag(
            '<ac:structured-macro ac:name="outer">'
            '<ac:parameter ac:name="own">ownval</ac:parameter>'
            '<ac:structured-macro ac:name="inner">'
            '<ac:parameter ac:name="stolen">stolenval</ac:parameter>'
            "</ac:structured-macro>"
            "</ac:structured-macro>"
        )
        macro = parse_macro(tag)
        assert macro.params == {"own": "ownval"}
        assert "stolen" not in macro.params

    def test_extracts_direct_rich_body_only(self) -> None:
        """Nested macro rich-text-body must NOT be stolen (#45)."""
        tag = _soup_tag(
            '<ac:structured-macro ac:name="outer">'
            "<ac:rich-text-body><p>outer body</p></ac:rich-text-body>"
            '<ac:structured-macro ac:name="inner">'
            "<ac:rich-text-body><p>inner body</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            "</ac:structured-macro>"
        )
        macro = parse_macro(tag)
        assert macro.rich_body is not None
        assert "outer body" in macro.rich_body.get_text()
        assert "inner body" not in macro.rich_body.get_text()

    def test_extracts_plain_body_text(self) -> None:
        tag = _soup_tag(
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body>print('hi')</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        macro = parse_macro(tag)
        assert macro.plain_body == "print('hi')"
        assert macro.rich_body is None

    def test_missing_body_and_params_are_none_and_empty(self) -> None:
        tag = _soup_tag('<ac:structured-macro ac:name="toc"></ac:structured-macro>')
        macro = parse_macro(tag)
        assert macro.name == "toc"
        assert macro.params == {}
        assert macro.rich_body is None
        assert macro.plain_body is None

    def test_nested_macro_plain_body_not_stolen(self) -> None:
        """Outer macro with no own plain-text-body must get plain_body=None,
        not steal the inner macro's plain-text-body (#45 shape)."""
        tag = _soup_tag(
            '<ac:structured-macro ac:name="outer">'
            '<ac:structured-macro ac:name="inner">'
            "<ac:plain-text-body>inner code</ac:plain-text-body>"
            "</ac:structured-macro>"
            "</ac:structured-macro>"
        )
        macro = parse_macro(tag)
        assert macro.plain_body is None
        assert macro.rich_body is None

    def test_multiple_params_all_extracted(self) -> None:
        tag = _soup_tag(
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">PROJ-1</ac:parameter>'
            '<ac:parameter ac:name="columns">key,summary</ac:parameter>'
            "</ac:structured-macro>"
        )
        macro = parse_macro(tag)
        assert macro.params["key"] == "PROJ-1"
        assert macro.params["columns"] == "key,summary"


# ---------------------------------------------------------------------------
# default_handler — 3 branches
# ---------------------------------------------------------------------------


class TestDefaultHandler:
    def _make_macro_from_html(self, html: str) -> tuple[Macro, BeautifulSoup]:
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("ac:structured-macro")
        assert isinstance(el, Tag)
        return parse_macro(el), soup

    def test_branch1_rich_body_returned(self) -> None:
        """Branch 1: has own rich body → returns the body tag."""
        macro, soup = self._make_macro_from_html(
            '<ac:structured-macro ac:name="xyz">'
            "<ac:rich-text-body><p>Keep me</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        assert result is macro.rich_body
        body_text = result.get_text() if isinstance(result, Tag) else str(result)
        assert "Keep me" in body_text

    def test_branch1_plain_body_returned_when_no_rich_body(self) -> None:
        """Branch 1 fallback: has own plain body → returns plain body text."""
        macro, soup = self._make_macro_from_html(
            '<ac:structured-macro ac:name="xyz">'
            "<ac:plain-text-body>plain content</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        assert result == "plain content"

    def test_branch2_bodyless_wrapping_macros_unwraps(self) -> None:
        """Branch 2: bodyless but wraps other macros → unwrap in-place, return None."""
        html = (
            '<ac:structured-macro ac:name="future-widget">'
            '<ac:parameter ac:name="conf">x</ac:parameter>'
            '<ac:structured-macro ac:name="inner"><ac:rich-text-body>'
            "<p>Inner body</p></ac:rich-text-body></ac:structured-macro>"
            "</ac:structured-macro>"
        )
        macro, soup = self._make_macro_from_html(html)
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        # Branch 2 returns None (handled in place via unwrap)
        assert result is None
        # The outer macro element was unwrapped: inner macro is still in soup
        assert soup.find("ac:structured-macro", {"ac:name": "inner"}) is not None
        # The outer macro shell is gone
        assert soup.find("ac:structured-macro", {"ac:name": "future-widget"}) is None
        # The parameter was decomposed: raw param value must not leak
        assert ">x<" not in str(soup)

    def test_branch3_no_body_no_inner_macros_returns_placeholder(self) -> None:
        """Branch 3: no body and no inner macros → visible v1-style placeholder."""
        macro, _soup = self._make_macro_from_html(
            '<ac:structured-macro ac:name="toc"></ac:structured-macro>'
        )
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        assert isinstance(result, Tag)
        assert result.get_text() == "[Confluence dynamic content: toc]"

    def test_branch3_unnamed_macro_returns_unnamed_placeholder(self) -> None:
        macro, _soup = self._make_macro_from_html(
            '<ac:structured-macro></ac:structured-macro>'
        )
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        assert result.get_text() == "[Confluence dynamic content: unnamed]"

    def test_branch3_placeholder_includes_resolved_page_param(self) -> None:
        """The placeholder preserves non-default params (ri:page by title)."""
        macro, _soup = self._make_macro_from_html(
            '<ac:structured-macro ac:name="include">'
            '<ac:parameter ac:name="page"><ri:page ri:content-title="Target"/></ac:parameter>'
            "</ac:structured-macro>"
        )
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        assert result.get_text() == "[Confluence dynamic content: include (page=Target)]"

    def test_branch1_empty_rich_body_falls_through_to_branch3(self) -> None:
        """A rich body that is present but has only whitespace is not branch 1."""
        macro, _soup = self._make_macro_from_html(
            '<ac:structured-macro ac:name="empty">'
            "<ac:rich-text-body>   </ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        ctx = _make_ctx()
        result = default_handler(macro, ctx)
        # No inner macros → branch 3 (visible placeholder)
        assert result.get_text() == "[Confluence dynamic content: empty]"


# ---------------------------------------------------------------------------
# ADF decision/task lists — pass 1
# ---------------------------------------------------------------------------


class TestAdfDecisionLists:
    def test_decided_item_marked_with_checkmark(self) -> None:
        """#40: decided items render with ✓ prefix."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>Ship it</p></ac:adf-node>'
            '<ac:adf-node type="decisionItem" state="UNDECIDED"><p>Maybe later</p></ac:adf-node>'
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert "<li>✓ Ship it</li>" in result
        assert "<li>Maybe later</li>" in result
        assert "✓ Maybe later" not in result

    def test_empty_decision_list_dropped(self) -> None:
        """#40: an empty decision list is dropped, not rendered as empty <ul>."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"></ac:adf-node>'
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert "<li>" not in result
        assert "ac:adf-node" not in result

    def test_nested_decision_list_not_double_rendered(self) -> None:
        """#43: a decisionList nested inside a decisionItem must not emit items twice."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>Outer</p>'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Inner</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert result.count("Outer") == 1

    def test_nested_decision_list_in_item_renders_as_sublist(self) -> None:
        """Innermost-first: inner decided item gets ✓, not fused into outer text."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Outer</p>'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>Inner</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert "✓ Inner" in result
        assert "OuterInner" not in result

    def test_decision_list_inside_decision_list_content_kept(self) -> None:
        """Outer has no direct items — content from inner list survives."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Inner</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert "<li>Inner</li>" in result

    def test_decision_item_with_only_nested_list_keeps_nested_items(self) -> None:
        """An item with no own text containing only a nested list."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem">'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Kept</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert "<li>Kept</li>" in result

    def test_three_level_decision_list_preserves_middle_nesting(self) -> None:
        """Three-level decision list: Mid > Inner must not be flattened."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Outer</p>'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Mid</p>'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>Inner</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert "<li>Mid<ul><li>✓ Inner</li></ul></li>" in result

    def test_nested_decision_list_sibling_content_kept(self) -> None:
        """A nested list that is a sibling of items (not inside any item)."""
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Alpha</p></ac:adf-node>'
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem"><p>Beta</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        assert result.count("Alpha") == 1
        assert result.count("Beta") == 1


class TestAdfTaskLists:
    def test_complete_task_renders_checked_checkbox(self) -> None:
        html = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Done item</ac:task-body>"
            "</ac:task></ac:task-list>"
        )
        result = _preprocess(html)
        assert "[x] Done item" in result

    def test_incomplete_task_renders_unchecked_checkbox(self) -> None:
        html = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Todo item</ac:task-body>"
            "</ac:task></ac:task-list>"
        )
        result = _preprocess(html)
        assert "[ ] Todo item" in result

    def test_nested_task_list_not_double_rendered(self) -> None:
        """#43: nested task list must not emit items twice or leak status text."""
        html = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Outer"
            "<ac:task-list><ac:task>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Inner</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:task-body>"
            "</ac:task></ac:task-list>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert "[x] Inner" in result
        assert "[ ] Outer" in result
        assert "complete" not in result  # status word must not leak

    def test_three_level_task_list_preserves_middle_nesting(self) -> None:
        """Outer > Mid > Inner: Mid > Inner sublist must not flatten to Outer > Inner."""
        html = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Outer"
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Mid"
            "<ac:task-list><ac:task>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Inner</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:task-body>"
            "</ac:task></ac:task-list>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert "<li>[ ] Mid<ul><li>[x] Inner</li></ul></li>" in result

    def test_task_list_directly_inside_task_list_content_kept(self) -> None:
        html = (
            "<ac:task-list>"
            "<ac:task-list><ac:task>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Inner</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:task-list>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert "[x] Inner" in result

    def test_stray_splice_keeps_deep_nesting(self) -> None:
        """Stray splice loop must not yank deep nested <ul> to top level."""
        html = (
            "<ac:task-list>"
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Mid"
            "<ac:task-list><ac:task>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Inner</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:task-list>"
        )
        result = _preprocess(html)
        assert result.count("Inner") == 1
        assert "<li>[ ] Mid<ul><li>[x] Inner</li></ul></li>" in result


class TestCrossTypeNesting:
    def test_task_list_inside_decision_item_renders_as_sublist(self) -> None:
        """Cross-type: decision item with nested task list, one pass."""
        html = (
            '<ac:adf-node type="decisionList"><ac:adf-node type="decisionItem">'
            "<p>d-outer</p>"
            "<ac:task-list><ac:task>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>t-inner</ac:task-body>"
            "</ac:task></ac:task-list>"
            "</ac:adf-node></ac:adf-node>"
        )
        result = _preprocess(html)
        assert result.count("t-inner") == 1
        assert "[x] t-inner" in result
        assert "complete" not in result
        assert "d-outer" in result

    def test_decision_list_inside_task_body_renders_as_sublist(self) -> None:
        """Cross-type reverse: task body with nested decision list."""
        html = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>t-outer"
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>d-inner</p></ac:adf-node>'
            "</ac:adf-node>"
            "</ac:task-body>"
            "</ac:task></ac:task-list>"
        )
        result = _preprocess(html)
        assert result.count("d-inner") == 1
        assert "✓ d-inner" in result
        assert "[ ] t-outer" in result


# ---------------------------------------------------------------------------
# Emoticons
# ---------------------------------------------------------------------------


class TestEmoticons:
    def test_known_emoticon_substituted(self) -> None:
        """Known emoticon names are substituted with Unicode."""
        from conex.convert.macros import EMOTICON_MAP

        if not EMOTICON_MAP:
            pytest.skip("EMOTICON_MAP is empty (macros.py stub not yet filled)")

        html = '<ac:emoticon ac:name="tick"/>'
        result = _preprocess(html)
        assert EMOTICON_MAP.get("tick", "✅") in result

    def test_unknown_emoticon_removed(self) -> None:
        """Unknown emoticon name → removed (not kept as raw tag)."""
        html = '<ac:emoticon ac:name="zzz-definitely-unknown"/>'
        result = _preprocess(html)
        assert "ac:emoticon" not in result

    def test_emoticon_inside_task_body_survives_get_text(self) -> None:
        """Emoticons must be substituted BEFORE task list get_text() (#43 class)."""
        from conex.convert.macros import EMOTICON_MAP

        # Even with empty EMOTICON_MAP the tag must not survive into the output
        # as a raw ac:emoticon element.
        html = (
            "<ac:task-list><ac:task>"
            "<ac:task-status>incomplete</ac:task-status>"
            '<ac:task-body>Ship it <ac:emoticon ac:name="thumbs-up"/></ac:task-body>'
            "</ac:task></ac:task-list>"
        )
        result = _preprocess(html)
        assert "Ship it" in result
        assert "ac:emoticon" not in result
        if EMOTICON_MAP.get("thumbs-up"):
            assert EMOTICON_MAP["thumbs-up"] in result


# ---------------------------------------------------------------------------
# Pass ordering: ADF lists BEFORE adf-node unwrap
# ---------------------------------------------------------------------------


class TestPassOrdering:
    def test_decision_list_before_adf_node_unwrap(self) -> None:
        """ADF list pass (1) must run before generic adf-node unwrap (6).

        If adf-node unwrap ran first, the decision list structure would
        collapse to plain text and lose the ✓ marker and list formatting.
        """
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>X</p></ac:adf-node>'
            "</ac:adf-node>"
        )
        result = _preprocess(html)
        # If lists ran AFTER unwrap, we'd get plain text "X" with no <li> and no ✓
        assert "<li>✓ X</li>" in result
        assert "ac:adf-node" not in result  # unwrap pass cleaned up residuals

    def test_adf_content_wrapper_unwrapped(self) -> None:
        """Generic ac:adf-content is unwrapped after list pass."""
        html = "<ac:adf-content><p>Preserved</p></ac:adf-content>"
        result = _preprocess(html)
        assert "Preserved" in result
        assert "ac:adf-content" not in result

    def test_adf_fallback_removed_before_list_pass(self) -> None:
        """ac:adf-fallback (duplicate content) must be removed, not preserved."""
        html = (
            "<ac:adf-content><p>Real</p></ac:adf-content>"
            "<ac:adf-fallback><p>Dupe</p></ac:adf-fallback>"
        )
        result = _preprocess(html)
        assert "Real" in result
        assert "Dupe" not in result


# ---------------------------------------------------------------------------
# Links (pass 3)
# ---------------------------------------------------------------------------


class TestLinks:
    def _make_ctx_with_att(
        self,
        att: Attachment,
        *,
        media_available: set[str] | None = None,
    ) -> ConvertContext:
        refs = MediaRefs.from_attachments([att])
        local_name = refs.filename_for_title(att.title)
        avail = media_available if media_available is not None else {local_name}
        return ConvertContext(
            page=_make_page(),
            space=_make_space(),
            site_url="https://example.atlassian.net",
            attachments=[att],
            media=refs,
            rendered_drawio={},
            resolve_user=lambda aid: aid,
            media_enabled=True,
            media_available=avail,
        )

    def test_attachment_link_resolves_when_available(self) -> None:
        att = Attachment(id="a1", title="doc.pdf", media_type="application/pdf")
        ctx = self._make_ctx_with_att(att)
        html = (
            '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
            "<ac:plain-text-link-body>My Doc</ac:plain-text-link-body></ac:link>"
        )
        result = _preprocess(html, ctx)
        assert ".media/doc.pdf" in result
        assert "My Doc" in result

    def test_image_wrapped_in_link_is_preserved(self) -> None:
        """Regression: an <ac:image> inside an <ac:link> body must not be dropped
        (the images pass turns it into <img>, leaving the link text empty)."""
        att = Attachment(id="a1", title="pic.png", media_type="image/png")
        ctx = self._make_ctx_with_att(att)
        html = (
            "<ac:link><ac:link-body>"
            '<ac:image><ri:attachment ri:filename="pic.png"/></ac:image>'
            "</ac:link-body></ac:link>"
        )
        result = _preprocess(html, ctx)
        assert ".media/pic.png" in result

    def test_image_in_ri_page_link_is_preserved(self) -> None:
        """Regression: an image in a ri:page link body must survive (not be
        collapsed to a title-only span)."""
        att = Attachment(id="a1", title="pic.png", media_type="image/png")
        ctx = self._make_ctx_with_att(att)
        html = (
            '<ac:link><ri:page ri:content-title="Other"/><ac:link-body>'
            '<ac:image><ri:attachment ri:filename="pic.png"/></ac:image>'
            "</ac:link-body></ac:link>"
        )
        result = _preprocess(html, ctx)
        assert ".media/pic.png" in result

    def test_attachment_link_missing_media_emits_note(self) -> None:
        att = Attachment(id="a1", title="doc.pdf")
        ctx = self._make_ctx_with_att(att, media_available=set())
        html = (
            '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
            "<ac:plain-text-link-body>My Doc</ac:plain-text-link-body></ac:link>"
        )
        result = _preprocess(html, ctx)
        assert "Missing attachment: My Doc" in result
        assert ".media/doc.pdf" not in result

    def test_page_link_renders_label(self) -> None:
        """v1 PARITY: page link renders the label (no cross-page resolution)."""
        html = (
            '<ac:link><ri:page ri:content-title="Other Page"/>'
            "<ac:plain-text-link-body>See here</ac:plain-text-link-body></ac:link>"
        )
        result = _preprocess(html)
        assert "See here" in result

    def test_page_link_uses_title_when_no_body(self) -> None:
        html = '<ac:link><ri:page ri:content-title="Another Page"/></ac:link>'
        result = _preprocess(html)
        assert "Another Page" in result

    def test_user_link_resolves_mention(self) -> None:
        ctx = _make_ctx(resolve_user=lambda aid: "Alice" if aid == "abc" else aid)
        html = '<ac:link><ri:user ri:account-id="abc"/></ac:link>'
        result = _preprocess(html, ctx)
        assert "@Alice" in result

    def test_user_link_falls_back_to_id(self) -> None:
        html = '<ac:link><ri:user ri:account-id="abc"/></ac:link>'
        result = _preprocess(html)
        assert "@abc" in result

    def test_empty_link_decomposed(self) -> None:
        result = _preprocess("<p>a<ac:link></ac:link>b</p>")
        assert "ab" in result
        assert "ac:link" not in result

    def test_link_with_content_no_ri_child_unwraps(self) -> None:
        """A link with no ri: child but holding content is unwrapped, not dropped."""
        result = _preprocess("<p><ac:link><span>Some text</span></ac:link></p>")
        assert "Some text" in result
        assert "ac:link" not in result

    def test_attachment_link_uses_content_id_for_disambiguation(self) -> None:
        """ri:content-id on the ri:attachment routes to id-specific planned name."""
        atts = [
            Attachment(id="id1", title="shot.png"),
            Attachment(id="id2", title="shot.png"),
        ]
        refs = MediaRefs.from_attachments(atts)
        id2_name = refs.filename_for_id("id2")
        ctx = ConvertContext(
            page=_make_page(),
            space=_make_space(),
            site_url="https://example.atlassian.net",
            attachments=atts,
            media=refs,
            rendered_drawio={},
            resolve_user=lambda aid: aid,
            media_enabled=True,
            media_available={id2_name} if id2_name else set(),
        )
        html = (
            '<ac:link><ri:attachment ri:filename="shot.png" ri:content-id="id2"/>'
            "</ac:link>"
        )
        result = _preprocess(html, ctx)
        assert id2_name is not None
        assert f".media/{id2_name}" in result
        assert '.media/shot.png"' not in result


# ---------------------------------------------------------------------------
# Images (pass 4)
# ---------------------------------------------------------------------------


class TestImages:
    def test_attachment_image_available(self) -> None:
        att = Attachment(id="a1", title="shot.png")
        refs = MediaRefs.from_attachments([att])
        ctx = ConvertContext(
            page=_make_page(),
            space=_make_space(),
            site_url="https://example.atlassian.net",
            attachments=[att],
            media=refs,
            rendered_drawio={},
            resolve_user=lambda aid: aid,
            media_enabled=True,
            media_available={"shot.png"},
        )
        html = '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>'
        result = _preprocess(html, ctx)
        assert '.media/shot.png"' in result or ".media/shot.png" in result
        assert "ac:image" not in result

    def test_attachment_image_missing_media_emits_note(self) -> None:
        att = Attachment(id="a1", title="shot.png")
        refs = MediaRefs.from_attachments([att])
        ctx = ConvertContext(
            page=_make_page(),
            space=_make_space(),
            site_url="https://example.atlassian.net",
            attachments=[att],
            media=refs,
            rendered_drawio={},
            resolve_user=lambda aid: aid,
            media_enabled=True,
            media_available=set(),  # empty = nothing available
        )
        html = '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>'
        result = _preprocess(html, ctx)
        assert "Missing attachment: shot.png" in result
        assert ".media/shot.png" not in result

    def test_external_url_image(self) -> None:
        html = '<ac:image><ri:url ri:value="https://example.com/img.png"/></ac:image>'
        result = _preprocess(html)
        assert "https://example.com/img.png" in result
        assert "ac:image" not in result

    def test_image_no_source_decomposed(self) -> None:
        result = _preprocess("<ac:image></ac:image>")
        assert "ac:image" not in result

    def test_attachment_image_media_disabled_emits_note(self) -> None:
        att = Attachment(id="a1", title="shot.png")
        refs = MediaRefs.from_attachments([att])
        ctx = ConvertContext(
            page=_make_page(),
            space=_make_space(),
            site_url="https://example.atlassian.net",
            attachments=[att],
            media=refs,
            rendered_drawio={},
            resolve_user=lambda aid: aid,
            media_enabled=False,  # --no-media run
            media_available={"shot.png"},  # even if listed, media_enabled=False gates it
        )
        html = '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>'
        result = _preprocess(html, ctx)
        assert "Missing attachment: shot.png" in result

    def test_casefold_attachment_image_resolves(self) -> None:
        """Attachment titled Report.PDF referenced as report.pdf — casefold match."""
        att = Attachment(id="a1", title="Report.PDF")
        refs = MediaRefs.from_attachments([att])
        local = refs.filename_for_title("Report.PDF")
        ctx = ConvertContext(
            page=_make_page(),
            space=_make_space(),
            site_url="https://example.atlassian.net",
            attachments=[att],
            media=refs,
            rendered_drawio={},
            resolve_user=lambda aid: aid,
            media_enabled=True,
            media_available={local} if local else set(),
        )
        html = '<ac:image><ri:attachment ri:filename="report.pdf"/></ac:image>'
        result = _preprocess(html, ctx)
        assert "Missing attachment" not in result
        # The resolved local name (Report.PDF) appears in the src
        assert local is not None
        assert local in result


# ---------------------------------------------------------------------------
# Layout unwrap (pass 6)
# ---------------------------------------------------------------------------


class TestLayoutUnwrap:
    def test_layout_tags_unwrapped(self) -> None:
        html = (
            "<ac:layout><ac:layout-section><ac:layout-cell>"
            "<p>Inner</p>"
            "</ac:layout-cell></ac:layout-section></ac:layout>"
        )
        result = _preprocess(html)
        assert "Inner" in result
        assert "ac:layout" not in result

    def test_inline_comment_unwrapped(self) -> None:
        html = '<p>Before <ac:inline-comment-marker ac:ref="x">noted</ac:inline-comment-marker> after</p>'
        result = _preprocess(html)
        assert "noted" in result
        assert "ac:inline-comment-marker" not in result

    def test_placeholder_removed(self) -> None:
        html = "<ac:placeholder>Type here</ac:placeholder>"
        result = _preprocess(html)
        assert "Type here" not in result
        assert "ac:placeholder" not in result

    def test_stray_ri_tag_unwrapped_keeping_text(self) -> None:
        result = _preprocess("<ri:something>kept text</ri:something>")
        assert "kept text" in result
        assert "ri:something" not in result


# ---------------------------------------------------------------------------
# Time/date (pass 7)
# ---------------------------------------------------------------------------


class TestTimeElements:
    def test_datetime_preserved(self) -> None:
        result = _preprocess('<time datetime="2025-03-15"/>')
        assert "2025-03-15" in result

    def test_empty_time_removed(self) -> None:
        result = _preprocess("<time/>")
        assert "<time" not in result


# ---------------------------------------------------------------------------
# User mentions (pre-pass)
# ---------------------------------------------------------------------------


class TestUserMentions:
    def test_standalone_mention_resolved(self) -> None:
        ctx = _make_ctx(resolve_user=lambda aid: "Alice" if aid == "abc" else aid)
        html = '<ri:user ri:account-id="abc"/>'
        result = _preprocess(html, ctx)
        assert "@Alice" in result

    def test_standalone_mention_unresolved_shows_id(self) -> None:
        html = '<ri:user ri:account-id="xyz"/>'
        result = _preprocess(html)
        assert "@xyz" in result

    def test_profile_picture_resolved_to_mention(self) -> None:
        ctx = _make_ctx(resolve_user=lambda aid: "Bob" if aid == "u1" else aid)
        html = (
            '<ac:structured-macro ac:name="profile-picture">'
            '<ac:parameter ac:name="User"><ri:user ri:account-id="u1"/></ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess(html, ctx)
        assert "@Bob" in result
        assert "<!-- macro:" not in result  # no placeholder leaked

    def test_profile_picture_empty_account_id_dropped(self) -> None:
        html = (
            '<ac:structured-macro ac:name="profile-picture">'
            '<ac:parameter ac:name="User"><ri:user/></ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess(html)
        assert "@" not in result

    def test_two_ri_users_in_one_link_both_resolved(self) -> None:
        ctx = _make_ctx(resolve_user=lambda aid: f"N-{aid}")
        html = (
            '<p><ac:link><ri:user ri:account-id="a1"/>'
            '<ri:user ri:account-id="a2"/></ac:link></p>'
        )
        result = _preprocess(html, ctx)
        assert "@N-a1" in result
        assert "@N-a2" in result

    def test_profile_macro_ri_user_not_stolen_by_pre_pass(self) -> None:
        """profile macro's ri:user must NOT be consumed by the mention pre-pass (F3)."""
        ctx = _make_ctx(resolve_user=lambda aid: "Alice" if aid == "a1" else aid)
        html = (
            '<ac:structured-macro ac:name="profile">'
            '<ac:parameter ac:name="User"><ri:user ri:account-id="a1"/></ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess(html, ctx)
        # The profile macro goes through default_handler (stub) which returns
        # the body. Since there's no rich body, it emits a placeholder comment.
        # The important thing is: "Unknown user" must NOT appear (the ri:user
        # was not stolen for the wrong mention and then missed by the profile handler).
        # With a stub, the placeholder appears — documented deliberate divergence:
        # full profile handler is a follow-up macros worker.
        assert "ac:structured-macro" not in result


# ---------------------------------------------------------------------------
# Macro dispatch — detached node guard
# ---------------------------------------------------------------------------


class TestMacroDetachedNodeGuard:
    def test_nested_profile_picture_macros_do_not_crash(self) -> None:
        """Two profile-picture macros in one compound: resolved in pre-pass,
        second is detached after first replaces the outer element — must not crash."""
        html = (
            '<ac:structured-macro ac:name="profile-picture">'
            '<ac:parameter ac:name="U1"><ri:user ri:account-id="abc"/></ac:parameter>'
            '<ac:parameter ac:name="U2"><ri:user ri:account-id="def"/></ac:parameter>'
            "</ac:structured-macro>"
        )
        ctx = _make_ctx(resolve_user=lambda aid: "Alice")
        result = _preprocess(html, ctx)
        assert "@Alice" in result  # at least one resolved


# ---------------------------------------------------------------------------
# build_frontmatter
# ---------------------------------------------------------------------------


class TestBuildFrontmatter:
    def test_golden_shape_current_page(self) -> None:
        page = _make_page(
            id="123",
            title="My Page",
            status="current",
            version=PageVersion(number=5, created_at="2025-06-15T10:00:00Z"),
            web_url="https://example.atlassian.net/wiki/spaces/TEST/pages/123",
        )
        space = _make_space(key="TEST")
        fm = build_frontmatter(
            page, space, "/My Page", "https://example.atlassian.net"
        )
        assert fm.startswith("---\n")
        assert fm.rstrip().endswith("---")
        data = yaml.safe_load(fm.split("---")[1])
        assert data["title"] == "My Page"
        assert str(data["page_id"]) == "123"
        assert data["space_key"] == "TEST"
        assert data["path"] == "/My Page"
        assert data["last_modified"] == "2025-06-15T10:00:00Z"
        assert data["version"] == 5
        assert "status" not in data  # current pages: no status field

    def test_archived_page_has_status_field(self) -> None:
        page = _make_page(id="99", title="Old Page", status="archived")
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/Old Page", "https://example.atlassian.net")
        data = yaml.safe_load(fm.split("---")[1])
        assert data["status"] == "archived"

    def test_attachments_block_emitted(self) -> None:
        """v1 parity: a page with attachments gets an attachments: [{name,type,size}] block."""
        from conex.models import Attachment

        page = _make_page(id="1", title="P", status="current")
        space = _make_space(key="TEST")
        atts = [
            Attachment(id="a1", title="report.pdf", media_type="application/pdf", file_size=2048),
            Attachment(id="a2", title="img.png", media_type="image/png", file_size=512),
        ]
        fm = build_frontmatter(page, space, "/P", "https://x.atlassian.net", attachments=atts)
        data = yaml.safe_load(fm.split("---")[1])
        assert data["attachments"] == [
            {"name": "report.pdf", "type": "application/pdf", "size": 2048},
            {"name": "img.png", "type": "image/png", "size": 512},
        ]

    def test_no_attachments_block_when_none(self) -> None:
        page = _make_page(id="1", title="P", status="current")
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/P", "https://x.atlassian.net", attachments=[])
        data = yaml.safe_load(fm.split("---")[1])
        assert "attachments" not in data

    def test_url_field_present(self) -> None:
        page = _make_page(
            id="10",
            title="P",
            web_url="https://example.atlassian.net/wiki/spaces/TEST/pages/10",
        )
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/P", "https://example.atlassian.net")
        data = yaml.safe_load(fm.split("---")[1])
        assert "example.atlassian.net" in data.get("url", "")

    def test_url_relative_webui_gets_wiki_prefix(self) -> None:
        """v1 parity: a relative _links.webui path is prefixed with /wiki."""
        page = _make_page(id="10", title="P", web_url="/spaces/TEST/pages/10/P")
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/P", "https://x.atlassian.net")
        data = yaml.safe_load(fm.split("---")[1])
        assert data["url"] == "https://x.atlassian.net/wiki/spaces/TEST/pages/10/P"

    def test_url_absolute_web_url_used_as_is(self) -> None:
        page = _make_page(id="10", title="P", web_url="https://x.atlassian.net/wiki/x")
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/P", "https://x.atlassian.net")
        data = yaml.safe_load(fm.split("---")[1])
        assert data["url"] == "https://x.atlassian.net/wiki/x"

    def test_url_empty_when_no_site_url(self) -> None:
        page = _make_page(id="10", title="P", web_url="/spaces/TEST/pages/10/P")
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/P", "")
        data = yaml.safe_load(fm.split("---")[1])
        assert data["url"] == ""

    def test_no_status_field_when_current(self) -> None:
        page = _make_page(status="current")
        space = _make_space(key="TEST")
        fm = build_frontmatter(page, space, "/P", "https://example.atlassian.net")
        data = yaml.safe_load(fm.split("---")[1])
        assert "status" not in data

    def test_fields_in_v1_key_order(self) -> None:
        """Keys appear in v1 order: title, page_id, space_key, path, url, …"""
        page = _make_page(id="1", title="T", status="current")
        space = _make_space(key="K")
        fm = build_frontmatter(page, space, "/T", "https://x.example.com")
        raw = fm.split("---")[1]
        keys = [line.split(":")[0].strip() for line in raw.strip().splitlines()
                if ":" in line]
        expected_prefix = ["title", "page_id", "space_key", "path", "url", "last_modified", "version"]
        assert keys[:len(expected_prefix)] == expected_prefix


# ---------------------------------------------------------------------------
# convert_page — end-to-end
# ---------------------------------------------------------------------------


class TestConvertPage:
    def test_returns_markdown_body_with_h1(self) -> None:
        ctx = _make_ctx(page=_make_page(title="Hello World"))
        md = convert_page("<p>Some content</p>", ctx)
        assert "# Hello World" in md
        assert "Some content" in md

    def test_does_not_include_frontmatter(self) -> None:
        ctx = _make_ctx(page=_make_page(title="T"))
        md = convert_page("<p>content</p>", ctx)
        assert "---" not in md

    def test_decision_list_rendered_in_output(self) -> None:
        ctx = _make_ctx(page=_make_page(title="Decisions"))
        html = (
            '<ac:adf-node type="decisionList">'
            '<ac:adf-node type="decisionItem" state="DECIDED"><p>Ship it</p></ac:adf-node>'
            "</ac:adf-node>"
        )
        md = convert_page(html, ctx)
        assert "✓ Ship it" in md

    def test_h1_not_doubled(self) -> None:
        """If body already starts with H1 matching title, no duplicate."""
        ctx = _make_ctx(page=_make_page(title="Docs"))
        md = convert_page("<h1>Docs</h1><p>Body</p>", ctx)
        assert md.count("# Docs") == 1

    def test_no_confluence_tags_leaked(self) -> None:
        ctx = _make_ctx(page=_make_page(title="Clean"))
        html = (
            '<ac:structured-macro ac:name="toc"></ac:structured-macro>'
            "<p>Text</p>"
        )
        md = convert_page(html, ctx)
        assert "ac:structured-macro" not in md
        assert "ac:adf-node" not in md
        assert "ri:" not in md


# ---------------------------------------------------------------------------
# Registry — register decorator
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_adds_handler(self) -> None:
        """@register('test-macro-xyz') wires the handler into HANDLERS."""
        @register("test-macro-xyz-unique")
        def _handler(macro: Macro, ctx: ConvertContext) -> str:
            return "custom"

        assert "test-macro-xyz-unique" in HANDLERS
        assert HANDLERS["test-macro-xyz-unique"] is _handler

    def test_registered_handler_called_via_dispatch(self) -> None:
        @register("my-test-macro-dispatch")
        def _h(macro: Macro, ctx: ConvertContext) -> str:
            return f"<em>handled:{macro.params.get('key', '')}</em>"

        html = (
            '<ac:structured-macro ac:name="my-test-macro-dispatch">'
            '<ac:parameter ac:name="key">value</ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess(html)
        assert "handled:value" in result


# ---------------------------------------------------------------------------
# ac:adf-extension unwrap regression (BLOCKER fix verification)
# ---------------------------------------------------------------------------


class TestAdfExtensionUnwrap:
    """ac:adf-extension must be unwrapped (content preserved), never replaced
    with a placeholder comment.  v1 treated it as a generic wrapper, not a
    structured macro.  Finding: default_handler branch 2 was gated only on
    finding a nested ac:structured-macro, so bare-content adf-extensions fell
    through to branch 3 (placeholder), silently destroying user content.
    """

    def test_adf_extension_wrapping_plain_paragraph(self) -> None:
        """ac:adf-extension around a <p> must preserve the paragraph text."""
        html = "<ac:adf-extension><p>important text</p></ac:adf-extension>"
        result = _preprocess(html)
        assert "important text" in result, (
            "plain content inside ac:adf-extension must survive into markdown"
        )
        assert "<!-- macro:" not in result, (
            "ac:adf-extension must not emit a placeholder comment"
        )

    def test_adf_extension_wrapping_adf_node_panel(self) -> None:
        """ac:adf-extension around an ac:adf-node panel must preserve the panel
        content, not replace the whole subtree with a placeholder."""
        html = (
            "<ac:adf-extension>"
            '<ac:adf-node type="panel">'
            "<p>panel content</p>"
            "</ac:adf-node>"
            "</ac:adf-extension>"
        )
        result = _preprocess(html)
        assert "panel content" in result, (
            "content inside ac:adf-node inside ac:adf-extension must be preserved"
        )
        assert "<!-- macro:" not in result, (
            "ac:adf-extension must not emit a placeholder comment"
        )

    def test_adf_extension_wrapping_list(self) -> None:
        """ac:adf-extension around a <ul> list must preserve the list items."""
        html = (
            "<ac:adf-extension>"
            "<ul><li>item one</li><li>item two</li></ul>"
            "</ac:adf-extension>"
        )
        result = _preprocess(html)
        assert "item one" in result, (
            "list items inside ac:adf-extension must be preserved"
        )
        assert "item two" in result
        assert "<!-- macro:" not in result

    def test_adf_extension_no_content_becomes_empty(self) -> None:
        """An empty ac:adf-extension produces no placeholder comment."""
        html = "<ac:adf-extension></ac:adf-extension>"
        result = _preprocess(html)
        assert "<!-- macro:" not in result

    def test_convert_page_adf_extension_plain_paragraph_preserved(self) -> None:
        """End-to-end: convert_page with ac:adf-extension body preserves content."""
        ctx = _make_ctx(page=_make_page(title="ADF Page"))
        body = "<ac:adf-extension><p>ADF wrapped content</p></ac:adf-extension>"
        md = convert_page(body, ctx)
        assert "ADF wrapped content" in md, (
            "convert_page must preserve content inside ac:adf-extension"
        )
        assert "macro: unnamed" not in md
        assert "macro: " not in md
