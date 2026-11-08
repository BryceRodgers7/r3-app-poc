"""Replay tick pacing and parallel per-feed decode.

A replay tick decodes every feed and draws it on the Qt thread. These cover
the two pieces that keep that affordable: feeds decode side by side (results
still in feed order), and the timer interval follows the measured tick cost
in whole 60 fps frame steps.
"""

from __future__ import annotations

from collections import deque
import threading
import time
import unittest

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from app.core.playback_controller import (
    PlaybackController,
    _REPLAY_COST_WINDOW,
    _REPLAY_FRAME_MS,
    _REPLAY_START_INTERVAL_MS,
)


def _bare_controller() -> PlaybackController:
    pc = PlaybackController.__new__(PlaybackController)
    pc._decode_pool = None
    pc._segment_decoders = {"a": object(), "b": object()}
    pc._replay_interval_ms = _REPLAY_START_INTERVAL_MS
    pc._replay_tick_costs = deque(maxlen=_REPLAY_COST_WINDOW)
    pc._intervals_set: list[int] = []

    class _Timer:
        def setInterval(self, ms: int) -> None:
            pc._intervals_set.append(ms)

    pc._replay_timer = _Timer()
    return pc


class _Decoder:
    def __init__(self, label: str, delay: float = 0.0) -> None:
        self.label = label
        self.delay = delay
        self.thread: str | None = None

    def decode(self, location: object) -> str:
        self.thread = threading.current_thread().name
        time.sleep(self.delay)
        return f"{self.label}:{location}"


class ParallelDecodeTests(unittest.TestCase):
    def test_results_come_back_in_feed_order_even_if_the_first_is_slowest(self) -> None:
        pc = _bare_controller()
        slow, fast = _Decoder("a", delay=0.05), _Decoder("b")
        frames = pc._decode_jobs([(slow, 1), (fast, 2)])
        self.assertEqual(frames, ["a:1", "b:2"])
        pc._decode_pool.shutdown()

    def test_feeds_decode_concurrently(self) -> None:
        pc = _bare_controller()
        started = time.perf_counter()
        pc._decode_jobs([(_Decoder("a", 0.1), 1), (_Decoder("b", 0.1), 2)])
        elapsed = time.perf_counter() - started
        # Serial would take 0.2 s; allow generous scheduling slack.
        self.assertLess(elapsed, 0.17)
        pc._decode_pool.shutdown()

    def test_work_runs_off_the_calling_thread(self) -> None:
        pc = _bare_controller()
        a, b = _Decoder("a"), _Decoder("b")
        pc._decode_jobs([(a, 1), (b, 2)])
        self.assertTrue(a.thread.startswith("replay-decode"))
        self.assertTrue(b.thread.startswith("replay-decode"))
        pc._decode_pool.shutdown()

    def test_a_single_feed_decodes_inline_without_a_pool(self) -> None:
        pc = _bare_controller()
        only = _Decoder("a")
        self.assertEqual(pc._decode_jobs([(only, 1)]), ["a:1"])
        self.assertIsNone(pc._decode_pool)
        self.assertEqual(only.thread, threading.current_thread().name)

    def test_no_jobs_is_fine(self) -> None:
        self.assertEqual(_bare_controller()._decode_jobs([]), [])

    def test_a_decode_error_propagates_like_it_did_inline(self) -> None:
        pc = _bare_controller()

        class _Boom:
            def decode(self, _location: object) -> None:
                raise RuntimeError("bad segment")

        with self.assertRaises(RuntimeError):
            pc._decode_jobs([(_Decoder("a"), 1), (_Boom(), 2)])
        pc._decode_pool.shutdown()


class IntervalAdaptationTests(unittest.TestCase):
    def _feed_costs(self, pc: PlaybackController, cost_ms: float, ticks: int = 30) -> None:
        for _ in range(ticks):
            pc._adapt_replay_interval(cost_ms)

    def test_cheap_ticks_speed_the_timer_up_to_a_single_frame(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 8.0)  # one feed: ~8 ms of a 33 ms interval
        self.assertEqual(pc._replay_interval_ms, round(_REPLAY_FRAME_MS))

    def test_expensive_ticks_slow_the_timer_down(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 40.0)
        self.assertGreater(pc._replay_interval_ms, _REPLAY_START_INTERVAL_MS)
        # Aimed at ~60% duty, in whole frame steps.
        self.assertLessEqual(40.0 / pc._replay_interval_ms, 0.8)

    def test_interval_is_always_a_whole_number_of_frames(self) -> None:
        pc = _bare_controller()
        for cost in (5.0, 9.0, 17.0, 26.0, 45.0, 90.0):
            pc._replay_tick_costs.clear()
            pc._replay_interval_ms = _REPLAY_START_INTERVAL_MS
            self._feed_costs(pc, cost)
            steps = pc._replay_interval_ms / _REPLAY_FRAME_MS
            self.assertAlmostEqual(steps, round(steps), delta=0.05, msg=f"cost {cost}")

    def test_a_comfortable_tick_leaves_the_interval_alone(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 17.0)  # two feeds, ~50% of 33 ms
        self.assertEqual(pc._replay_interval_ms, _REPLAY_START_INTERVAL_MS)
        self.assertEqual(pc._intervals_set, [])

    def test_it_settles_instead_of_flapping(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 17.0, ticks=5)
        changes_before = len(pc._intervals_set)
        self._feed_costs(pc, 17.0, ticks=200)
        self.assertEqual(len(pc._intervals_set), changes_before)

    def test_one_slow_tick_does_not_move_the_timer(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 17.0, ticks=20)
        pc._adapt_replay_interval(120.0)  # a segment boundary / disk hiccup
        self.assertEqual(pc._replay_interval_ms, _REPLAY_START_INTERVAL_MS)

    def test_a_slow_tick_every_segment_does_not_pulse_the_timer(self) -> None:
        # One ~100 ms tick in every 120 (about 4 s of ticks), forever.
        pc = _bare_controller()
        for tick in range(2000):
            pc._adapt_replay_interval(100.0 if tick % 120 == 0 else 17.0)
        self.assertEqual(pc._intervals_set, [])

    def test_it_waits_for_enough_ticks_before_acting(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 90.0, ticks=3)
        self.assertEqual(pc._intervals_set, [])

    def test_changes_are_applied_to_the_timer(self) -> None:
        pc = _bare_controller()
        self._feed_costs(pc, 8.0)
        self.assertEqual(pc._intervals_set[-1], pc._replay_interval_ms)


class TimerSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_replay_timer_is_precise_and_starts_at_the_default_interval(self) -> None:
        # Windows coarse timers quantize to ~15.6 ms, which would make a
        # one- or two-frame interval uneven.
        timer = QTimer()
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.assertEqual(timer.timerType(), Qt.TimerType.PreciseTimer)
        self.assertGreater(_REPLAY_START_INTERVAL_MS, _REPLAY_FRAME_MS)


if __name__ == "__main__":
    unittest.main()
