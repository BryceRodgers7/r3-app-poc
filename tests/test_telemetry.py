"""Unit tests for `app.core.telemetry`."""

from __future__ import annotations

import logging
import unittest

from app.core.telemetry import FeedMetrics, RateCounter, TelemetryHub


class FakeClock:
    """Manually advanced monotonic clock for deterministic rate-counter tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class RateCounterTests(unittest.TestCase):
    def test_empty_counter_reports_zero(self) -> None:
        clock = FakeClock()
        counter = RateCounter(window_seconds=2.0, clock=clock)
        self.assertEqual(counter.rate(), 0.0)

    def test_steady_rate_within_window(self) -> None:
        clock = FakeClock()
        counter = RateCounter(window_seconds=2.0, clock=clock)
        for _ in range(20):
            counter.tick()
            clock.advance(0.1)
        # Allow minor float drift from repeated 0.1 additions; the goal is
        # "rate is roughly 20 events / 2s window".
        self.assertAlmostEqual(counter.rate(), 10.0, delta=0.5)

    def test_old_events_evicted_from_window(self) -> None:
        clock = FakeClock()
        counter = RateCounter(window_seconds=2.0, clock=clock)
        for _ in range(5):
            counter.tick()
        clock.advance(10.0)
        self.assertEqual(counter.rate(), 0.0)

    def test_partial_window_after_burst(self) -> None:
        clock = FakeClock()
        counter = RateCounter(window_seconds=4.0, clock=clock)
        for _ in range(8):
            counter.tick()
            clock.advance(0.25)
        clock.advance(2.0)
        self.assertAlmostEqual(counter.rate(), 8 / 4.0, places=2)
        clock.advance(2.5)
        self.assertEqual(counter.rate(), 0.0)

    def test_invalid_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RateCounter(window_seconds=0)
        with self.assertRaises(ValueError):
            RateCounter(window_seconds=-1.0)

    def test_reset_clears_events(self) -> None:
        clock = FakeClock()
        counter = RateCounter(window_seconds=2.0, clock=clock)
        for _ in range(5):
            counter.tick()
        counter.reset()
        self.assertEqual(counter.rate(), 0.0)


class FeedMetricsTests(unittest.TestCase):
    def test_snapshot_reflects_independent_counters(self) -> None:
        clock = FakeClock()
        metrics = FeedMetrics(
            feed_id="cam_a",
            display_name="USB Cam A",
            window_seconds=2.0,
            clock=clock,
        )
        for _ in range(20):
            metrics.tick_source()
            metrics.tick_preview()
            clock.advance(0.05)
        snap = metrics.snapshot()
        self.assertEqual(snap.feed_id, "cam_a")
        self.assertEqual(snap.display_name, "USB Cam A")
        self.assertAlmostEqual(snap.source_fps, 10.0, places=1)
        self.assertAlmostEqual(snap.preview_fps, 10.0, places=1)
        self.assertEqual(snap.recording_fps, 0.0)

    def test_recording_counter_independent_from_preview(self) -> None:
        clock = FakeClock()
        metrics = FeedMetrics(
            feed_id="cam_b",
            display_name="USB Cam B",
            window_seconds=1.0,
            clock=clock,
        )
        for _ in range(15):
            metrics.tick_preview()
            clock.advance(0.05)
        snap = metrics.snapshot()
        self.assertGreater(snap.preview_fps, 0.0)
        self.assertEqual(snap.recording_fps, 0.0)


class TelemetryHubTests(unittest.TestCase):
    def test_register_returns_same_metrics_for_repeat_id(self) -> None:
        hub = TelemetryHub()
        a = hub.register("cam_a", "Cam A")
        b = hub.register("cam_a", "Cam A")
        self.assertIs(a, b)

    def test_register_rejects_empty_feed_id(self) -> None:
        hub = TelemetryHub()
        with self.assertRaises(ValueError):
            hub.register("", "x")

    def test_metrics_for_returns_none_for_unknown_feed(self) -> None:
        hub = TelemetryHub()
        self.assertIsNone(hub.metrics_for("missing"))

    def test_snapshot_preserves_registration_order(self) -> None:
        hub = TelemetryHub()
        hub.register("cam_a", "A")
        hub.register("cam_b", "B")
        hub.register("cam_c", "C")
        ids = [s.feed_id for s in hub.snapshot()]
        self.assertEqual(ids, ["cam_a", "cam_b", "cam_c"])

    def test_start_stop_are_idempotent(self) -> None:
        hub = TelemetryHub()
        cancel_calls: list[int] = []

        def registrar(interval, callback):
            self.assertGreater(interval, 0)

            def cancel() -> None:
                cancel_calls.append(1)

            return cancel

        hub.start(registrar)
        hub.start(registrar)
        hub.stop()
        hub.stop()
        self.assertEqual(cancel_calls, [1])

    def test_log_all_snapshots_emits_one_line_per_feed(self) -> None:
        hub = TelemetryHub()
        hub.register("cam_a", "A")
        hub.register("cam_b", "B")
        with self.assertLogs("app.core.telemetry", level="INFO") as captured:
            hub._log_all_snapshots()
        self.assertEqual(len(captured.records), 2)
        self.assertIn("feed_id=cam_a", captured.output[0])
        self.assertIn("feed_id=cam_b", captured.output[1])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
