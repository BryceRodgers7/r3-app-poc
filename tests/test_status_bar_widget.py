"""Tests for StatusBarWidget formatters (Phase 7.B)."""

from __future__ import annotations

import unittest

from app.core.app_state import UiState
from app.ui.status_bar_widget import _format_mmss, _format_replay_coverage


class FormatMmssTests(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(_format_mmss(0.0), "0:00")

    def test_under_one_minute(self) -> None:
        self.assertEqual(_format_mmss(42.0), "0:42")

    def test_exact_minute(self) -> None:
        self.assertEqual(_format_mmss(60.0), "1:00")

    def test_multiple_minutes(self) -> None:
        self.assertEqual(_format_mmss(155.0), "2:35")

    def test_truncates_subseconds(self) -> None:
        self.assertEqual(_format_mmss(42.9), "0:42")

    def test_negative_clamped_to_zero(self) -> None:
        self.assertEqual(_format_mmss(-1.0), "0:00")


class FormatReplayCoverageTests(unittest.TestCase):
    def test_unavailable_when_not_recording(self) -> None:
        # Defaults: replay_available=False, is_recording=False.
        s = UiState()
        self.assertEqual(
            _format_replay_coverage(s),
            "not available — start a game recording",
        )

    def test_unavailable_during_first_segment(self) -> None:
        # Recording active, but first segment hasn't finalized yet.
        s = UiState(is_recording=True)
        self.assertEqual(
            _format_replay_coverage(s),
            "not yet available — first segment finalizing",
        )

    def test_available_renders_range_and_lag(self) -> None:
        s = UiState(
            replay_available=True,
            latest_replayable_session_time_ns=42_000_000_000,
            replay_buffer_span_seconds=42.0,
            live_lag_behind_replayable_seconds=4.0,
        )
        self.assertEqual(
            _format_replay_coverage(s),
            "covers 0:00 – 0:42 (latest finalized −4s)",
        )

    def test_available_with_late_recording_start(self) -> None:
        # Recording started 30s into the session: latest=70s, span=40s,
        # earliest=30s.
        s = UiState(
            replay_available=True,
            latest_replayable_session_time_ns=70_000_000_000,
            replay_buffer_span_seconds=40.0,
            live_lag_behind_replayable_seconds=4.0,
        )
        self.assertEqual(
            _format_replay_coverage(s),
            "covers 0:30 – 1:10 (latest finalized −4s)",
        )

    def test_lag_rendering_rounds_to_seconds(self) -> None:
        # 7.6s lag rounds to "−8s" (default %.0f rounding).
        s = UiState(
            replay_available=True,
            latest_replayable_session_time_ns=12_000_000_000,
            replay_buffer_span_seconds=12.0,
            live_lag_behind_replayable_seconds=7.6,
        )
        self.assertIn("−8s", _format_replay_coverage(s))


if __name__ == "__main__":
    unittest.main()
