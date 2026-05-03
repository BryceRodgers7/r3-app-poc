"""Phase 10.D — slow-disk runtime surface.

Two layers covered here:

1. `_evaluate_disk_throughput` — streak-based emission against a
   controllable disk-budget threshold.
2. `FeedMetricsSnapshot.record_queue_saturation_pct` — pure-function
   percentage derived from the existing depth/capacity fields.

The diagnostics-widget glyph is exercised through a dedicated unit
test on the helper, not by rendering the widget.
"""

from __future__ import annotations

import unittest

from app.core.health_events import HealthEventLog, HealthSeverity
import app.core.health_events as health_events
from app.core.telemetry import (
    DISK_SLOW_STREAK_THRESHOLD,
    DiskSnapshot,
    FeedMetricsSnapshot,
    TelemetryHub,
)


class _TestLogPatch:
    def __init__(self) -> None:
        self.log = HealthEventLog()
        self._saved = None

    def __enter__(self) -> HealthEventLog:
        self._saved = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = self.log
        return self.log

    def __exit__(self, *exc) -> None:
        health_events._DEFAULT_LOG = self._saved


def _snap(write_mb_s: float, *, available: bool = True) -> DiskSnapshot:
    return DiskSnapshot(
        path="C:\\test",
        available=available,
        free_bytes=500 * 1024 ** 3,
        total_bytes=1024 ** 4,
        write_mb_s_estimate=write_mb_s,
    )


class DiskSlowEmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patch = _TestLogPatch()
        self.log = self._patch.__enter__()
        self.hub = TelemetryHub(health_log=self.log, disk_budget_mb_s=200.0)

    def tearDown(self) -> None:
        self._patch.__exit__(None, None, None)

    def test_under_budget_does_not_emit(self) -> None:
        for _ in range(10):
            self.hub._evaluate_disk_throughput(_snap(150.0))
        self.assertFalse(self.log.has_open_event(category="disk_slow", feed_id=None))

    def test_over_budget_below_streak_does_not_emit(self) -> None:
        # Single tick over budget → streak == 1, below threshold == 3.
        self.hub._evaluate_disk_throughput(_snap(220.0))
        self.assertFalse(self.log.has_open_event(category="disk_slow", feed_id=None))

    def test_three_consecutive_over_budget_emits(self) -> None:
        for _ in range(DISK_SLOW_STREAK_THRESHOLD):
            self.hub._evaluate_disk_throughput(_snap(220.0))
        self.assertTrue(self.log.has_open_event(category="disk_slow", feed_id=None))

    def test_event_severity_is_warning(self) -> None:
        for _ in range(DISK_SLOW_STREAK_THRESHOLD):
            self.hub._evaluate_disk_throughput(_snap(220.0))
        events = [e for e in self.log.open_events() if e.category == "disk_slow"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].severity, HealthSeverity.WARNING.value)

    def test_event_emitted_once_while_open(self) -> None:
        for _ in range(DISK_SLOW_STREAK_THRESHOLD * 3):
            self.hub._evaluate_disk_throughput(_snap(220.0))
        self.assertEqual(self.log.category_count("disk_slow"), 1)

    def test_one_under_budget_clears_event(self) -> None:
        for _ in range(DISK_SLOW_STREAK_THRESHOLD):
            self.hub._evaluate_disk_throughput(_snap(220.0))
        self.assertTrue(self.log.has_open_event(category="disk_slow", feed_id=None))
        self.hub._evaluate_disk_throughput(_snap(150.0))
        self.assertFalse(self.log.has_open_event(category="disk_slow", feed_id=None))

    def test_streak_resets_on_under_budget_sample(self) -> None:
        # Two over, one under, then two over again — streak resets, no emission.
        self.hub._evaluate_disk_throughput(_snap(220.0))
        self.hub._evaluate_disk_throughput(_snap(220.0))
        self.hub._evaluate_disk_throughput(_snap(150.0))  # resets
        self.hub._evaluate_disk_throughput(_snap(220.0))
        self.hub._evaluate_disk_throughput(_snap(220.0))
        self.assertFalse(self.log.has_open_event(category="disk_slow", feed_id=None))

    def test_unavailable_snapshot_skipped(self) -> None:
        for _ in range(DISK_SLOW_STREAK_THRESHOLD):
            self.hub._evaluate_disk_throughput(_snap(220.0, available=False))
        self.assertFalse(self.log.has_open_event(category="disk_slow", feed_id=None))


class NoBudgetTests(unittest.TestCase):
    def test_evaluation_skipped_when_budget_unset(self) -> None:
        with _TestLogPatch() as log:
            hub = TelemetryHub(health_log=log)
            for _ in range(10):
                hub._evaluate_disk_throughput(_snap(99999.0))
            self.assertFalse(log.has_open_event(category="disk_slow", feed_id=None))

    def test_evaluation_skipped_when_budget_zero(self) -> None:
        with _TestLogPatch() as log:
            hub = TelemetryHub(health_log=log, disk_budget_mb_s=0.0)
            for _ in range(10):
                hub._evaluate_disk_throughput(_snap(99999.0))
            self.assertFalse(log.has_open_event(category="disk_slow", feed_id=None))


class QueueSaturationPctTests(unittest.TestCase):
    def test_zero_capacity_returns_zero(self) -> None:
        s = FeedMetricsSnapshot(
            feed_id="cam_a",
            display_name="Cam A",
            source_fps=0.0,
            preview_fps=0.0,
            recording_fps=0.0,
            queue_depth_recording=0,
            queue_max_recording=0,
        )
        self.assertEqual(s.record_queue_saturation_pct, 0.0)

    def test_half_full_returns_50(self) -> None:
        s = FeedMetricsSnapshot(
            feed_id="cam_a",
            display_name="Cam A",
            source_fps=0.0,
            preview_fps=0.0,
            recording_fps=0.0,
            queue_depth_recording=10,
            queue_max_recording=20,
        )
        self.assertEqual(s.record_queue_saturation_pct, 50.0)

    def test_full_returns_100(self) -> None:
        s = FeedMetricsSnapshot(
            feed_id="cam_a",
            display_name="Cam A",
            source_fps=0.0,
            preview_fps=0.0,
            recording_fps=0.0,
            queue_depth_recording=20,
            queue_max_recording=20,
        )
        self.assertEqual(s.record_queue_saturation_pct, 100.0)

    def test_overfull_clamps_to_100(self) -> None:
        # Edge case — a buggy sampler reporting depth > capacity.
        s = FeedMetricsSnapshot(
            feed_id="cam_a",
            display_name="Cam A",
            source_fps=0.0,
            preview_fps=0.0,
            recording_fps=0.0,
            queue_depth_recording=42,
            queue_max_recording=20,
        )
        self.assertEqual(s.record_queue_saturation_pct, 100.0)

    def test_preview_saturation_pct_mirrors_record(self) -> None:
        s = FeedMetricsSnapshot(
            feed_id="cam_a",
            display_name="Cam A",
            source_fps=0.0,
            preview_fps=0.0,
            recording_fps=0.0,
            queue_depth_preview=15,
            queue_max_preview=20,
        )
        self.assertEqual(s.preview_queue_saturation_pct, 75.0)


class DiskWriteGlyphTests(unittest.TestCase):
    """Pure unit test on the diagnostics-widget glyph helper."""

    def _glyph(self, write_mb_s: float, budget: float) -> str:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        from app.config.settings import AppSettings
        from app.core.telemetry import TelemetryHub
        from app.ui.diagnostics_widget import DiagnosticsWidget

        settings = AppSettings()
        settings.disk_budget_mb_s = budget
        hub = TelemetryHub()
        widget = DiagnosticsWidget(hub, settings=settings)
        return widget._disk_write_glyph(write_mb_s)

    def test_under_80pct_is_check(self) -> None:
        self.assertEqual(self._glyph(50.0, 200.0), "✓")

    def test_at_80pct_is_warn(self) -> None:
        self.assertEqual(self._glyph(160.0, 200.0), "⚠")

    def test_at_budget_is_cross(self) -> None:
        self.assertEqual(self._glyph(200.0, 200.0), "✗")

    def test_zero_budget_returns_empty_string(self) -> None:
        self.assertEqual(self._glyph(50.0, 0.0), "")


class AlertBannerWiringTests(unittest.TestCase):
    def test_disk_slow_in_allowlist(self) -> None:
        from app.ui.alert_banner import _OPERATOR_VISIBLE_CATEGORIES
        self.assertIn("disk_slow", _OPERATOR_VISIBLE_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
