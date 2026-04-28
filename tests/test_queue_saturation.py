"""Tests for slice 3.B queue-depth metrics and saturation health rules."""

from __future__ import annotations

import unittest

from app.core.feed_state import FeedState, make_feed_state_machine
from app.core.health_events import HealthEventLog
from app.core.recording_state import RecordingState, make_recording_state_machine
from app.core.telemetry import FeedMetrics, TelemetryHub


class _Sampler:
    """Stub queue-depth sampler returning a configurable shape."""

    def __init__(self, preview: dict, record: dict) -> None:
        self._payload = {"preview": preview, "record": record}

    def __call__(self) -> dict:
        return self._payload


class FeedMetricsQueueDepthTests(unittest.TestCase):
    def test_queue_depths_round_trip_through_snapshot(self) -> None:
        m = FeedMetrics(feed_id="f1", display_name="Feed 1")
        m.set_queue_capacity(preview_max_buffers=4, recording_max_buffers=256)
        m.set_queue_depths(preview_buffers=2, recording_buffers=128)
        snap = m.snapshot()
        self.assertEqual(snap.queue_depth_preview, 2)
        self.assertEqual(snap.queue_max_preview, 4)
        self.assertEqual(snap.queue_depth_recording, 128)
        self.assertEqual(snap.queue_max_recording, 256)

    def test_default_zero_when_no_sampler_registered(self) -> None:
        m = FeedMetrics(feed_id="f1", display_name="Feed 1")
        snap = m.snapshot()
        self.assertEqual(snap.queue_depth_preview, 0)
        self.assertEqual(snap.queue_max_preview, 0)
        self.assertEqual(snap.queue_depth_recording, 0)
        self.assertEqual(snap.queue_max_recording, 0)

    def test_negative_values_clamped_to_zero(self) -> None:
        m = FeedMetrics(feed_id="f1", display_name="Feed 1")
        m.set_queue_depths(preview_buffers=-5, recording_buffers=-10)
        snap = m.snapshot()
        self.assertEqual(snap.queue_depth_preview, 0)
        self.assertEqual(snap.queue_depth_recording, 0)


class HubQueueDepthSamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.health_log = HealthEventLog()
        self.hub = TelemetryHub(
            log_interval_seconds=1.0,
            disk_interval_seconds=5.0,
            health_log=self.health_log,
        )

    def test_refresh_reads_through_registered_sampler(self) -> None:
        self.hub.register("f1", "Feed 1")
        sampler = _Sampler(
            preview={"buffers": 3, "max_buffers": 4},
            record={"buffers": 50, "max_buffers": 256},
        )
        self.hub.register_queue_depth_sampler("f1", sampler)
        self.hub._log_all_snapshots()  # type: ignore[attr-defined]
        snap = self.hub.snapshot()[0]
        self.assertEqual(snap.queue_depth_preview, 3)
        self.assertEqual(snap.queue_max_preview, 4)
        self.assertEqual(snap.queue_depth_recording, 50)
        self.assertEqual(snap.queue_max_recording, 256)

    def test_sampler_exception_does_not_break_snapshot(self) -> None:
        self.hub.register("f1", "Feed 1")

        def boom() -> dict:
            raise RuntimeError("sampler exploded")

        self.hub.register_queue_depth_sampler("f1", boom)
        # Should not raise.
        self.hub._log_all_snapshots()  # type: ignore[attr-defined]
        snap = self.hub.snapshot()[0]
        # Values stay at the previous (zero) reading.
        self.assertEqual(snap.queue_depth_preview, 0)
        self.assertEqual(snap.queue_depth_recording, 0)


class RecordingSaturationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.health_log = HealthEventLog()
        self.hub = TelemetryHub(
            log_interval_seconds=1.0,
            disk_interval_seconds=5.0,
            health_log=self.health_log,
        )
        self.hub.register("f1", "Feed 1")
        self.recording_state = make_recording_state_machine()
        self.hub.register_recording_state(self.recording_state)
        # Drive into RECORDING so the saturation rule can transition.
        self.recording_state.transition_to(RecordingState.STARTING_RECORDING)
        self.recording_state.transition_to(RecordingState.RECORDING)

    def test_two_consecutive_saturated_ticks_drives_recording_error(self) -> None:
        sampler = _Sampler(
            preview={"buffers": 0, "max_buffers": 4},
            record={"buffers": 200, "max_buffers": 256},  # ~78% saturated
        )
        self.hub.register_queue_depth_sampler("f1", sampler)
        self.hub._log_all_snapshots()  # tick 1: streak=1, no transition yet  # type: ignore[attr-defined]
        self.assertEqual(self.recording_state.state, RecordingState.RECORDING)
        self.hub._log_all_snapshots()  # tick 2: streak=2 → RECORDING_ERROR  # type: ignore[attr-defined]
        self.assertEqual(self.recording_state.state, RecordingState.RECORDING_ERROR)
        self.assertTrue(
            self.health_log.has_open_event(
                category="recording_branch_saturated", feed_id="f1"
            )
        )

    def test_single_saturated_tick_does_not_transition(self) -> None:
        sampler = _Sampler(
            preview={"buffers": 0, "max_buffers": 4},
            record={"buffers": 200, "max_buffers": 256},
        )
        self.hub.register_queue_depth_sampler("f1", sampler)
        self.hub._log_all_snapshots()  # type: ignore[attr-defined]
        self.assertEqual(self.recording_state.state, RecordingState.RECORDING)

    def test_saturation_streak_resets_on_drain(self) -> None:
        sampler = _Sampler(
            preview={"buffers": 0, "max_buffers": 4},
            record={"buffers": 200, "max_buffers": 256},
        )
        self.hub.register_queue_depth_sampler("f1", sampler)
        self.hub._log_all_snapshots()  # type: ignore[attr-defined]
        # Drain: replace the sampler with a low-depth payload.
        self.hub.register_queue_depth_sampler(
            "f1",
            _Sampler(
                preview={"buffers": 0, "max_buffers": 4},
                record={"buffers": 10, "max_buffers": 256},
            ),
        )
        self.hub._log_all_snapshots()  # type: ignore[attr-defined]
        # Re-saturate.
        self.hub.register_queue_depth_sampler("f1", sampler)
        self.hub._log_all_snapshots()  # type: ignore[attr-defined]
        # Streak only reached 1 after re-saturation, so no transition yet.
        self.assertEqual(self.recording_state.state, RecordingState.RECORDING)


class PreviewSaturationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.health_log = HealthEventLog()
        self.hub = TelemetryHub(
            log_interval_seconds=1.0,
            disk_interval_seconds=5.0,
            health_log=self.health_log,
        )
        self.hub.register("f1", "Feed 1")
        # Force feed_state into LIVE so transition LIVE→DEGRADED is valid.
        self.feed_state = make_feed_state_machine("f1", "Feed 1")
        self.feed_state.transition_to(FeedState.CONNECTING)
        self.feed_state.transition_to(FeedState.LIVE)
        self.hub.register_feed_state("f1", self.feed_state)

    def test_preview_saturation_drives_feed_to_degraded(self) -> None:
        sampler = _Sampler(
            preview={"buffers": 4, "max_buffers": 4},  # 100% saturated
            record={"buffers": 0, "max_buffers": 256},
        )
        self.hub.register_queue_depth_sampler("f1", sampler)
        self.hub._log_all_snapshots()  # tick 1  # type: ignore[attr-defined]
        self.assertEqual(self.feed_state.state, FeedState.LIVE)
        self.hub._log_all_snapshots()  # tick 2 → DEGRADED  # type: ignore[attr-defined]
        self.assertEqual(self.feed_state.state, FeedState.DEGRADED)


if __name__ == "__main__":
    unittest.main()
