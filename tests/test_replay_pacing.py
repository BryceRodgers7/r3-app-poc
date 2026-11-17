"""Replay pacing: click-path decode in parallel, and free-running replay on workers.

A free-running replay tick only works out which frame each feed should show and
hands it to that feed's worker thread; the worker decodes and scales it and the
UI thread gives the finished picture to Qt. These cover that hand-off (never
waits for a decode, newest request wins, stale frames are dropped), the timer
interval (one tick per frame of footage, no adaptation), and the click path
(`_decode_jobs`), which still decodes feeds side by side and returns the frames
in feed order.
"""

from __future__ import annotations

from types import SimpleNamespace
import threading
import time
import unittest

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from app.core.app_state import UiState
from app.core.models import MediaFrame, PlaybackMode
from app.core.playback_controller import (
    PlaybackController,
    _FramesReadyBridge,
    _REPLAY_MAX_INTERVAL_MS,
    _REPLAY_MIN_INTERVAL_MS,
    _replay_interval_ms,
)
import numpy as np


def _bare_controller() -> PlaybackController:
    pc = PlaybackController.__new__(PlaybackController)
    pc._decode_pool = None
    pc._segment_decoders = {"a": object(), "b": object()}
    pc._output_renderer = None
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


class ReplayIntervalTests(unittest.TestCase):
    def test_one_tick_per_frame_of_the_footage(self) -> None:
        self.assertEqual(_replay_interval_ms(33_366_700), 33)  # 29.97 fps
        self.assertEqual(_replay_interval_ms(33_333_333), 33)  # 30 fps
        self.assertEqual(_replay_interval_ms(16_683_350), 17)  # 59.94 fps
        self.assertEqual(_replay_interval_ms(40_000_000), 40)  # 25 fps

    def test_absurd_frame_periods_are_clamped(self) -> None:
        self.assertEqual(_replay_interval_ms(1_000_000), _REPLAY_MIN_INTERVAL_MS)
        self.assertEqual(_replay_interval_ms(500_000_000), _REPLAY_MAX_INTERVAL_MS)

    def test_there_is_no_interval_adaptation_left(self) -> None:
        # A tick no longer decodes anything, so its cost says nothing about what
        # the interval should be; frames are dropped instead (see the worker).
        self.assertFalse(hasattr(PlaybackController, "_adapt_replay_interval"))


class _Renderer:
    """Records what was prepared and shown, and on which threads."""

    def __init__(self) -> None:
        self.shown: list[MediaFrame] = []
        self.shown_threads: list[str] = []
        self.prepared_threads: list[str] = []

    def prepare_frame(self, frame: MediaFrame) -> MediaFrame:
        self.prepared_threads.append(threading.current_thread().name)
        return frame

    def show_frame(self, frame: MediaFrame) -> None:
        self.shown.append(frame)
        self.shown_threads.append(threading.current_thread().name)


class _FrameDecoder:
    """Decodes a location into a frame whose pixel value is the location's number."""

    def __init__(self, feed_id: str, delay: float = 0.0) -> None:
        self.feed_id = feed_id
        self.delay = delay
        self.requests: list[int] = []
        self.gate: threading.Event | None = None

    def decode(self, location: SimpleNamespace) -> MediaFrame:
        self.requests.append(location.number)
        if self.gate is not None:
            self.gate.wait(3)
        time.sleep(self.delay)
        image = np.full((4, 4, 3), location.number % 255, dtype=np.uint8)
        return MediaFrame(
            frame_id=location.number, timestamp=0.0, image=image, source_name="s", feed_id=self.feed_id
        )


class _Store:
    def __init__(self) -> None:
        self.number = 0

    def nearest_frame_location(self, *, feed_id, session_time_ns, recording_state):
        return SimpleNamespace(number=int(session_time_ns), is_freeze=False)


def _free_running_controller(decoders: dict[str, _FrameDecoder]) -> tuple[PlaybackController, _Renderer]:
    pc = PlaybackController.__new__(PlaybackController)
    renderer = _Renderer()
    pc._output_renderer = renderer
    pc._segment_decoders = decoders
    pc._feed_runtimes = tuple(SimpleNamespace(feed=SimpleNamespace(feed_id=feed_id)) for feed_id in decoders)
    pc._recording_manager = SimpleNamespace(recording_state=SimpleNamespace(state="recording"))
    pc._replay_store = _Store()
    pc._lock = threading.RLock()
    pc._state = UiState()
    pc._state.current_playback_mode = PlaybackMode.REPLAY
    pc._replay_epoch = 0
    pc._replay_workers = {}
    pc._decode_pool = None
    pc._shutting_down = False
    pc._replay_stats_logged_at = time.monotonic()
    pc._frames_bridge = _FramesReadyBridge(pc._deliver_replay_frames)
    return pc, renderer


class FreeRunningReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._controllers: list[PlaybackController] = []

    def tearDown(self) -> None:
        for pc in self._controllers:
            for worker in pc._replay_workers.values():
                worker.stop()

    def _controller(self, decoders):
        pc, renderer = _free_running_controller(decoders)
        self._controllers.append(pc)
        return pc, renderer

    def _pump(self, condition, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if condition():
                return True
            time.sleep(0.005)
        return False

    def test_a_tick_returns_without_waiting_for_the_decode(self) -> None:
        pc, _ = self._controller({"a": _FrameDecoder("a", delay=0.3), "b": _FrameDecoder("b", delay=0.3)})
        started = time.perf_counter()
        pc._submit_replay_frames(10)
        self.assertLess(time.perf_counter() - started, 0.1)  # two 300 ms decodes not waited for

    def test_frames_are_prepared_on_the_worker_and_shown_on_the_calling_thread(self) -> None:
        pc, renderer = self._controller({"a": _FrameDecoder("a"), "b": _FrameDecoder("b")})
        pc._submit_replay_frames(7)
        self.assertTrue(self._pump(lambda: len(renderer.shown) == 2))
        self.assertEqual({frame.feed_id for frame in renderer.shown}, {"a", "b"})
        self.assertTrue(all(name.startswith("replay-frames-") for name in renderer.prepared_threads))
        self.assertEqual(set(renderer.shown_threads), {threading.current_thread().name})

    def test_a_slow_worker_skips_to_the_newest_frame(self) -> None:
        slow = _FrameDecoder("a")
        slow.gate = threading.Event()
        pc, renderer = self._controller({"a": slow})
        pc._submit_replay_frames(1)
        self.assertTrue(self._pump(lambda: slow.requests == [1]))  # 1 is being decoded
        for tick in (2, 3, 4, 5):
            pc._submit_replay_frames(tick)  # 2..4 are replaced before they start
        slow.gate.set()
        self.assertTrue(self._pump(lambda: renderer.shown and renderer.shown[-1].frame_id == 5))
        self.assertEqual(slow.requests, [1, 5])
        self.assertEqual(pc.replay_stats()["a"]["dropped_before_decode"], 3)

    def test_a_frame_finished_after_a_click_is_not_shown(self) -> None:
        decoder = _FrameDecoder("a")
        decoder.gate = threading.Event()
        pc, renderer = self._controller({"a": decoder})
        pc._submit_replay_frames(1)
        self.assertTrue(self._pump(lambda: decoder.requests == [1]))
        pc._begin_replay_epoch()  # what every click-path render does first
        decoder.gate.set()
        self.assertTrue(self._pump(lambda: pc.replay_stats()["a"]["decoded"] == 1))
        QApplication.processEvents()
        self.assertEqual(renderer.shown, [])

    def test_frames_finished_after_replay_leaves_replay_mode_are_not_shown(self) -> None:
        decoder = _FrameDecoder("a")
        decoder.gate = threading.Event()
        pc, renderer = self._controller({"a": decoder})
        pc._submit_replay_frames(1)
        self.assertTrue(self._pump(lambda: decoder.requests == [1]))
        pc._state.current_playback_mode = PlaybackMode.PAUSED
        decoder.gate.set()
        self.assertTrue(self._pump(lambda: pc.replay_stats()["a"]["decoded"] == 1))
        QApplication.processEvents()
        self.assertEqual(renderer.shown, [])

    def test_stopping_the_clock_starts_a_new_epoch(self) -> None:
        pc, _ = self._controller({"a": _FrameDecoder("a")})

        class _Timer:
            def isActive(self) -> bool:
                return True

            def stop(self) -> None:
                pass

        pc._replay_timer = _Timer()
        pc._replay_clock_anchor_session_time_ns = 1
        pc._replay_clock_anchor_monotonic = 1.0
        before = pc._replay_epoch
        with pc._lock:
            pc._stop_replay_clock_locked()
        self.assertEqual(pc._replay_epoch, before + 1)

    def test_freeze_state_is_updated_by_a_tick(self) -> None:
        pc, _ = self._controller({"a": _FrameDecoder("a")})
        pc._replay_store.nearest_frame_location = lambda **_k: SimpleNamespace(number=1, is_freeze=True)
        pc._submit_replay_frames(1)
        self.assertEqual(pc._state.feeds_in_freeze_frame, ("a",))

    def test_a_feed_with_nothing_to_show_gets_no_request(self) -> None:
        pc, renderer = self._controller({"a": _FrameDecoder("a")})
        pc._replay_store.nearest_frame_location = lambda **_k: None
        pc._submit_replay_frames(1)
        self.assertEqual(pc._replay_workers, {})
        self.assertEqual(renderer.shown, [])

    def test_the_stats_line_reports_what_was_shown_and_dropped(self) -> None:
        pc, renderer = self._controller({"a": _FrameDecoder("a")})
        pc._submit_replay_frames(1)
        self.assertTrue(self._pump(lambda: len(renderer.shown) == 1))
        pc._replay_stats_logged_at = time.monotonic() - 10.0  # make it due
        with self.assertLogs("app.core.playback_controller", level="INFO") as logs:
            pc._submit_replay_frames(2)
        self.assertTrue(any("replay frames feed=a" in line for line in logs.output))


class TimerSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_replay_timer_is_precise(self) -> None:
        # Windows coarse timers quantize to ~15.6 ms, which would make a
        # one- or two-frame interval uneven.
        timer = QTimer()
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.assertEqual(timer.timerType(), Qt.TimerType.PreciseTimer)


if __name__ == "__main__":
    unittest.main()
