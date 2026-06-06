"""Tests for filesystem-safety helpers (S1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from confluence_export.paths import (
    _with_suffix_token,
    drawio_render_name,
    is_safe_component,
    plan_attachment_names,
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

    def test_internal_manifest_name_is_reserved(self):
        assert safe_attachment_name(".versions.json") != ".versions.json"
        att = SimpleNamespace(id="att1", title=".versions.json")

        plan = plan_attachment_names([att])

        assert plan.for_attachment(att) != ".versions.json"

    @pytest.mark.parametrize(
        "raw", ["../../x.png", "/abs/x.png", "..", "a/b.png", "x\x00.png"]
    )
    def test_unsafe_names_sanitized(self, raw):
        out = safe_attachment_name(raw)
        assert is_safe_component(out)

    def test_collision_plan_is_order_independent(self):
        a = SimpleNamespace(id="att1", title="a/b.png")
        b = SimpleNamespace(id="att2", title="a-b.png")

        first = plan_attachment_names([a, b])
        second = plan_attachment_names([b, a])

        assert first.by_id == second.by_id
        assert first.for_attachment(a) == second.for_attachment(a)
        assert first.for_attachment(b) == second.for_attachment(b)
        assert len({first.for_attachment(a), first.for_attachment(b)}) == 2

    def test_collision_plan_keeps_older_attachment_on_bare_name(self):
        old = SimpleNamespace(id="att1", title="a/b.png", created_at="2024-01-01")
        new = SimpleNamespace(id="att2", title="a-b.png", created_at="2025-01-01")

        plan = plan_attachment_names([new, old])

        assert plan.for_attachment(old) == "a-b.png"
        assert plan.for_attachment(new) != "a-b.png"

    def test_collision_plan_handles_long_shared_id_prefixes(self):
        attachments = [
            SimpleNamespace(id=f"abcdefghijklmnop{i}", title=title)
            for i, title in zip(("A", "B", "C"), ("a/b.png", "a\\b.png", "a///b.png"))
        ]

        plan = plan_attachment_names(attachments)

        names = {plan.for_attachment(att) for att in attachments}
        assert len(names) == 3
        assert all(is_safe_component(name) for name in names)

    def test_collision_plan_distinguishes_duplicate_titles_without_ids(self):
        first = SimpleNamespace(id="", title="same.png")
        second = SimpleNamespace(id="", title="same.png")

        plan = plan_attachment_names([first, second])

        assert plan.for_attachment(first) != plan.for_attachment(second)
        assert len({plan.for_attachment(first), plan.for_attachment(second)}) == 2

    def test_no_id_duplicate_title_plan_uses_stable_identity_not_order(self):
        first = SimpleNamespace(id="", title="same.png", download_link="/wiki/b")
        second = SimpleNamespace(id="", title="same.png", download_link="/wiki/a")

        plan = plan_attachment_names([first, second])
        reversed_plan = plan_attachment_names([second, first])

        assert plan.for_attachment(first) == reversed_plan.for_attachment(first)
        assert plan.for_attachment(second) == reversed_plan.for_attachment(second)

    def test_no_id_identity_ignores_mutable_version_and_size(self):
        before = SimpleNamespace(
            id="", title="same.png", download_link="/wiki/a?version=1",
            file_size=10, version=SimpleNamespace(number=1),
        )
        other = SimpleNamespace(id="", title="same.png", download_link="/wiki/b")
        after = SimpleNamespace(
            id="", title="same.png", download_link="/wiki/a?version=2",
            file_size=20, version=SimpleNamespace(number=2),
        )

        before_plan = plan_attachment_names([before, other])
        after_plan = plan_attachment_names([after, other])

        assert before_plan.for_attachment(before) == after_plan.for_attachment(after)


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

    def test_symlinked_leaf_is_rejected_before_resolve(self, tmp_path):
        (tmp_path / "other.png").write_bytes(b"other")
        (tmp_path / "img.png").symlink_to(tmp_path / "other.png")

        with pytest.raises(ValueError, match="symlink"):
            resolve_within(tmp_path, "img.png")


class TestWithSuffixToken:
    def test_drops_extension_when_suffix_alone_exceeds_max_len(self):
        # A pathological name whose combined suffixes are longer than max_len:
        # there is no room to keep any extension, so the suffix is dropped and
        # the collision marker is still appended to a non-empty stem.
        name = "a" + ".x" * 60  # 120-char suffix, far over max_len
        out = _with_suffix_token(name, "tok")

        assert len(out) <= 100
        assert not out.endswith(".x")
        assert "-tok" in out
        assert is_safe_component(out)

    def test_keeps_extension_when_it_fits(self):
        out = _with_suffix_token("photo.png", "tok")

        assert out.endswith(".png")
        assert "-tok" in out


class TestDrawioRenderName:
    def test_uses_default_name_when_unreserved(self):
        assert drawio_render_name("diagram.drawio", "tok", set()) == "diagram.drawio.png"

    def test_appends_token_on_collision(self):
        out = drawio_render_name("diagram.drawio", "tok", {"diagram.drawio.png"})

        assert out != "diagram.drawio.png"
        assert is_safe_component(out)
        assert "tok" in out


class TestPlanAttachmentNamesEdges:
    def test_duplicate_attachment_id_is_deduplicated(self):
        # Two attachments share one id: the second is skipped entirely, so the
        # id maps to exactly one (the first-seen) filename.
        first = SimpleNamespace(id="dup", title="first.png")
        second = SimpleNamespace(id="dup", title="second.png")

        plan = plan_attachment_names([first, second])

        assert list(plan.by_id) == ["dup"]
        assert plan.by_id["dup"] == "first.png"
        assert plan.for_attachment(second) == "first.png"


class TestAttachmentNamePlanLookups:
    def test_for_attachment_falls_back_to_title_for_unplanned_object(self):
        planned = SimpleNamespace(id="att1", title="doc.png")
        plan = plan_attachment_names([planned])

        # A different, unplanned object (no id, distinct object identity) that
        # nonetheless shares the title resolves via the by_title fallback.
        other_same_title = SimpleNamespace(id="", title="doc.png")
        assert plan.for_attachment(other_same_title) == "doc.png"

    def test_for_attachment_sanitizes_unknown_title(self):
        plan = plan_attachment_names([SimpleNamespace(id="att1", title="doc.png")])

        unknown = SimpleNamespace(id="", title="../evil.png")
        out = plan.for_attachment(unknown)

        assert out not in plan.by_title.values()
        assert is_safe_component(out)

    def test_for_reference_prefers_attachment_id(self):
        a = SimpleNamespace(id="att1", title="a/b.png")
        plan = plan_attachment_names([a])

        # The id resolves regardless of the (mismatched) title passed in.
        assert plan.for_reference("does-not-matter", attachment_id="att1") == plan.by_id["att1"]
