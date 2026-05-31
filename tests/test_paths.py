"""Tests for filesystem-safety helpers (S1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from confluence_export.paths import (
    is_safe_component,
    resolve_within,
    safe_attachment_name,
    safe_component,
)


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
    def test_never_returns_a_traversal_or_separator(self, raw):
        out = safe_component(raw)
        assert out not in {".", ".."}
        assert "/" not in out and "\\" not in out
        assert not out.startswith((".", "-"))
        assert is_safe_component(out)

    def test_empty_falls_back(self):
        assert safe_component("") == "attachment"
        assert safe_component(None) == "attachment"
        # all-stripped input (no word chars survive) → fallback
        assert safe_component("@@@", fallback="x") == "x"
        # all-dots is not empty but must still be a safe, non-dotfile component
        assert is_safe_component(safe_component("..."))

    def test_preserves_filesystem_safe_punctuation(self):
        assert safe_component("My Diagram (v2).png") == "My Diagram (v2).png"
        assert safe_component("data.final.xlsx") == "data.final.xlsx"

    def test_truncates_preserving_extension(self):
        out = safe_component("a" * 200 + ".png")
        assert len(out) <= 100
        assert out.endswith(".png")


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


class TestResolveWithin:
    def test_allows_safe_component(self, tmp_path):
        assert resolve_within(tmp_path, "ok.png") == (tmp_path / "ok.png").resolve()

    @pytest.mark.parametrize(
        "comp", ["../up.png", "/abs", "a/b", "..", ".", "a\\b", ""]
    )
    def test_rejects_escape(self, tmp_path, comp):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, comp)

    def test_symlinked_component_target_is_rejected(self, tmp_path):
        # A component that itself contains a separator is rejected up front;
        # this documents that resolve_within only accepts in-place leaf names.
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "nested/evil")
