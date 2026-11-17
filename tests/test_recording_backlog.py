"""Recording backlog: the operator warning, and a full queue is not a lost source.

The recording queue is non-leaky and holds one segment (4 s at the defaults). When
it fills, the shared tee blocks and the ingest reads zero frames, which used to
get the feed declared lost and rebuilt. These cover the warning that now appears
before that, and the rule that keeps a back-pressured feed from being rebuilt.
"""

from __future__ import annotations

import unittest

from app.core import health_events
from app.core.feed_state import FeedState, make_feed_state_machine
from app.core.health_events import HealthEventLog
from app.core.recording_state import RecordingState, make_recording_state_machine
from app.core.telemetry import (
    FEED_LOST_ZERO_SAMPLES,
    RECORD_BEHIND_STREAK,
    RECORD_STUCK_SAMPLES,
    FeedMetrics,
    TelemetryHub,
)
from app.ui.alert_banner import select_banner_state, format_banner_text

WINDOW_NS = 4_000_000_000  # the recording queue's one-segment window


class _Queue:
    """A queue-depth sampler whose recording backlog the test sets, in seconds."""

    def __init__(self) -> None:
        self.backlog_s = 0.0
        self.max_buffers = 256

    def __call__(self) -> dict:
        return {
            "preview": {"buffers": 0, "max_buffers": 4, "time_ns": 0, "max_time_ns": 200_000_000},
            "record": {
                "buffers": int(self.backlog_s * 30),
                "max_buffers": self.max_buffers,
                "time_ns": int(self.backlog_s * 1e9),
                "max_time_ns": WINDOW_NS,
            },
        }


class _Rig:
    """A hub with one feed, a queue sampler, and counters the test can feed."""

    def __init__(self, *, recording: bool = True) -> None:
        self.log = HealthEventLog()
        self._orig = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = self.log  # state machines record into the same log
        self.hub = TelemetryHub(health_log=self.log)
        self.metrics: FeedMetrics = self.hub.register("cam_a", "Cam A")
        self.queue = _Queue()
        self.hub.register_queue_depth_sampler("cam_a", self.queue)
        self.feed = make_feed_state_machine("cam_a", "Cam A")
        self.feed.transition_to(FeedState.CONNECTING)
        self.feed.transition_to(FeedState.LIVE)
        self.hub.register_feed_state("cam_a", self.feed)
        self.recording = make_recording_state_machine()
        self.hub.register_recording_state(self.recording)
        if recording:
            self.recording.transition_to(RecordingState.STARTING_RECORDING)
            self.recording.transition_to(RecordingState.RECORDING)

    def close(self) -> None:
        health_events._DEFAULT_LOG = self._orig

    def tick(self, *, source: bool = True, recording: bool = True, times: int = 1) -> None:
        """One 1 s telemetry cycle: frames flowing (or not), then the sampler cycle."""
        for _ in range(times):
            if source:
                for _ in range(40):
                    self.metrics.tick_source()
            else:
                self._empty(self.metrics._source)  # a stalled ingest: nothing in the window
            if recording:
                for _ in range(40):
                    self.metrics.tick_recording()
            else:
                self._empty(self.metrics._recording)
            self.hub._log_all_snapshots()

    @staticmethod
    def _empty(counter) -> None:
        """Make a 5 s rate window read as empty, as it does after a real stall."""
        counter._events.clear()

    def open(self, category: str) -> bool:
        return self.log.has_open_event(category=category, feed_id="cam_a")


class RecordingBacklogWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _Rig()

    def tearDown(self) -> None:
        self.rig.close()

    def test_no_warning_while_recording_keeps_up(self) -> None:
        self.rig.queue.backlog_s = 0.3
        self.rig.tick(times=6)
        self.assertFalse(self.rig.open("recording_behind"))
        self.assertFalse(self.rig.open("recording_backpressure"))

    def test_a_backlog_of_a_second_opens_a_warning_after_two_samples(self) -> None:
        self.rig.queue.backlog_s = 1.4
        self.rig.tick(times=RECORD_BEHIND_STREAK - 1)
        self.assertFalse(self.rig.open("recording_behind"))
        self.rig.tick()
        self.assertTrue(self.rig.open("recording_behind"))
        event = next(e for e in self.rig.log.open_events() if e.category == "recording_behind")
        self.assertEqual(event.severity, "warning")
        self.assertIn("1.4 s behind", event.message)
        self.assertEqual(event.feed_id, "cam_a")

    def test_one_slow_sample_does_not_warn(self) -> None:
        self.rig.queue.backlog_s = 2.0
        self.rig.tick()
        self.rig.queue.backlog_s = 0.1
        self.rig.tick(times=3)
        self.assertFalse(self.rig.open("recording_behind"))

    def test_a_full_queue_opens_the_error_level_event_too(self) -> None:
        self.rig.queue.backlog_s = 3.9
        self.rig.tick(times=RECORD_BEHIND_STREAK)
        self.assertTrue(self.rig.open("recording_behind"))
        self.assertTrue(self.rig.open("recording_backpressure"))
        event = next(e for e in self.rig.log.open_events() if e.category == "recording_backpressure")
        self.assertEqual(event.severity, "error")

    def test_the_warning_clears_only_when_the_backlog_is_well_down(self) -> None:
        self.rig.queue.backlog_s = 2.0
        self.rig.tick(times=3)
        self.assertTrue(self.rig.open("recording_behind"))
        self.rig.queue.backlog_s = 0.8  # under the warn level, over the clear level
        self.rig.tick(times=4)
        self.assertTrue(self.rig.open("recording_behind"))  # hysteresis: still shown
        self.rig.queue.backlog_s = 0.2
        self.rig.tick(times=RECORD_BEHIND_STREAK)
        self.assertFalse(self.rig.open("recording_behind"))
        self.assertFalse(self.rig.open("recording_backpressure"))

    def test_the_warning_is_not_repeated_while_open(self) -> None:
        self.rig.queue.backlog_s = 2.0
        self.rig.tick(times=10)
        warnings = [e for e in self.rig.log.open_events() if e.category == "recording_behind"]
        self.assertEqual(len(warnings), 1)

    def test_it_can_open_again_after_it_cleared(self) -> None:
        self.rig.queue.backlog_s = 2.0
        self.rig.tick(times=3)
        self.rig.queue.backlog_s = 0.0
        self.rig.tick(times=3)
        self.assertFalse(self.rig.open("recording_behind"))
        self.rig.queue.backlog_s = 2.5
        self.rig.tick(times=3)
        self.assertTrue(self.rig.open("recording_behind"))

    def test_nothing_changes_the_recording_state(self) -> None:
        self.rig.queue.backlog_s = 3.9
        self.rig.tick(times=8)
        self.assertEqual(self.rig.recording.state, RecordingState.RECORDING)

    def test_no_warning_when_not_recording(self) -> None:
        rig = _Rig(recording=False)
        try:
            rig.queue.backlog_s = 3.9
            rig.tick(times=6)
            self.assertFalse(rig.open("recording_behind"))
            self.assertFalse(rig.open("recording_backpressure"))
        finally:
            rig.close()

    def test_stopping_recording_clears_a_warning(self) -> None:
        self.rig.queue.backlog_s = 2.0
        self.rig.tick(times=3)
        self.assertTrue(self.rig.open("recording_behind"))
        self.rig.recording.transition_to(RecordingState.STOPPING_RECORDING)
        self.rig.tick()
        self.assertFalse(self.rig.open("recording_behind"))

    def test_without_a_time_window_there_is_nothing_to_warn_about(self) -> None:
        self.rig.queue.__class__ = type("NoTime", (_Queue,), {
            "__call__": lambda s: {"preview": {}, "record": {"buffers": 100, "max_buffers": 256}}
        })
        self.rig.tick(times=6)
        self.assertFalse(self.rig.open("recording_behind"))

    def test_the_banner_shows_it_with_the_feed_and_the_numbers(self) -> None:
        self.rig.queue.backlog_s = 3.9
        self.rig.tick(times=RECORD_BEHIND_STREAK)
        state = select_banner_state(self.rig.log.open_events())
        self.assertTrue(state.visible)
        # The error-level event outranks the warning.
        self.assertEqual(state.primary.category, "recording_backpressure")
        text = format_banner_text(state)
        self.assertIn("Recording can't keep up", text)
        self.assertIn("[cam_a]", text)
        self.assertIn("3.9 s behind", text)
        self.assertEqual(state.extra_count, 1)

    def test_the_warning_level_alone_is_a_warning_banner(self) -> None:
        self.rig.queue.backlog_s = 1.6
        self.rig.tick(times=RECORD_BEHIND_STREAK)
        state = select_banner_state(self.rig.log.open_events())
        self.assertEqual(state.primary.category, "recording_behind")
        self.assertEqual(state.severity, "warning")
        self.assertIn("Recording behind live", format_banner_text(state))


class BackpressureIsNotALostSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _Rig()

    def tearDown(self) -> None:
        self.rig.close()

    def _stall(self, times: int, *, recording: bool = True) -> None:
        self.rig.tick(source=False, recording=recording, times=times)

    def test_a_stalled_ingest_with_a_free_queue_is_still_a_lost_source(self) -> None:
        self.rig.queue.backlog_s = 0.0
        self._stall(FEED_LOST_ZERO_SAMPLES)
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)
        self.assertTrue(self.rig.open("feed_lost"))

    def test_a_stalled_ingest_with_a_full_queue_is_held_not_declared_lost(self) -> None:
        self.rig.queue.backlog_s = 4.0
        self._stall(FEED_LOST_ZERO_SAMPLES + 10)
        self.assertIs(self.rig.feed.state, FeedState.LIVE)
        self.assertFalse(self.rig.open("feed_lost"))
        self.assertTrue(self.rig.open("recording_backpressure"))  # the operator is told

    def test_the_hold_is_announced_once(self) -> None:
        self.rig.queue.backlog_s = 4.0
        with self.assertLogs("app.core.telemetry", level="WARNING") as logs:
            self._stall(FEED_LOST_ZERO_SAMPLES + 6)
        holds = [line for line in logs.output if "treating it as back-pressure" in line]
        self.assertEqual(len(holds), 1)

    def test_a_half_full_queue_does_not_hold(self) -> None:
        self.rig.queue.backlog_s = 2.0  # 50% of the window: nothing is blocking
        self._stall(FEED_LOST_ZERO_SAMPLES)
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)

    def test_a_wedged_recording_branch_is_eventually_declared_lost(self) -> None:
        self.rig.queue.backlog_s = 4.0
        self._stall(FEED_LOST_ZERO_SAMPLES + RECORD_STUCK_SAMPLES - 2, recording=False)
        self.assertIs(self.rig.feed.state, FeedState.LIVE)  # not yet
        self._stall(4, recording=False)
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)

    def test_a_slow_but_moving_recording_branch_is_never_declared_lost(self) -> None:
        self.rig.queue.backlog_s = 4.0
        self._stall(FEED_LOST_ZERO_SAMPLES + RECORD_STUCK_SAMPLES * 3, recording=True)
        self.assertIs(self.rig.feed.state, FeedState.LIVE)

    def test_progress_resets_the_wedged_count(self) -> None:
        self.rig.queue.backlog_s = 4.0
        self._stall(FEED_LOST_ZERO_SAMPLES + RECORD_STUCK_SAMPLES - 3, recording=False)
        self._stall(1, recording=True)  # one frame of progress
        self._stall(RECORD_STUCK_SAMPLES - 3, recording=False)
        self.assertIs(self.rig.feed.state, FeedState.LIVE)

    def test_if_the_queue_drains_and_ingest_stays_dead_the_feed_is_declared_lost(self) -> None:
        self.rig.queue.backlog_s = 4.0
        self._stall(FEED_LOST_ZERO_SAMPLES + 2)
        self.assertIs(self.rig.feed.state, FeedState.LIVE)
        self.rig.queue.backlog_s = 0.0  # the backlog cleared, nothing is blocking now
        with self.assertLogs("app.core.telemetry", level="WARNING"):
            self._stall(1)
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)

    def test_ingest_resuming_ends_the_hold(self) -> None:
        self.rig.queue.backlog_s = 4.0
        self._stall(FEED_LOST_ZERO_SAMPLES + 4)
        self.rig.tick(source=True)  # frames flow again
        self.assertNotIn("cam_a", self.rig.hub._backpressure_hold)
        self.assertIs(self.rig.feed.state, FeedState.LIVE)
        # A later, real loss with a free queue is detected as before.
        self.rig.queue.backlog_s = 0.0
        self._stall(FEED_LOST_ZERO_SAMPLES)
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)

    def test_a_feed_that_was_already_lost_is_not_touched_by_the_hold_logic(self) -> None:
        self.rig.queue.backlog_s = 0.0
        self._stall(FEED_LOST_ZERO_SAMPLES)
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)
        self.rig.queue.backlog_s = 4.0
        self._stall(5)  # still dead; must not raise or flip anything
        self.assertIs(self.rig.feed.state, FeedState.DISCONNECTED)


class SnapshotBacklogTests(unittest.TestCase):
    def test_the_binding_limit_is_the_time_window_not_the_buffer_count(self) -> None:
        metrics = FeedMetrics("f", "F")
        metrics.set_queue_capacity(recording_max_buffers=256)
        metrics.set_queue_depths(recording_buffers=120)  # 47% by buffers
        metrics.set_queue_time(recording_ms=4000.0, recording_max_ms=4000.0)  # 100% by time
        snap = metrics.snapshot()
        self.assertAlmostEqual(snap.record_queue_saturation_pct, 46.875, places=2)
        self.assertEqual(snap.record_backlog_fraction, 1.0)
        self.assertEqual(snap.record_backlog_seconds, 4.0)

    def test_everything_reads_zero_without_a_sampler(self) -> None:
        snap = FeedMetrics("f", "F").snapshot()
        self.assertEqual(snap.record_backlog_fraction, 0.0)
        self.assertEqual(snap.record_backlog_seconds, 0.0)

    def test_the_log_line_reports_the_backlog(self) -> None:
        rig = _Rig()
        try:
            rig.queue.backlog_s = 2.5
            with self.assertLogs("app.core.telemetry", level="INFO") as logs:
                rig.tick()
            self.assertTrue(any("qrec_s=2.5" in line for line in logs.output))
        finally:
            rig.close()


if __name__ == "__main__":
    unittest.main()
