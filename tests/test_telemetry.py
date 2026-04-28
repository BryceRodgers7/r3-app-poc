"""Unit tests for `app.core.telemetry`."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import unittest

from app.core import health_events
from app.core.feed_state import FeedState, make_feed_state_machine
from app.core.health_events import HealthEventLog
from app.core.telemetry import (
    DROPPED_BUFFERS_DEGRADED_THRESHOLD,
    DiskSampler,
    FeedMetrics,
    FEED_LOST_ZERO_SAMPLES,
    LatencyRegistry,
    LatencySampler,
    RateCounter,
    TelemetryHub,
    latency_snapshots,
    record_latency,
    reset_latency_registry,
    time_block,
)


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

    def setUp(self) -> None:
        reset_latency_registry()

    def test_log_all_snapshots_emits_one_line_per_feed(self) -> None:
        hub = TelemetryHub()
        hub.register("cam_a", "A")
        hub.register("cam_b", "B")
        with self.assertLogs("app.core.telemetry", level="INFO") as captured:
            hub._log_all_snapshots()
        self.assertEqual(len(captured.records), 2)
        self.assertIn("feed_id=cam_a", captured.output[0])
        self.assertIn("feed_id=cam_b", captured.output[1])

    def test_log_all_snapshots_includes_non_empty_latency_samplers(self) -> None:
        hub = TelemetryHub()
        hub.register("cam_a", "A")
        record_latency("segment_write_video", 0.0021)
        record_latency("segment_write_video", 0.0034)
        with self.assertLogs("app.core.telemetry", level="INFO") as captured:
            hub._log_all_snapshots()
        latency_lines = [line for line in captured.output if "latency" in line]
        self.assertEqual(len(latency_lines), 1)
        self.assertIn("name=segment_write_video", latency_lines[0])
        self.assertIn("count=2", latency_lines[0])

    def test_log_all_snapshots_skips_empty_latency_samplers(self) -> None:
        hub = TelemetryHub()
        hub.register("cam_a", "A")
        # Touch a sampler without recording anything.
        from app.core.telemetry import _LATENCY_REGISTRY
        _LATENCY_REGISTRY.sampler("never_called")
        with self.assertLogs("app.core.telemetry", level="INFO") as captured:
            hub._log_all_snapshots()
        latency_lines = [line for line in captured.output if "latency" in line]
        self.assertEqual(latency_lines, [])


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


class TelemetryHubHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_latency_registry()
        self.health_log = HealthEventLog()

    def test_feed_lost_emitted_after_streak_threshold(self) -> None:
        hub = TelemetryHub(health_log=self.health_log)
        hub.register("cam_a", "Cam A")
        # Tick three zero-fps log cycles in a row.
        for _ in range(FEED_LOST_ZERO_SAMPLES):
            hub._log_all_snapshots()
        self.assertTrue(
            self.health_log.has_open_event(category="feed_lost", feed_id="cam_a")
        )

    def test_feed_lost_not_emitted_below_threshold(self) -> None:
        hub = TelemetryHub(health_log=self.health_log)
        hub.register("cam_a", "Cam A")
        for _ in range(FEED_LOST_ZERO_SAMPLES - 1):
            hub._log_all_snapshots()
        self.assertFalse(
            self.health_log.has_open_event(category="feed_lost", feed_id="cam_a")
        )

    def test_feed_lost_not_re_emitted_while_open(self) -> None:
        hub = TelemetryHub(health_log=self.health_log)
        hub.register("cam_a", "Cam A")
        for _ in range(FEED_LOST_ZERO_SAMPLES + 5):
            hub._log_all_snapshots()
        # The log should hold exactly one feed_lost record (no spam).
        feed_lost_events = [
            e for (fid, cat), e in self.health_log._last_categories.items()
            if cat == "feed_lost"
        ]
        self.assertEqual(len(feed_lost_events), 1)

    def test_feed_recovery_emits_recovered_event(self) -> None:
        clock = FakeClock()
        hub = TelemetryHub(health_log=self.health_log)
        hub.register("cam_a", "Cam A")
        for _ in range(FEED_LOST_ZERO_SAMPLES):
            hub._log_all_snapshots()
        # Now feed comes back: tick the source counter and re-snapshot.
        metrics = hub.metrics_for("cam_a")
        assert metrics is not None
        for _ in range(20):
            metrics.tick_source()
        hub._log_all_snapshots()
        self.assertFalse(
            self.health_log.has_open_event(category="feed_lost", feed_id="cam_a")
        )

    def test_disk_low_emits_when_fraction_below_threshold(self) -> None:
        usage = _FakeDiskUsageFn([_FakeDiskUsage(total=1000, used=970, free=30)])
        sampler = DiskSampler("X:\\fake", disk_usage_fn=usage)
        hub = TelemetryHub(health_log=self.health_log)
        hub.set_disk_path("X:\\fake", sampler=sampler)
        hub._log_disk_snapshot()
        self.assertTrue(self.health_log.has_open_event(category="disk_low", feed_id=None))

    def test_disk_low_silent_when_above_threshold(self) -> None:
        usage = _FakeDiskUsageFn([_FakeDiskUsage(total=1000, used=200, free=800)])
        sampler = DiskSampler("X:\\fake", disk_usage_fn=usage)
        hub = TelemetryHub(health_log=self.health_log)
        hub.set_disk_path("X:\\fake", sampler=sampler)
        hub._log_disk_snapshot()
        self.assertFalse(self.health_log.has_open_event(category="disk_low", feed_id=None))


class TelemetryHubFeedStateTests(unittest.TestCase):
    """Tests for hub-driven `FeedState` transitions (slice 2.A)."""

    def setUp(self) -> None:
        reset_latency_registry()
        # Swap the process-wide log so hub + state-machine emissions land in
        # one inspectable instance.
        self._orig_log = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = HealthEventLog()
        self.log = health_events._DEFAULT_LOG

    def tearDown(self) -> None:
        health_events._DEFAULT_LOG = self._orig_log

    def test_streak_drives_state_machine_to_disconnected(self) -> None:
        hub = TelemetryHub(health_log=self.log)
        hub.register("cam_a", "Cam A")
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        hub.register_feed_state("cam_a", sm)
        for _ in range(FEED_LOST_ZERO_SAMPLES):
            hub._log_all_snapshots()
        self.assertIs(sm.state, FeedState.DISCONNECTED)
        self.assertTrue(self.log.has_open_event(category="feed_lost", feed_id="cam_a"))

    def test_dropped_buffers_drive_live_to_degraded(self) -> None:
        clock = FakeClock()
        hub = TelemetryHub(health_log=self.log)
        metrics = hub.register("cam_a", "Cam A")
        # Inject a clock-controlled internal counter so the test is deterministic.
        metrics._dropped = RateCounter(window_seconds=1.0, clock=clock)
        metrics._source = RateCounter(window_seconds=1.0, clock=clock)
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        hub.register_feed_state("cam_a", sm)
        # Source frames present (so we don't trip the zero-fps streak),
        # plus enough QOS drops to exceed the threshold.
        for _ in range(30):
            metrics.tick_source()
        for _ in range(int(DROPPED_BUFFERS_DEGRADED_THRESHOLD * 1.0) + 2):
            metrics.tick_dropped()
        hub._log_all_snapshots()
        self.assertIs(sm.state, FeedState.DEGRADED)
        self.assertTrue(self.log.has_open_event(category="feed_degraded", feed_id="cam_a"))

    def test_drops_subside_returns_state_to_live(self) -> None:
        clock = FakeClock()
        hub = TelemetryHub(health_log=self.log)
        metrics = hub.register("cam_a", "Cam A")
        metrics._dropped = RateCounter(window_seconds=1.0, clock=clock)
        metrics._source = RateCounter(window_seconds=1.0, clock=clock)
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.force(FeedState.LIVE)
        hub.register_feed_state("cam_a", sm)
        for _ in range(30):
            metrics.tick_source()
        for _ in range(5):
            metrics.tick_dropped()
        hub._log_all_snapshots()
        self.assertIs(sm.state, FeedState.DEGRADED)
        # Now drops subside: time advances, dropped counter empties.
        clock.advance(2.0)
        for _ in range(30):
            metrics.tick_source()
        hub._log_all_snapshots()
        self.assertIs(sm.state, FeedState.LIVE)


class FeedMetricsDroppedTests(unittest.TestCase):
    def test_dropped_counter_independent(self) -> None:
        clock = FakeClock()
        m = FeedMetrics("cam_a", "A", window_seconds=1.0, clock=clock)
        for _ in range(10):
            m.tick_dropped()
        snap = m.snapshot()
        self.assertGreater(snap.dropped_per_sec, 0.0)
        self.assertEqual(snap.preview_fps, 0.0)
        self.assertEqual(snap.recording_fps, 0.0)


class FeedMetricsPipelineModeTests(unittest.TestCase):
    def test_default_pipeline_mode_is_python_push(self) -> None:
        m = FeedMetrics("cam_a", "A")
        self.assertEqual(m.pipeline_mode, "python_push")
        self.assertEqual(m.snapshot().pipeline_mode, "python_push")

    def test_set_pipeline_mode_propagates_to_snapshot(self) -> None:
        m = FeedMetrics("cam_a", "A")
        m.set_pipeline_mode("native")
        self.assertEqual(m.snapshot().pipeline_mode, "native")

    def test_python_frames_counter_independent_from_other_counters(self) -> None:
        clock = FakeClock()
        m = FeedMetrics("cam_a", "A", window_seconds=1.0, clock=clock)
        for _ in range(20):
            m.tick_python_frame()
        snap = m.snapshot()
        self.assertGreater(snap.python_frames_per_sec, 0.0)
        self.assertEqual(snap.preview_fps, 0.0)
        self.assertEqual(snap.recording_fps, 0.0)
        self.assertEqual(snap.source_fps, 0.0)

    def test_native_source_reports_zero_python_frames(self) -> None:
        m = FeedMetrics("cam_a", "A", pipeline_mode="native")
        # Tick everything except python_frame.
        m.tick_source()
        m.tick_preview()
        m.tick_recording()
        snap = m.snapshot()
        self.assertEqual(snap.python_frames_per_sec, 0.0)
        self.assertEqual(snap.pipeline_mode, "native")


class LatencySamplerTests(unittest.TestCase):
    def test_empty_sampler_reports_zero_count(self) -> None:
        clock = FakeClock()
        sampler = LatencySampler("seek", window_seconds=2.0, clock=clock)
        snap = sampler.snapshot()
        self.assertEqual(snap.count, 0)
        self.assertEqual(snap.avg_ms, 0.0)
        self.assertEqual(snap.max_ms, 0.0)
        self.assertEqual(snap.p95_ms, 0.0)

    def test_records_within_window_compute_avg_max_p95(self) -> None:
        clock = FakeClock()
        sampler = LatencySampler("seek", window_seconds=10.0, clock=clock)
        # 10 samples: 1ms, 2ms, ..., 10ms
        for ms in range(1, 11):
            sampler.record(ms / 1000.0)
            clock.advance(0.1)
        snap = sampler.snapshot()
        self.assertEqual(snap.count, 10)
        self.assertAlmostEqual(snap.avg_ms, 5.5, places=4)
        self.assertAlmostEqual(snap.max_ms, 10.0, places=4)
        # ceil(0.95 * 10) - 1 = 9 → the 10th sorted value, which is 10ms.
        self.assertAlmostEqual(snap.p95_ms, 10.0, places=4)

    def test_old_samples_evicted(self) -> None:
        clock = FakeClock()
        sampler = LatencySampler("seek", window_seconds=2.0, clock=clock)
        sampler.record(0.005)
        clock.advance(10.0)
        snap = sampler.snapshot()
        self.assertEqual(snap.count, 0)

    def test_negative_durations_ignored(self) -> None:
        sampler = LatencySampler("seek", window_seconds=2.0)
        sampler.record(-0.1)
        self.assertEqual(sampler.snapshot().count, 0)

    def test_invalid_construction_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LatencySampler("")
        with self.assertRaises(ValueError):
            LatencySampler("x", window_seconds=0)

    def test_reset_clears_samples(self) -> None:
        sampler = LatencySampler("seek", window_seconds=2.0)
        sampler.record(0.005)
        sampler.reset()
        self.assertEqual(sampler.snapshot().count, 0)


class LatencyRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_latency_registry()

    def test_record_latency_creates_sampler_on_first_call(self) -> None:
        record_latency("seek", 0.002)
        snaps = {s.name: s for s in latency_snapshots()}
        self.assertIn("seek", snaps)
        self.assertEqual(snaps["seek"].count, 1)

    def test_repeat_record_under_same_name_aggregates(self) -> None:
        for _ in range(5):
            record_latency("seek", 0.001)
        snap = next(s for s in latency_snapshots() if s.name == "seek")
        self.assertEqual(snap.count, 5)

    def test_reset_drops_all_samplers(self) -> None:
        record_latency("a", 0.001)
        record_latency("b", 0.001)
        reset_latency_registry()
        self.assertEqual(latency_snapshots(), [])

    def test_time_block_records_under_name(self) -> None:
        with time_block("segment_write_video"):
            pass
        snaps = {s.name: s for s in latency_snapshots()}
        self.assertIn("segment_write_video", snaps)
        self.assertEqual(snaps["segment_write_video"].count, 1)

    def test_time_block_records_even_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with time_block("segment_write_video"):
                raise RuntimeError("boom")
        snaps = {s.name: s for s in latency_snapshots()}
        self.assertEqual(snaps["segment_write_video"].count, 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
