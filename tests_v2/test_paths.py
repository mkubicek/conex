"""Tests for conex.paths — filesystem-safety helpers (S1 posture).

Coverage intent:
- sanitize_filename: page-dir sanitization semantics
- safe_component / is_safe_component: attachment component neutralization
- safe_attachment_name: passthrough-or-sanitize for attachment filenames
- truncate_with_suffix: layout collision suffix without exceeding cap
- resolve_within: containment assert (S1)
- nfc / nfc_casefold: Unicode normalization folds
- plan_attachment_names / AttachmentNamePlan: collision-safe allocation +
  for_reference by-id / by-title / folded-title resolution
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conex.paths import (
    MAX_FILENAME_LEN,
    AttachmentNamePlan,
    _with_suffix_token,
    assert_within,
    clone_or_copy,
    is_safe_component,
    nfc,
    nfc_casefold,
    plan_attachment_names,
    resolve_within,
    safe_attachment_name,
    safe_component,
    sanitize_filename,
    truncate_with_suffix,
)


# ---------------------------------------------------------------------------
# assert_within — full-path containment assert
# ---------------------------------------------------------------------------


class TestAssertWithin:
    def test_path_inside_root_returns_resolved(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b.txt"
        assert assert_within(tmp_path, target) == target.resolve()

    def test_nonexistent_path_inside_root_ok(self, tmp_path: Path) -> None:
        # resolve() must not require the path to exist.
        assert_within(tmp_path, tmp_path / "does" / "not" / "exist.md")

    def test_absolute_path_outside_root_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes root"):
            assert_within(tmp_path, Path("/etc/passwd"))

    def test_dotdot_traversal_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes root"):
            assert_within(tmp_path / "sub", tmp_path / "sub" / ".." / ".." / "x")

    def test_symlink_escaping_root_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "root"
        root.mkdir()
        link = root / "link"
        link.symlink_to(outside)
        # A target reached through the escaping symlink resolves outside root.
        with pytest.raises(ValueError, match="escapes root"):
            assert_within(root, link / "loot.txt")


# ---------------------------------------------------------------------------
# clone_or_copy — reflink with copy fallback
# ---------------------------------------------------------------------------


class TestCloneOrCopy:
    def test_produces_identical_bytes(self, tmp_path: Path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"hello world " * 1000)
        dst = tmp_path / "dst.bin"
        clone_or_copy(src, dst)
        assert dst.read_bytes() == src.read_bytes()

    def test_dst_is_independent_on_write(self, tmp_path: Path) -> None:
        # Whether reflinked (CoW) or plain-copied, writing dst must not change
        # src — the blob (src) is conex's immutable source of truth.
        src = tmp_path / "src.bin"
        original = b"original-content" * 100
        src.write_bytes(original)
        dst = tmp_path / "dst.bin"
        clone_or_copy(src, dst)
        dst.write_bytes(b"mutated")
        assert src.read_bytes() == original


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_plain_title_preserves_word_chars(self):
        assert sanitize_filename("Hello World") == "Hello-World"

    def test_strips_non_word_non_hyphen_non_space(self):
        result = sanitize_filename("My Page! (v2)")
        assert result == "My-Page-v2"

    def test_collapses_mixed_separators(self):
        assert sanitize_filename("a - b  c") == "a-b-c"

    def test_strips_leading_and_trailing_hyphens(self):
        assert sanitize_filename("---Hello---") == "Hello"

    def test_returns_untitled_for_empty(self):
        assert sanitize_filename("") == "untitled"

    def test_returns_untitled_for_all_stripped(self):
        # All non-word chars
        assert sanitize_filename("@@@!!!") == "untitled"

    def test_caps_at_max_filename_len(self):
        long_title = "a" * 200
        result = sanitize_filename(long_title)
        assert len(result) <= MAX_FILENAME_LEN

    def test_no_trailing_hyphen_after_truncation(self):
        # Truncation point should not leave a trailing hyphen
        result = sanitize_filename("a" * 99 + "-" + "b" * 10)
        assert not result.endswith("-")
        assert len(result) <= MAX_FILENAME_LEN

    def test_unicode_word_chars_kept(self):
        # \w includes Unicode letters
        result = sanitize_filename("über die Straße")
        assert "ber" in result or "über" in result  # depends on regex engine

    def test_result_is_never_a_dotfile_or_traversal(self):
        for title in (".", "..", ".hidden", "../evil"):
            result = sanitize_filename(title)
            assert result not in {".", ".."}
            assert not result.startswith(".")

    def test_hyphen_is_preserved(self):
        assert sanitize_filename("my-page") == "my-page"


# ---------------------------------------------------------------------------
# truncate_with_suffix
# ---------------------------------------------------------------------------

class TestTruncateWithSuffix:
    def test_short_segment_appends_suffix(self):
        result = truncate_with_suffix("hello", "-2")
        assert result == "hello-2"

    def test_long_segment_truncated_to_fit(self):
        long = "a" * 100
        result = truncate_with_suffix(long, "-42")
        assert len(result) <= MAX_FILENAME_LEN
        assert result.endswith("-42")

    def test_no_trailing_hyphen_in_truncated_base(self):
        # Base that ends in a hyphen at the truncation boundary
        segment = "a" * 97 + "-"
        result = truncate_with_suffix(segment, "-2")
        assert not result[:-2].endswith("-")

    def test_empty_base_uses_untitled(self):
        result = truncate_with_suffix("", "-2")
        assert result == "untitled-2"

    def test_all_hyphen_base_uses_untitled(self):
        result = truncate_with_suffix("----", "-3")
        assert result == "untitled-3"

    def test_result_length_exactly_at_cap(self):
        segment = "a" * 98
        result = truncate_with_suffix(segment, "-2")
        assert len(result) == MAX_FILENAME_LEN


# ---------------------------------------------------------------------------
# safe_component / is_safe_component
# ---------------------------------------------------------------------------

class TestSafeComponent:
    @pytest.mark.parametrize(
        "raw",
        [
            "../../../etc/passwd",
            "/etc/passwd",
            "..",
            ".",
            "a/b/c.png",
            "a\\b.png",
            "x\x00y.png",
            ".hidden",
            "-rf",
        ],
    )
    def test_never_returns_traversal_or_separator(self, raw):
        out = safe_component(raw)
        assert out not in {".", ".."}
        assert "/" not in out and "\\" not in out
        assert not out.startswith((".", "-"))
        assert is_safe_component(out)

    def test_empty_returns_fallback(self):
        assert safe_component("") == "attachment"
        assert safe_component(None) == "attachment"

    def test_custom_fallback(self):
        assert safe_component("@@@", fallback="x") == "x"

    def test_all_dots_sanitized_to_safe_component(self):
        result = safe_component("...")
        assert is_safe_component(result)

    def test_benign_names_preserved(self):
        assert safe_component("My Diagram (v2).png") == "My Diagram (v2).png"
        assert safe_component("data.final.xlsx") == "data.final.xlsx"

    def test_truncates_preserving_extension(self):
        out = safe_component("a" * 200 + ".png")
        assert len(out) <= MAX_FILENAME_LEN
        assert out.endswith(".png")

    def test_control_characters_stripped(self):
        out = safe_component("file\x01\x1fname.png")
        assert is_safe_component(out)
        assert "\x01" not in out and "\x1f" not in out


class TestIsSafeComponent:
    def test_safe_names(self):
        for name in ("ok.png", "My File (2).pdf", "résumé.docx", "a-b_c.txt"):
            assert is_safe_component(name)

    def test_unsafe_names(self):
        for name in ("", ".", "..", "../x", "/abs", "a/b", "a\\b", ".hidden", "-flag"):
            assert not is_safe_component(name)

    def test_control_char_is_unsafe(self):
        assert not is_safe_component("file\x00name")


# ---------------------------------------------------------------------------
# safe_attachment_name
# ---------------------------------------------------------------------------

class TestSafeAttachmentName:
    def test_benign_names_unchanged(self):
        for name in ("report.pdf", "My Diagram (v2).png", "résumé.docx", "a-b_c.txt"):
            assert safe_attachment_name(name) == name

    @pytest.mark.parametrize(
        "raw", ["../../x.png", "/abs/x.png", "..", "a/b.png", "x\x00.png"]
    )
    def test_unsafe_names_sanitized(self, raw):
        out = safe_attachment_name(raw)
        assert is_safe_component(out)

    def test_empty_string_returns_safe(self):
        out = safe_attachment_name("")
        assert is_safe_component(out)

    def test_none_returns_safe(self):
        out = safe_attachment_name(None)
        assert is_safe_component(out)

    def test_dotfile_is_sanitized(self):
        out = safe_attachment_name(".hidden")
        assert is_safe_component(out)
        assert not out.startswith(".")


# ---------------------------------------------------------------------------
# resolve_within
# ---------------------------------------------------------------------------

class TestResolveWithin:
    def test_allows_safe_component(self, tmp_path):
        result = resolve_within(tmp_path, "ok.png")
        assert result == (tmp_path / "ok.png").resolve()

    @pytest.mark.parametrize(
        "comp", ["../up.png", "/abs", "a/b", "..", ".", "a\\b", ""]
    )
    def test_rejects_escape(self, tmp_path, comp):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, comp)

    def test_rejects_nested_separator(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "nested/evil")

    def test_rejects_symlinked_leaf(self, tmp_path):
        (tmp_path / "other.png").write_bytes(b"other")
        (tmp_path / "img.png").symlink_to(tmp_path / "other.png")

        with pytest.raises(ValueError, match="symlink"):
            resolve_within(tmp_path, "img.png")

    def test_non_symlink_non_existent_component_accepted(self, tmp_path):
        # Component need not exist — it just must not be an actual symlink
        result = resolve_within(tmp_path, "newfile.png")
        assert str(result).startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# nfc / nfc_casefold
# ---------------------------------------------------------------------------

class TestNfc:
    def test_nfc_decomposes_and_recomposes(self):
        # é can be e + combining acute (NFD) or precomposed (NFC)
        nfd_e = "é"  # NFD form
        assert nfc(nfd_e) == "\xe9"  # NFC: single precomposed codepoint

    def test_nfc_idempotent(self):
        s = "café"
        assert nfc(nfc(s)) == nfc(s)

    def test_nfc_casefold_lowercases(self):
        assert nfc_casefold("CAFÉ") == "café"

    def test_nfc_casefold_normalizes_and_lowercases(self):
        nfd = "é"  # NFD e-acute
        assert nfc_casefold(nfd) == nfc_casefold("\xe9")

    def test_nfc_casefold_idempotent(self):
        s = "ÜBER"
        assert nfc_casefold(nfc_casefold(s)) == nfc_casefold(s)


# ---------------------------------------------------------------------------
# _with_suffix_token (internal; tested for correctness guarantees)
# ---------------------------------------------------------------------------

class TestWithSuffixToken:
    def test_keeps_extension_when_it_fits(self):
        out = _with_suffix_token("photo.png", "tok")
        assert out.endswith(".png")
        assert "-tok" in out

    def test_drops_extension_when_suffix_alone_exceeds_max_len(self):
        name = "a" + ".x" * 60  # 120-char suffix
        out = _with_suffix_token(name, "tok")
        assert len(out) <= MAX_FILENAME_LEN
        assert "-tok" in out
        assert is_safe_component(out)

    def test_result_within_max_len(self):
        out = _with_suffix_token("a" * 90 + ".png", "longtoken")
        assert len(out) <= MAX_FILENAME_LEN

    def test_retry_parameter_appends_counter(self):
        out0 = _with_suffix_token("file.png", "tok", retry=0)
        out1 = _with_suffix_token("file.png", "tok", retry=1)
        assert out0 != out1


# ---------------------------------------------------------------------------
# plan_attachment_names / AttachmentNamePlan
# ---------------------------------------------------------------------------

class TestPlanAttachmentNames:
    def test_no_collision_basic(self):
        a = SimpleNamespace(id="att1", title="report.pdf")
        b = SimpleNamespace(id="att2", title="photo.png")

        plan = plan_attachment_names([a, b])

        assert plan.by_id["att1"] == "report.pdf"
        assert plan.by_id["att2"] == "photo.png"

    def test_collision_produces_two_distinct_names(self):
        # Both titles sanitize to "a-b.png"
        a = SimpleNamespace(id="att1", title="a/b.png")
        b = SimpleNamespace(id="att2", title="a-b.png")

        plan = plan_attachment_names([a, b])

        names = {plan.by_id["att1"], plan.by_id["att2"]}
        assert len(names) == 2
        assert all(is_safe_component(n) for n in names)

    def test_collision_plan_is_stable_regardless_of_input_order(self):
        a = SimpleNamespace(id="att1", title="a/b.png")
        b = SimpleNamespace(id="att2", title="a-b.png")

        first = plan_attachment_names([a, b])
        second = plan_attachment_names([b, a])

        assert first.by_id == second.by_id

    def test_older_attachment_keeps_bare_name(self):
        # att1 has an earlier created_at → it should get the bare base name
        old = SimpleNamespace(id="att1", title="a/b.png", created_at="2024-01-01")
        new = SimpleNamespace(id="att2", title="a-b.png", created_at="2025-01-01")

        plan = plan_attachment_names([new, old])

        assert plan.by_id["att1"] == "a-b.png"
        assert plan.by_id["att2"] != "a-b.png"

    def test_duplicate_attachment_id_is_deduplicated(self):
        first = SimpleNamespace(id="dup", title="first.png")
        second = SimpleNamespace(id="dup", title="second.png")

        plan = plan_attachment_names([first, second])

        assert list(plan.by_id) == ["dup"]
        assert plan.by_id["dup"] == "first.png"

    def test_three_way_collision_all_distinct(self):
        atts = [
            SimpleNamespace(id=f"abcdefghijklmnop{i}", title=title)
            for i, title in zip(("A", "B", "C"), ("a/b.png", "a\\b.png", "a///b.png"))
        ]

        plan = plan_attachment_names(atts)

        names = {plan.by_id[att.id] for att in atts}
        assert len(names) == 3
        assert all(is_safe_component(n) for n in names)

    def test_empty_list_returns_empty_plan(self):
        plan = plan_attachment_names([])
        assert plan.by_id == {}
        assert plan.by_title == {}
        assert plan.by_folded_title == {}

    def test_no_id_attachments_get_by_title_entry(self):
        a = SimpleNamespace(id="", title="doc.pdf")

        plan = plan_attachment_names([a])

        assert "doc.pdf" in plan.by_title
        assert plan.by_title["doc.pdf"] == "doc.pdf"

    def test_no_id_duplicate_titles_both_planned(self):
        first = SimpleNamespace(id="", title="same.png")
        second = SimpleNamespace(id="", title="same.png")

        plan = plan_attachment_names([first, second])

        # by_title has exactly one entry for the shared title
        assert "same.png" in plan.by_title
        # All generated names are safe
        assert all(is_safe_component(n) for n in plan.by_title.values())


class TestAttachmentNamePlanForReference:
    def _simple_plan(self):
        a = SimpleNamespace(id="att1", title="a/b.png")  # sanitizes to a-b.png
        b = SimpleNamespace(id="att2", title="other.pdf")
        return plan_attachment_names([a, b])

    def test_by_id_takes_priority_over_title(self):
        plan = self._simple_plan()
        # Pass a title that doesn't match, but the id does
        result = plan.for_reference("does-not-matter", attachment_id="att1")
        assert result == plan.by_id["att1"]

    def test_exact_title_match(self):
        plan = self._simple_plan()
        result = plan.for_reference("other.pdf")
        assert result == "other.pdf"

    def test_nfc_casefold_title_match(self):
        # Create a plan with a title that has a casefold equivalent
        a = SimpleNamespace(id="att1", title="MyFile.PNG")
        plan = plan_attachment_names([a])

        # The exact folded key maps unambiguously
        result = plan.for_reference("myfile.png")
        # Should resolve via by_folded_title if the fold is unambiguous
        folded_key = nfc_casefold("MyFile.PNG")
        if folded_key in plan.by_folded_title:
            assert result == plan.by_folded_title[folded_key]
        else:
            assert is_safe_component(result)

    def test_fallback_sanitizes_unknown_title(self):
        a = SimpleNamespace(id="att1", title="doc.png")
        plan = plan_attachment_names([a])

        result = plan.for_reference("../evil.png")
        assert is_safe_component(result)

    def test_no_id_arg_falls_through_to_title(self):
        a = SimpleNamespace(id="att1", title="doc.png")
        plan = plan_attachment_names([a])

        result = plan.for_reference("doc.png", attachment_id=None)
        assert result == "doc.png"

    def test_unknown_id_falls_through_to_title(self):
        a = SimpleNamespace(id="att1", title="doc.png")
        plan = plan_attachment_names([a])

        result = plan.for_reference("doc.png", attachment_id="nonexistent")
        assert result == "doc.png"

    def test_for_reference_with_id_and_matching_title(self):
        a = SimpleNamespace(id="att1", title="a/b.png")
        plan = plan_attachment_names([a])

        # Both id and sanitized title known; id wins
        id_result = plan.for_reference("a-b.png", attachment_id="att1")
        title_result = plan.for_reference("a-b.png")
        assert id_result == title_result  # same name either way in this case


class TestAttachmentNamePlanByFoldedTitle:
    def test_unambiguous_fold_populates_by_folded_title(self):
        a = SimpleNamespace(id="att1", title="MyFile.PNG")
        plan = plan_attachment_names([a])

        folded = nfc_casefold("MyFile.PNG")
        assert folded in plan.by_folded_title

    def test_ambiguous_fold_not_in_by_folded_title(self):
        # Two different titles that casefold to the same key → ambiguous
        a = SimpleNamespace(id="att1", title="MYFILE.PNG")
        b = SimpleNamespace(id="att2", title="myfile.png")
        plan = plan_attachment_names([a, b])

        folded = nfc_casefold("MYFILE.PNG")
        assert folded not in plan.by_folded_title


# ---------------------------------------------------------------------------
# Integration: safe_attachment_name + plan consistency
# ---------------------------------------------------------------------------

class TestPlanAndSafeNameConsistency:
    def test_all_plan_names_are_safe_components(self):
        atts = [
            SimpleNamespace(id=f"id{i}", title=title)
            for i, title in enumerate([
                "normal.pdf",
                "../escape.png",
                "a/b/c.png",
                ".hidden",
                "-flag",
                "a" * 150 + ".png",
                "",
                "with spaces.docx",
                "UPPERCASE.PNG",
            ])
        ]

        plan = plan_attachment_names(atts)

        for name in plan.by_id.values():
            assert is_safe_component(name), f"not safe: {name!r}"

    def test_safe_attachment_name_and_plan_agree_on_simple_title(self):
        a = SimpleNamespace(id="att1", title="simple.pdf")
        plan = plan_attachment_names([a])

        assert plan.by_id["att1"] == safe_attachment_name("simple.pdf")

    def test_sanitize_filename_and_safe_attachment_name_differ_in_behavior(self):
        # sanitize_filename strips dots; safe_attachment_name preserves dotfiles via prefix
        dotname = ".hidden"
        page_result = sanitize_filename(dotname)
        att_result = safe_attachment_name(dotname)

        # Both must be safe, but via different means
        assert page_result != dotname or not page_result.startswith(".")
        assert is_safe_component(att_result)
