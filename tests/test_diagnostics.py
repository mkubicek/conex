"""Tests for the end-of-run warning collector and summary formatting."""

from __future__ import annotations

from confluence_export.diagnostics import WarningCollector, format_warning_summary


class TestWarningCollector:
    def test_records_and_counts_by_category(self):
        wc = WarningCollector()
        wc.record("a")
        wc.record("a")
        wc.record("b")
        assert wc.counts() == {"a": 2, "b": 1}

    def test_counts_returns_a_copy(self):
        wc = WarningCollector()
        wc.record("a")
        snapshot = wc.counts()
        snapshot["a"] = 99
        assert wc.counts() == {"a": 1}

    def test_total_sums_all_categories(self):
        wc = WarningCollector()
        assert wc.total == 0
        wc.record("a")
        wc.record("b")
        wc.record("b")
        assert wc.total == 3


class TestFormatWarningSummary:
    def test_empty_counts_yields_empty_string(self):
        assert format_warning_summary({}) == ""

    def test_orders_by_count_descending_then_name(self):
        summary = format_warning_summary({"rare": 1, "common": 5, "also-1": 1})
        # Most frequent first; ties broken alphabetically.
        assert summary == "7 warning(s): common ×5, also-1 ×1, rare ×1"

    def test_single_category(self):
        assert format_warning_summary({"x": 2}) == "2 warning(s): x ×2"
