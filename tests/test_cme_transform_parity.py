"""CME-derived markdown transformation parity tests.

Ports the transformation-focused test cases from confluence-markdown-exporter
(CME) and adapts them to conex's API. Each case below cites its source file in
CME (cloned at /tmp/cme-compare during development) so future drift can be
traced back.

Conex differs from CME in shape:

* CME exposes a ``Page.Converter`` instance that subclasses markdownify's
  converter; tests call ``converter.convert(html)`` or
  ``converter._helper(...)`` directly.
* Conex preprocesses Confluence storage HTML in ``_preprocess_html`` and runs
  markdownify with a fixed configuration. Helpers like
  ``_normalize_unicode_whitespace`` and ``_escape_template_placeholders`` are
  module-level functions.

To keep tests readable we provide a small ``_render(html)`` adapter that runs
the full conex transformation (preprocess + markdownify + post-pass), so tests
can compare end-to-end behavior similarly to CME's ``converter.convert(html)``.

Tests for behavior that conex currently does not implement are marked
``xfail`` with a clear reason in the ``docs/cme-markdown-transformation-gaps.md``
report rather than failing CI.
"""

from __future__ import annotations

import pytest
from markdownify import markdownify as md

from confluence_export.converter import (
    _escape_template_placeholders,
    _normalize_anchor_links,
    _normalize_anchor_slug,
    _normalize_unicode_whitespace,
    _pre_code_language,
    _preprocess_html,
)


def _render(html: str) -> str:
    """Run the full conex HTML→markdown pipeline (mimics CME's converter.convert)."""
    processed = _preprocess_html(html, [])
    out = md(
        processed,
        heading_style="ATX",
        bullets="*",
        strip=["script", "style"],
        code_language_callback=_pre_code_language,
    )
    out = _normalize_anchor_links(out)
    out = _escape_template_placeholders(out)
    return out


# =============================================================================
# Source: tests/unit/test_emoticon_conversion.py (CME)
# Conex stores Confluence emoticons as <ac:emoticon ac:name="..."/>. CME deals
# with rendered <img class="emoticon"> tags. We added _resolve_emoticon_img so
# the same rendered-HTML inputs work in conex too.
# =============================================================================
class TestEmoticonImgConversion:
    def test_atlassian_check_mark(self) -> None:
        html = (
            '<img class="emoticon emoticon-tick"'
            ' data-emoji-id="atlassian-check_mark"'
            ' data-emoji-fallback=":check_mark:"'
            ' data-emoji-shortname=":check_mark:"'
            ' alt="(tick)" />'
        )
        assert _render(html).strip() == "\u2705"

    def test_atlassian_cross_mark(self) -> None:
        html = (
            '<img class="emoticon emoticon-cross"'
            ' data-emoji-id="atlassian-cross_mark"'
            ' data-emoji-fallback=":cross_mark:"'
            ' alt="(error)" />'
        )
        assert _render(html).strip() == "\u274c"

    def test_unicode_emoji_by_hex_id(self) -> None:
        html = (
            '<img class="emoticon emoticon-blue-star"'
            ' data-emoji-id="1f6e0"'
            ' data-emoji-fallback="\U0001f6e0\ufe0f"'
            ' data-emoji-shortname=":tools:"'
            ' alt="(blue star)" />'
        )
        # CME prefers the direct Unicode fallback when present and not a :shortname:
        assert _render(html).strip() == "\U0001f6e0\ufe0f"

    def test_unicode_emoji_fallback_direct(self) -> None:
        html = (
            '<img class="emoticon"'
            ' data-emoji-id="1f600"'
            ' data-emoji-fallback="\U0001f600"'
            ' alt="smile" />'
        )
        assert _render(html).strip() == "\U0001f600"

    def test_custom_emoji_uuid_falls_back_to_shortname(self) -> None:
        html = (
            '<img class="emoticon emoticon-blue-star"'
            ' data-emoji-id="fb5b359f-23fa-44bd-872b-676e207eaaef"'
            ' data-emoji-fallback=":alert-1:"'
            ' data-emoji-shortname=":alert-1:"'
            ' alt="(blue star)" />'
        )
        assert _render(html).strip() == ":alert-1:"

    def test_non_emoticon_img_unchanged(self) -> None:
        html = '<img src="http://example.com/image.png" alt="photo" />'
        result = _render(html).strip()
        assert "emoticon" not in result
        assert "example.com" in result

    def test_emoticon_inline_in_text(self) -> None:
        html = (
            'Status: <img class="emoticon emoticon-tick"'
            ' data-emoji-id="atlassian-check_mark"'
            ' data-emoji-fallback=":check_mark:"'
            ' alt="(tick)" /> Done'
        )
        result = _render(html).strip()
        assert "\u2705" in result
        assert "Done" in result


# =============================================================================
# Source: tests/unit/test_nbsp_fix.py (CME)
# =============================================================================
class TestUnicodeWhitespacePreservation:
    def test_em_with_leading_nbsp(self) -> None:
        assert _render("<em>&nbsp;text</em>").strip() == "*text*"
        with_context = _render("word<em>&nbsp;text</em>").strip()
        assert "word *text*" in with_context or "word  *text*" in with_context

    def test_em_with_trailing_nbsp(self) -> None:
        assert _render("<em>text&nbsp;</em>").strip() == "*text*"
        with_context = _render("<em>text&nbsp;</em>word").strip()
        assert "*text* word" in with_context or "*text*  word" in with_context

    def test_em_with_both_nbsp(self) -> None:
        result = _render("word<em>&nbsp;text&nbsp;</em>end").strip()
        assert "*text*" in result
        assert "word *text* end" in result or "word  *text*  end" in result

    def test_strong_with_leading_nbsp(self) -> None:
        result = _render("word<strong>&nbsp;text</strong>").strip()
        assert "**text**" in result
        assert "word **text**" in result or "word  **text**" in result

    def test_strong_with_trailing_nbsp(self) -> None:
        result = _render("<strong>text&nbsp;</strong>word").strip()
        assert "**text**" in result
        assert "**text** word" in result or "**text**  word" in result

    def test_code_with_leading_nbsp(self) -> None:
        result = _render("word<code>&nbsp;text</code>").strip()
        assert "`text`" in result
        assert "word `text`" in result or "word  `text`" in result

    def test_code_with_trailing_nbsp(self) -> None:
        result = _render("<code>text&nbsp;</code>word").strip()
        assert "`text`" in result
        assert "`text` word" in result or "`text`  word" in result

    def test_i_tag_with_nbsp(self) -> None:
        result = _render("word<i>&nbsp;text</i>").strip()
        assert "*text*" in result
        assert "word *text*" in result or "word  *text*" in result

    def test_b_tag_with_nbsp(self) -> None:
        result = _render("word<b>&nbsp;text</b>").strip()
        assert "**text**" in result
        assert "word **text**" in result or "word  **text**" in result

    def test_real_world_confluence_example(self) -> None:
        result = _render("property<em>&nbsp;JungerRoot</em> .").strip()
        assert "property*JungerRoot*" not in result, "Space was lost!"
        assert "*JungerRoot*" in result
        assert "property" in result

    def test_multiple_nbsp_in_sequence(self) -> None:
        result = _render("word<em>&nbsp;&nbsp;text</em>").strip()
        # Multiple nbsp should remain spacing of some kind around emphasis
        assert "*text*" in result or "* text*" in result

    def test_mixed_whitespace(self) -> None:
        result = _render("see <em>figure 1</em> below").strip()
        assert "see *figure 1* below" in result

    def test_normalize_helper_function(self) -> None:
        assert "\xa0" in "\xa0text\xa0"
        normalized = _normalize_unicode_whitespace("\xa0text\xa0")
        assert "\xa0" not in normalized
        assert normalized.strip() == "text"
        assert normalized.startswith(" ")
        assert normalized.endswith(" ")

    def test_unicode_em_space(self) -> None:
        normalized = _normalize_unicode_whitespace("\u2003text")
        assert "\u2003" not in normalized
        assert normalized.strip() == "text"
        assert normalized.startswith(" ")

    def test_unicode_thin_space(self) -> None:
        normalized = _normalize_unicode_whitespace("text\u2009end")
        assert "\u2009" not in normalized
        assert normalized == "text end"

    def test_preserves_newlines_and_tabs(self) -> None:
        text = "text\nwith\nnewlines"
        assert _normalize_unicode_whitespace(text) == text

    def test_no_modification_when_no_unicode_whitespace(self) -> None:
        text = "normal text"
        assert _normalize_unicode_whitespace(text) == text


# =============================================================================
# Source: tests/unit/test_template_placeholders.py (CME)
# =============================================================================
class TestTemplatePlaceholderEscaping:
    def test_multi_word_placeholder_escaped(self) -> None:
        result = _escape_template_placeholders("Replace <medical device> here.")
        assert result == "Replace \\<medical device\\> here."

    def test_allcaps_placeholder_escaped(self) -> None:
        result = _escape_template_placeholders("Page: Literature Search Report: <TOPIC>")
        assert result == "Page: Literature Search Report: \\<TOPIC\\>"

    def test_complex_placeholder_escaped(self) -> None:
        text = "the <(e.g., clinical performance or state of the art)> of <medical device>."
        result = _escape_template_placeholders(text)
        assert "\\<(e.g., clinical performance or state of the art)\\>" in result
        assert "\\<medical device\\>" in result

    def test_placeholder_with_slash_in_name_escaped(self) -> None:
        result = _escape_template_placeholders("the <medical device/equivalent device> here")
        assert "\\<medical device/equivalent device\\>" in result

    def test_fake_closing_tag_placeholder_escaped(self) -> None:
        result = _escape_template_placeholders("use the </insert excerpt> function")
        assert "\\</insert excerpt\\>" in result

    def test_br_tag_preserved(self) -> None:
        result = _escape_template_placeholders("text<br/>more text")
        assert result == "text<br/>more text"

    def test_br_with_space_preserved(self) -> None:
        result = _escape_template_placeholders("text<br />more text")
        assert result == "text<br />more text"

    def test_br_uppercase_preserved(self) -> None:
        result = _escape_template_placeholders("text<BR/>more text")
        assert result == "text<BR/>more text"

    def test_closing_html_tag_preserved(self) -> None:
        result = _escape_template_placeholders("</div>")
        assert result == "</div>"

    def test_inline_code_not_modified(self) -> None:
        result = _escape_template_placeholders("Use `<TOPIC>` here.")
        assert result == "Use `<TOPIC>` here."

    def test_fenced_code_block_not_modified(self) -> None:
        text = "before\n```\n<TOPIC>\n<medical device>\n```\nafter"
        result = _escape_template_placeholders(text)
        assert "<TOPIC>" in result
        assert "<medical device>" in result
        assert "\\<TOPIC\\>" not in result

    def test_tilde_fenced_code_block_not_modified(self) -> None:
        text = "before\n~~~\n<TOPIC>\n~~~\nafter"
        result = _escape_template_placeholders(text)
        assert "<TOPIC>" in result

    def test_text_outside_code_block_still_escaped(self) -> None:
        text = "Replace <TOPIC> here.\n```\n<TOPIC>\n```\nAlso <medical device>."
        result = _escape_template_placeholders(text)
        lines = result.split("\n")
        assert "\\<TOPIC\\>" in lines[0]
        assert "<TOPIC>" in lines[2]
        assert "\\<medical device\\>" in lines[4]


# =============================================================================
# Source: tests/unit/test_confluence.py::TestAnchorLinkConversion (CME)
# Conex normalizes anchor slugs as a markdown post-pass; CME does it inside
# convert_a. Behavior should match for fragment-only links with default
# (non-wiki) settings.
# =============================================================================
class TestAnchorLinkConversion:
    def test_anchor_uses_href_not_link_text(self) -> None:
        html = '<a href="#1.-Request-Service">request service</a>'
        assert _render(html).strip() == "[request service](#1-request-service)"

    def test_anchor_plain_heading(self) -> None:
        html = '<a href="#My-Heading">My Heading</a>'
        assert _render(html).strip() == "[My Heading](#my-heading)"

    def test_anchor_with_numbers_and_punctuation(self) -> None:
        html = '<a href="#2.-Setup-Steps">setup steps</a>'
        assert _render(html).strip() == "[setup steps](#2-setup-steps)"

    def test_normalize_anchor_slug_helper(self) -> None:
        assert _normalize_anchor_slug("1.-Request-Service") == "1-request-service"
        assert _normalize_anchor_slug("My-Heading") == "my-heading"
        assert _normalize_anchor_slug("--leading-trailing--") == "leading-trailing"

    @pytest.mark.xfail(
        reason=(
            "Wiki-style anchor links ([[#Heading]]) need a configurable "
            "page_href setting; conex has no equivalent today. Tracked in "
            "docs/cme-markdown-transformation-gaps.md (Wiki-style links)."
        ),
        strict=True,
    )
    def test_wiki_anchor_uses_link_text(self) -> None:
        html = '<a href="#1.-Request-Service">Request Service</a>'
        # If a wiki mode were configured the expected output would be:
        assert _render(html).strip() == "[[#Request Service]]"


# =============================================================================
# Source: tests/unit/test_plantuml_conversion.py (CME)
# CME pulls the JSON from a separate ``page.editor2`` field; conex receives
# storage HTML that already contains ``<ac:plain-text-body>``, so we read the
# JSON in place. The behavior on the JSON payload itself matches.
# =============================================================================
class TestPlantUMLConversion:
    def test_convert_plantuml_basic(self) -> None:
        html = (
            '<ac:structured-macro ac:name="plantuml" ac:macro-id="m1">'
            '<ac:parameter ac:name="fileName">plantuml_test</ac:parameter>'
            '<ac:plain-text-body>'
            r'<![CDATA[{"umlDefinition":"@startuml\nAlice -> Bob: Hello\n@enduml"}]]>'
            "</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        result = _render(html)
        assert "```plantuml" in result
        assert "@startuml" in result
        assert "Alice -> Bob: Hello" in result
        assert "@enduml" in result

    def test_convert_plantuml_complex_diagram(self) -> None:
        uml = (
            "@startuml\\nskinparam backgroundColor white\\ntitle Test Diagram\\n\\n"
            "|Actor|\\nstart\\n:Action 1;\\n:Action 2;\\nstop\\n@enduml"
        )
        html = (
            '<ac:structured-macro ac:name="plantuml" ac:macro-id="m2">'
            '<ac:plain-text-body>'
            f'<![CDATA[{{"umlDefinition":"{uml}"}}]]>'
            "</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        result = _render(html)
        assert "```plantuml" in result
        assert "@startuml" in result
        assert "skinparam backgroundColor white" in result
        assert "title Test Diagram" in result
        assert "@enduml" in result

    def test_convert_plantuml_invalid_json(self) -> None:
        html = (
            '<ac:structured-macro ac:name="plantuml" ac:macro-id="m3">'
            '<ac:plain-text-body><![CDATA[{invalid json}]]></ac:plain-text-body>'
            "</ac:structured-macro>"
        )
        result = _render(html)
        assert "PlantUML diagram" in result
        assert "invalid JSON" in result

    def test_convert_plantuml_empty_body(self) -> None:
        html = (
            '<ac:structured-macro ac:name="plantuml" ac:macro-id="m4">'
            '<ac:plain-text-body></ac:plain-text-body>'
            "</ac:structured-macro>"
        )
        result = _render(html)
        assert "PlantUML diagram" in result
        assert "empty content" in result

    @pytest.mark.xfail(
        reason=(
            "CME also resolves the macro from a separate editor2 XML field by "
            "matching data-macro-id (rendered-HTML pages). Conex consumes "
            "storage XML directly, so this code path is not implemented. "
            "Tracked in docs/cme-markdown-transformation-gaps.md."
        ),
        strict=True,
    )
    def test_convert_plantuml_no_macro_id(self) -> None:
        # In CME, this test exercises the rendered <div data-macro-name="plantuml">
        # path with no macro-id, expecting the "no macro-id found" comment.
        html = '<div data-macro-name="plantuml"></div>'
        result = _render(html)
        assert "no macro-id found" in result
