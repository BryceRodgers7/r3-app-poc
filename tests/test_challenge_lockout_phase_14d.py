"""Phase 14.D: challenge lockout — fence + cross-window wiring.

Two layers covered:

1. `PlaybackController` fence behavior — `set_clip_bounds` /
   `clear_clip_bounds` and how every seek primitive clamps against
   the fence. (Builds on the existing fixture in
   `test_playback_controller.py` — the 0..20s 5-segment timeline.)
2. `ApplicationCoordinator.mark_challenge` installing the fence on
   the referee controller, and `mark_next_play` /  `mark_timeout` /
   `toggle_long_session_recording` (stop branch) clearing it.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.core.models import (
    Clip,
    FeedDefinition,
    FrameOverlayInfo,
    MediaFrame,
    PlaybackMode,
    SessionPaths,
)
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState
from app.media.feed_runtime import FeedRuntime
from app.media.recording_manager import RecordingManager
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import RecordingSegmentReplayStore

# Sibling test module — reused fixtures (5×4 s segment timeline,
# fake decoder/renderer/source/pipeline). Unittest's loader adds
# `tests/` to sys.path so this resolves at import time.
from test_playback_controller import (  # noqa: E402
    _FakePipelineManager,
    _FakeRenderer,
    _FakeSource,
    _StubSegmentDecoder,
    _make_segment,
)


class ClipBoundsFenceTests(unittest.TestCase):
    """Build the standard 5×4 s segment fixture (0..20 s), install a
    challenge fence at [4 s, 12 s], and verify every primitive clamps."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root_dir = Path(self._temp_dir.name) / "session_001"
        recording_dir = root_dir / "recording"
        for path in (root_dir, recording_dir):
            path.mkdir(parents=True, exist_ok=True)

        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root_dir,
            recording_dir=recording_dir,
        )
        self.feed = FeedDefinition(feed_id="feed_main", display_name="Fake Source")
        fake_source = _FakeSource(self.feed.feed_id)
        fake_pipeline = _FakePipelineManager(self.feed.feed_id)
        self.recording_manager = RecordingManager()

        self.segment_index = SegmentIndex()
        for i in range(5):
            self.segment_index.add(
                _make_segment(fragment_index=i, start_pts_ns=i * 4_000_000_000)
            )
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)
        self.stub_decoder = _StubSegmentDecoder(
            self.feed.feed_id, self.feed.display_name
        )

        self.runtime = FeedRuntime(
            feed=self.feed,
            source=fake_source,
            pipeline_manager=fake_pipeline,
        )
        self.runtime.start(self.session_paths)

        # Prime a few live frames so the controller has overlay state.
        for frame_id in range(2):
            timestamp = 100.0 + frame_id
            image = np.full((24, 32, 3), frame_id % 255, dtype=np.uint8)
            frame = MediaFrame(
                frame_id=frame_id,
                timestamp=timestamp,
                image=image,
                source_name="Fake Source",
                feed_id=self.feed.feed_id,
            )
            self.runtime._on_live_frame(frame)
            self.runtime._on_live_overlay(
                FrameOverlayInfo.from_media_frame(frame, feed_id=self.feed.feed_id)
            )

        self.renderer = _FakeRenderer()
        self.controller = PlaybackController(
            feed_runtimes=[self.runtime],
            output_renderer=self.renderer,
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Fake Source",
            session_role="operator",
            live_only=False,
            decoder_factory=lambda *_args: self.stub_decoder,
            rewind_seconds=10,
        )
        self.controller.initialize(self.session_paths.session_id)
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()

    def tearDown(self) -> None:
        self.controller.shutdown()
        self._temp_dir.cleanup()

    # ------------------------------------------------------------------
    # Bounds primitives
    # ------------------------------------------------------------------

    def test_set_and_clear_clip_bounds(self) -> None:
        self.assertIsNone(self.controller._clip_bounds)
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        self.assertEqual(
            self.controller._clip_bounds, (4_000_000_000, 12_000_000_000)
        )
        self.controller.clear_clip_bounds()
        self.assertIsNone(self.controller._clip_bounds)

    # ------------------------------------------------------------------
    # Rewind clamping
    # ------------------------------------------------------------------

    def test_rewind_clamps_to_fence_start_and_lands_paused(self) -> None:
        """Operator's playback position is at 10s; rewind by 10s would
        land at 0s, which is below the fence start (4s). Result: clamp
        to 4s, bounce to PAUSED (not REPLAYING), and emit the fence
        status."""
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 10_000_000_000
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        status_messages: list[str] = []
        self.controller.signals.status_message.connect(status_messages.append)

        self.controller.rewind_configured_seconds()

        self.assertEqual(
            self.controller._playback_session_time_ns, 4_000_000_000
        )
        self.assertEqual(
            self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED
        )
        self.assertEqual(self.controller._playback_rate, 0.0)
        self.assertIn("Held at start of play (challenge)", status_messages)

    def test_rewind_inside_fence_still_resumes_replaying(self) -> None:
        """Rewind from 10s by 4s = 6s, which is inside [4,12] — no
        fence hit, normal REPLAYING resume."""
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 10_000_000_000
        # Need 4s rewind to land inside [4,12]; rebuild with 4s rewind.
        self.controller._rewind_ns = 4 * 1_000_000_000
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)

        self.controller.rewind_configured_seconds()

        self.assertEqual(
            self.controller._playback_session_time_ns, 6_000_000_000
        )
        self.assertEqual(
            self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY
        )

    # ------------------------------------------------------------------
    # Step clamping
    # ------------------------------------------------------------------

    def _enter_paused_at(self, session_time_ns: int) -> None:
        """Legitimately land in PAUSED at a known position — needed
        so `step_frames` anchors on `_playback_session_time_ns`
        instead of `latest_replayable`."""
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = session_time_ns
            self.controller.replay_state.transition_to(ReplayState.SEEKING)
            self.controller.replay_state.transition_to(ReplayState.REPLAYING)
        self.controller.pause_playback()

    def test_step_past_fence_end_clamps_and_emits_fence_status(self) -> None:
        self._enter_paused_at(11_999_000_000)
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        status_messages: list[str] = []
        self.controller.signals.status_message.connect(status_messages.append)

        # 10 frames @ 30 fps = ~333 ms forward — overshoots fence end.
        self.controller.step_frames(10)

        self.assertEqual(
            self.controller._playback_session_time_ns, 12_000_000_000
        )
        self.assertIn("Held at end of play (challenge)", status_messages)

    def test_step_before_fence_start_clamps(self) -> None:
        self._enter_paused_at(4_001_000_000)
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        status_messages: list[str] = []
        self.controller.signals.status_message.connect(status_messages.append)

        self.controller.step_frames(-30)

        self.assertEqual(
            self.controller._playback_session_time_ns, 4_000_000_000
        )
        self.assertIn("Held at start of play (challenge)", status_messages)

    # ------------------------------------------------------------------
    # seek_to_session_time clamping
    # ------------------------------------------------------------------

    def test_seek_to_session_time_clamps_to_fence_high(self) -> None:
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        status_messages: list[str] = []
        self.controller.signals.status_message.connect(status_messages.append)

        self.controller.seek_to_session_time(18_000_000_000)

        self.assertEqual(
            self.controller._playback_session_time_ns, 12_000_000_000
        )
        self.assertIn("Held at end of play (challenge)", status_messages)

    def test_seek_to_session_time_clamps_to_fence_low(self) -> None:
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        status_messages: list[str] = []
        self.controller.signals.status_message.connect(status_messages.append)

        self.controller.seek_to_session_time(1_000_000_000)

        self.assertEqual(
            self.controller._playback_session_time_ns, 4_000_000_000
        )
        self.assertIn("Held at start of play (challenge)", status_messages)

    # ------------------------------------------------------------------
    # set_playback_rate behavior under a fence
    # ------------------------------------------------------------------

    def test_set_playback_rate_from_live_snaps_to_fence_start(self) -> None:
        """Without a fence, entering 1× from live snaps to latest=20s.
        With a fence, it snaps to the fence's lower edge — the
        referee starts watching the challenged play from its
        beginning."""
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        self.controller.set_playback_rate(1.0)
        self.assertEqual(
            self.controller._playback_session_time_ns, 4_000_000_000
        )

    # ------------------------------------------------------------------
    # Replay clock tick auto-pause at fence end
    # ------------------------------------------------------------------

    def test_replay_clock_tick_snaps_to_fence_end_and_pauses(self) -> None:
        """Set the replay clock's anchor just before the fence end
        plus a long elapsed time so the natural-rate advance would
        cross the upper edge. Tick should snap to `end` and PAUSE."""
        # Enter REPLAY at 11.9s, rate 1×.
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 11_900_000_000
            self.controller._playback_rate = 1.0
            self.controller._replay_clock_anchor_session_time_ns = 11_900_000_000
            self.controller._replay_clock_anchor_monotonic = 0.0
            self.controller.replay_state.transition_to(ReplayState.SEEKING)
            self.controller.replay_state.transition_to(ReplayState.REPLAYING)
        # Force monotonic time to look like 1 s has passed → elapsed
        # would push us to 12.9s, well past the fence.
        with mock.patch(
            "app.core.playback_controller.time.monotonic",
            return_value=1.0,
        ):
            self.controller._on_replay_timer_tick()

        self.assertEqual(
            self.controller._playback_session_time_ns, 12_000_000_000
        )
        self.assertEqual(
            self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED
        )
        self.assertEqual(self.controller._playback_rate, 0.0)

    # ------------------------------------------------------------------
    # Clear restores full range
    # ------------------------------------------------------------------

    def test_clear_clip_bounds_restores_full_range(self) -> None:
        self.controller.set_clip_bounds(4_000_000_000, 12_000_000_000)
        self.controller.clear_clip_bounds()
        # Now a seek to 18s lands at 18s (not the fence end).
        self.controller.seek_to_session_time(18_000_000_000)
        self.assertEqual(
            self.controller._playback_session_time_ns, 18_000_000_000
        )


class CoordinatorChallengeWiringTests(unittest.TestCase):
    """Coordinator-level wiring: Challenge installs the fence,
    Next-Play / Time-out / End-Game clear it, back-to-back Challenge
    no-ops the second press."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _build_coord(self):
        from app.core.application_coordinator import (
            ApplicationCoordinator,
            _CoordinatorSignals,
        )
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._shutting_down = False
        coord._recording_manager = mock.Mock()
        coord._recording_manager.is_any_recording.return_value = True
        coord.referee_controller = mock.Mock()
        coord.operator_controller = mock.Mock()
        coord.session_clock = mock.Mock()
        coord.session_clock.now_session_time_ns.return_value = 18_000_000_000
        coord.clip_manager = mock.Mock()
        coord.signals = _CoordinatorSignals()
        coord._challenge_active = False
        return coord

    def test_mark_challenge_installs_fence_on_referee(self) -> None:
        coord = self._build_coord()
        challenge_clip = Clip(
            session_id="s1",
            game_subdir="game_001",
            clip_number=3,
            type="challenge",
            play_number=None,
            start_session_time_ns=18_000_000_000,
            created_at="2026-05-26T00:00:00+00:00",
        )
        coord.clip_manager.mark_challenge.return_value = challenge_clip
        coord.clip_manager.last_play_bounds.return_value = (
            10_000_000_000,
            18_000_000_000,
        )
        emitted: list[bool] = []
        coord.signals.challenge_state_changed.connect(emitted.append)

        coord.mark_challenge()

        coord.referee_controller.replay_current_play.assert_called_once_with(
            10_000_000_000, 18_000_000_000
        )
        # Operator controller (live_only) is not fenced.
        coord.operator_controller.replay_current_play.assert_not_called()
        self.assertEqual(emitted, [True])
        self.assertTrue(coord._challenge_active)

    def test_back_to_back_challenge_no_ops_the_second(self) -> None:
        coord = self._build_coord()
        # ClipManager rejects the second press (returns None).
        coord.clip_manager.mark_challenge.return_value = None
        emitted: list[bool] = []
        coord.signals.challenge_state_changed.connect(emitted.append)

        coord.mark_challenge()

        coord.referee_controller.replay_current_play.assert_not_called()
        self.assertEqual(emitted, [])
        self.assertFalse(coord._challenge_active)

    def test_mark_next_play_clears_fence(self) -> None:
        coord = self._build_coord()
        coord._challenge_active = True
        coord.referee_controller.clear_clip_bounds = mock.Mock()
        next_clip = mock.Mock()
        next_clip.play_number = 4
        coord.clip_manager.mark_next_play.return_value = next_clip
        emitted: list[bool] = []
        coord.signals.challenge_state_changed.connect(emitted.append)

        coord.mark_next_play()

        coord.referee_controller.clear_clip_bounds.assert_called_once()
        self.assertEqual(emitted, [False])
        self.assertFalse(coord._challenge_active)

    def test_mark_timeout_clears_fence(self) -> None:
        coord = self._build_coord()
        coord._challenge_active = True
        coord.referee_controller.clear_clip_bounds = mock.Mock()
        timeout_clip = mock.Mock()
        coord.clip_manager.mark_timeout.return_value = timeout_clip
        emitted: list[bool] = []
        coord.signals.challenge_state_changed.connect(emitted.append)

        coord.mark_timeout()

        coord.referee_controller.clear_clip_bounds.assert_called_once()
        self.assertEqual(emitted, [False])

    def test_mark_next_play_does_not_emit_when_no_fence_active(self) -> None:
        coord = self._build_coord()
        # _challenge_active stays False — Next Play during normal play
        # shouldn't cause a spurious challenge_state_changed emission.
        next_clip = mock.Mock()
        next_clip.play_number = 2
        coord.clip_manager.mark_next_play.return_value = next_clip
        emitted: list[bool] = []
        coord.signals.challenge_state_changed.connect(emitted.append)

        coord.mark_next_play()

        coord.referee_controller.clear_clip_bounds.assert_not_called()
        self.assertEqual(emitted, [])


class ClipManagerLastPlayBoundsTests(unittest.TestCase):
    """ClipManager exposes `last_play_bounds()` for the Challenge hot
    path. The cache is populated when a play closes and reset on
    `start_game` / `stop_game`."""

    def test_last_play_bounds_starts_none(self) -> None:
        from app.core.clip_manager import ClipManager
        cm = ClipManager(db=mock.Mock())
        self.assertIsNone(cm.last_play_bounds())

    def test_closing_a_play_populates_bounds(self) -> None:
        from app.core.clip_manager import ClipManager
        db = mock.Mock()
        db.clips_for_game.return_value = []
        db.insert_clip.side_effect = iter(range(1, 100))
        cm = ClipManager(db=db)
        cm.start_game(
            session_id="s1",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        # Open play #1.
        cm.mark_next_play(now_session_time_ns=2_000_000_000)
        # Close the play by opening a timeout.
        cm.mark_timeout(now_session_time_ns=10_000_000_000)
        # The just-closed play's bounds are now cached.
        self.assertEqual(
            cm.last_play_bounds(), (2_000_000_000, 10_000_000_000)
        )

    def test_stop_game_resets_bounds(self) -> None:
        from app.core.clip_manager import ClipManager
        db = mock.Mock()
        db.clips_for_game.return_value = []
        db.insert_clip.side_effect = iter(range(1, 100))
        cm = ClipManager(db=db)
        cm.start_game(
            session_id="s1",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        cm.mark_next_play(now_session_time_ns=2_000_000_000)
        cm.mark_timeout(now_session_time_ns=10_000_000_000)
        self.assertIsNotNone(cm.last_play_bounds())
        cm.stop_game(end_session_time_ns=12_000_000_000)
        self.assertIsNone(cm.last_play_bounds())


if __name__ == "__main__":
    unittest.main()
