"""Unit tests for `app.core.telemetry`."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import unittest

from app.core.telemetry import DiskSampler, FeedMetrics, RateCounter, TelemetryHub


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


@dataclass
class _FakeDiskUsage:
    total: int
    used: int
    free: int


class _FakeDiskUsageFn:
    """Programmable `shutil.disk_usage` replacement.

    Returns successive values from `samples`, raising `FileNotFoundError`
    for any sample equal to `None`.
    """

    def __init__(self, samples: list[_FakeDiskUsage | None]) -> None:
        self._samples = list(samples)
        self.calls = 0

    def __call__(self, _path) -> _FakeDiskUsage:
        if not self._samples:
            raise AssertionError("DiskSampler called more times than expected")
        sample = self._samples.pop(0)
        self.calls += 1
        if sample is None:
            raise FileNotFoundError("path missing")
        return sample


class DiskSamplerTests(unittest.TestCase):
    def test_no_path_reports_unavailable(self) -> None:
        sampler = DiskSampler(None)
        snap = sampler.sample()
        self.assertFalse(snap.available)
        self.assertEqual(snap.write_mb_s_estimate, 0.0)

    def test_first_sample_has_zero_write_rate(self) -> None:
        clock = FakeClock()
        usage = _FakeDiskUsageFn([_FakeDiskUsage(total=1000, used=200, free=800)])
        sampler = DiskSampler("X:\\fake", clock=clock, disk_usage_fn=usage)
        snap = sampler.sample()
        self.assertTrue(snap.available)
        self.assertEqual(snap.free_bytes, 800)
        self.assertEqual(snap.total_bytes, 1000)
        self.assertEqual(snap.write_mb_s_estimate, 0.0)

    def test_second_sample_computes_positive_write_rate(self) -> None:
        clock = FakeClock()
        one_mb = 1024 * 1024
        usage = _FakeDiskUsageFn([
            _FakeDiskUsage(total=10 * one_mb, used=0, free=10 * one_mb),
            _FakeDiskUsage(total=10 * one_mb, used=2 * one_mb, free=8 * one_mb),
        ])
        sampler = DiskSampler("X:\\fake", clock=clock, disk_usage_fn=usage)
        sampler.sample()
        clock.advance(1.0)
        snap = sampler.sample()
        self.assertAlmostEqual(snap.write_mb_s_estimate, 2.0, places=3)

    def test_negative_delta_clamps_to_zero(self) -> None:
        clock = FakeClock()
        one_mb = 1024 * 1024
        usage = _FakeDiskUsageFn([
            _FakeDiskUsage(total=10 * one_mb, used=5 * one_mb, free=5 * one_mb),
            _FakeDiskUsage(total=10 * one_mb, used=2 * one_mb, free=8 * one_mb),
        ])
        sampler = DiskSampler("X:\\fake", clock=clock, disk_usage_fn=usage)
        sampler.sample()
        clock.advance(1.0)
        snap = sampler.sample()
        self.assertEqual(snap.write_mb_s_estimate, 0.0)

    def test_missing_path_resets_baseline(self) -> None:
        clock = FakeClock()
        one_mb = 1024 * 1024
        usage = _FakeDiskUsageFn([
            _FakeDiskUsage(total=10 * one_mb, used=0, free=10 * one_mb),
            None,
            _FakeDiskUsage(total=10 * one_mb, used=2 * one_mb, free=8 * one_mb),
        ])
        sampler = DiskSampler("X:\\fake", clock=clock, disk_usage_fn=usage)
        sampler.sample()
        clock.advance(1.0)
        missing = sampler.sample()
        self.assertFalse(missing.available)
        clock.advance(1.0)
        snap = sampler.sample()
        # Baseline was reset by the missing-path sample, so the post-recovery
        # sample reports zero rate even though `free` decreased.
        self.assertEqual(snap.write_mb_s_estimate, 0.0)

    def test_set_path_resets_baseline(self) -> None:
        clock = FakeClock()
        one_mb = 1024 * 1024
        usage = _FakeDiskUsageFn([
            _FakeDiskUsage(total=10 * one_mb, used=0, free=10 * one_mb),
            _FakeDiskUsage(total=20 * one_mb, used=0, free=20 * one_mb),
            _FakeDiskUsage(total=20 * one_mb, used=one_mb, free=19 * one_mb),
        ])
        sampler = DiskSampler("X:\\old", clock=clock, disk_usage_fn=usage)
        sampler.sample()
        sampler.set_path("Y:\\new")
        clock.advance(1.0)
        first_after_switch = sampler.sample()
        self.assertEqual(first_after_switch.write_mb_s_estimate, 0.0)
        clock.advance(1.0)
        second_after_switch = sampler.sample()
        self.assertAlmostEqual(second_after_switch.write_mb_s_estimate, 1.0, places=3)


class TelemetryHubDiskTests(unittest.TestCase):
    def test_disk_log_emits_after_set_path(self) -> None:
        clock = FakeClock()
        one_mb = 1024 * 1024
        usage = _FakeDiskUsageFn([
            _FakeDiskUsage(total=100 * one_mb, used=0, free=100 * one_mb),
        ])
        sampler = DiskSampler("X:\\fake", clock=clock, disk_usage_fn=usage)
        hub = TelemetryHub()
        hub.set_disk_path("X:\\fake", sampler=sampler)
        with self.assertLogs("app.core.telemetry", level="INFO") as captured:
            hub._log_disk_snapshot()
        self.assertEqual(len(captured.records), 1)
        self.assertIn("path=X:\\fake", captured.output[0])
        self.assertIn("free_gb=", captured.output[0])
        self.assertIn("write_mb_s=", captured.output[0])

    def test_disk_log_reports_unavailable_when_path_missing(self) -> None:
        usage = _FakeDiskUsageFn([None])
        sampler = DiskSampler("X:\\missing", disk_usage_fn=usage)
        hub = TelemetryHub()
        hub.set_disk_path("X:\\missing", sampler=sampler)
        with self.assertLogs("app.core.telemetry", level="INFO") as captured:
            hub._log_disk_snapshot()
        self.assertIn("available=false", captured.output[0])

    def test_disk_log_skipped_when_path_never_set(self) -> None:
        hub = TelemetryHub()
        # No set_disk_path called.
        with self.assertNoLogs("app.core.telemetry", level="INFO"):
            hub._log_disk_snapshot()

    def test_disk_snapshot_returns_latest_sample(self) -> None:
        clock = FakeClock()
        one_mb = 1024 * 1024
        usage = _FakeDiskUsageFn([
            _FakeDiskUsage(total=100 * one_mb, used=10 * one_mb, free=90 * one_mb),
        ])
        sampler = DiskSampler("X:\\fake", clock=clock, disk_usage_fn=usage)
        hub = TelemetryHub()
        hub.set_disk_path("X:\\fake", sampler=sampler)
        self.assertIsNone(hub.disk_snapshot())
        hub._log_disk_snapshot()
        snap = hub.disk_snapshot()
        self.assertIsNotNone(snap)
        self.assertTrue(snap.available)
        self.assertEqual(snap.free_bytes, 90 * one_mb)

    def test_start_registers_disk_callback_when_path_set(self) -> None:
        intervals: list[float] = []

        def registrar(interval, callback):
            intervals.append(interval)
            return lambda: None

        hub = TelemetryHub()
        hub.set_disk_path("X:\\fake", sampler=DiskSampler(None))
        hub.start(registrar)
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0], 1.0)
        self.assertEqual(intervals[1], 5.0)

    def test_start_registers_only_feed_callback_without_disk(self) -> None:
        intervals: list[float] = []

        def registrar(interval, callback):
            intervals.append(interval)
            return lambda: None

        hub = TelemetryHub()
        hub.start(registrar)
        self.assertEqual(intervals, [1.0])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
