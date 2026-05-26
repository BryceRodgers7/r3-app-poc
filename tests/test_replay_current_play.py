"""Phase 7.H.4 / 14.C: `replay_current_play` controller primitive.

Phase 14.C removed the explicit Replay Play UI button; the primitive
stays on the controller and is repurposed by Phase 14.D as the
challenge-open hook (snap to a play's start, install bounds, pause).
These tests pin the no-op + happy-path behavior the 14.D wiring
relies on.
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.core.app_state import UiState
from app.core.models import (
    CLIP_TYPE_PLAY,
    Clip,
    PlaybackMode,
    PlaybackOverlayInfo,
)
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState, make_replay_state_machine
from app.core.signals import AppSignals


def _make_play_clip(*, play_number: int, start_ns: int, clip_number: int = 1) -> Clip:
    return Clip(
        session_id="session_001",
        game_subdir="game_001",
        clip_number=clip_number,
        type=CLIP_TYPE_PLAY,
        play_number=play_number,
        start_session_time_ns=start_ns,
        created_at="2026-04-30T00:00:00+00:00",
    )


def _build_pc_stub(
    *,
    is_recording: bool,
    clip_manager,
    live_only: bool = False,
    earliest_replayable_ns: int | None = 0,
) -> PlaybackController:
    """Stub PlaybackController exercising `replay_current_play`."""
    pc = PlaybackController.__new__(PlaybackController)
    pc._lock = mock.MagicMock()
    pc._lock.__enter__ = lambda self: None
    pc._lock.__exit__ = lambda self, *args: None
    pc._state = UiState()
    pc._state.is_recording = is_recording
    pc._state.current_playback_mode = PlaybackMode.LIVE
    pc._state.source_connected = True
    pc._state.replay_buffer_span_seconds = 0.0
    pc._latest_live_timestamp = None
    pc._latest_live_overlay = type("F", (), {"feed_id": "f"})()
    pc._playback_session_time_ns = None
    pc._playback_rate = 1.0
    pc._session_clock = None
    pc._clip_manager = clip_manager
    pc._live_only = live_only
    pc._replay_store = mock.Mock()
    pc._replay_store.available_session_time_range.return_value = (
        earliest_replayable_ns,
        20_000_000_000,
    )
    # _replay_actions_allowed reads is_replay_available — gate it on
    # the recording flag so the not-recording path early-returns.
    pc._replay_store.is_replay_available.return_value = is_recording
    rm = mock.Mock()
    rm.recording_state = type("S", (), {})()
    rm.recording_state.state = (
        RecordingState.RECORDING if is_recording else RecordingState.NOT_RECORDING
    )
    pc._recording_manager = rm
    pc.replay_state = make_replay_state_machine(role="operator")
    if is_recording:
        # Move replay state machine into LIVE_WHILE_RECORDING so the
        # SEEKING transition is valid.
        pc.replay_state.transition_to(ReplayState.LIVE_WHILE_RECORDING)
    pc.signals = AppSignals()
    pc._render_at_session_time_ns = mock.Mock()
    pc._start_replay_clock_locked = mock.Mock()
    pc._update_state_timestamps_locked = mock.Mock()
    pc._emit_state = mock.Mock()
    return pc


class ReplayCurrentPlayHappyPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_seeks_to_play_start_and_enters_replay(self) -> None:
        play = _make_play_clip(play_number=2, start_ns=8_000_000_000)
        pm = mock.Mock()
        pm.current_clip.return_value = play
        pc = _build_pc_stub(is_recording=True, clip_manager=pm)
        pc.replay_current_play()
        # Playback session time = the play's start.
        self.assertEqual(pc._playback_session_time_ns, 8_000_000_000)
        self.assertEqual(pc._state.current_playback_mode, PlaybackMode.REPLAY)
        self.assertEqual(pc._playback_rate, 1.0)
        # Replay clock anchored.
        pc._start_replay_clock_locked.assert_called_once_with(8_000_000_000)
        # Renderer driven for the seek target.
        pc._render_at_session_time_ns.assert_called_once_with(8_000_000_000)

    def test_clamps_to_earliest_replayable_when_play_start_is_before_coverage(
        self,
    ) -> None:
        # Defensive: play marker at 7s but earliest replayable is 9s
        # (e.g. operator pressed Start before the first segment
        # finalized). Seek should clamp to 9s; the §8.6.1 freeze-frame
        # rule would then render the first frame as a freeze.
        play = _make_play_clip(play_number=1, start_ns=7_000_000_000)
        pm = mock.Mock()
        pm.current_clip.return_value = play
        pc = _build_pc_stub(
            is_recording=True,
            clip_manager=pm,
            earliest_replayable_ns=9_000_000_000,
        )
        pc.replay_current_play()
        self.assertEqual(pc._playback_session_time_ns, 9_000_000_000)
        pc._render_at_session_time_ns.assert_called_once_with(9_000_000_000)

    def test_status_message_includes_play_number(self) -> None:
        play = _make_play_clip(play_number=5, start_ns=20_000_000_000)
        pm = mock.Mock()
        pm.current_clip.return_value = play
        pc = _build_pc_stub(is_recording=True, clip_manager=pm)
        pc.replay_current_play()
        pc._emit_state.assert_called_once_with("Replaying Play #5")


class ReplayCurrentPlayNoOpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_no_op_when_live_only(self) -> None:
        pm = mock.Mock()
        pm.current_clip.return_value = _make_play_clip(play_number=1, start_ns=0)
        pc = _build_pc_stub(is_recording=True, clip_manager=pm, live_only=True)
        pc.replay_current_play()
        pc._render_at_session_time_ns.assert_not_called()
        pc._start_replay_clock_locked.assert_not_called()
        # State emission still happens (status message).
        pc._emit_state.assert_called_once_with("This output is locked to live.")

    def test_no_op_when_not_recording(self) -> None:
        pm = mock.Mock()
        pc = _build_pc_stub(is_recording=False, clip_manager=pm)
        pc.replay_current_play()
        pc._render_at_session_time_ns.assert_not_called()
        pc._emit_state.assert_called_once_with(
            "Replay unavailable: start game recording first."
        )

    def test_no_op_when_no_clip_manager(self) -> None:
        pc = _build_pc_stub(is_recording=True, clip_manager=None)
        pc.replay_current_play()
        pc._render_at_session_time_ns.assert_not_called()
        pc._emit_state.assert_not_called()

    def test_no_op_when_no_open_play(self) -> None:
        # ClipManager is attached but no clip is open (defensive —
        # shouldn't happen during RECORDING but locked in).
        pm = mock.Mock()
        pm.current_clip.return_value = None
        pc = _build_pc_stub(is_recording=True, clip_manager=pm)
        pc.replay_current_play()
        pc._render_at_session_time_ns.assert_not_called()
        pc._emit_state.assert_called_once_with("No play is currently open.")

    def test_no_op_when_current_clip_is_not_a_play(self) -> None:
        # During pre-game / timeout / challenge, replay_current_play
        # has no target play to seek to. Until 14.D extends this to
        # the most-recent play, the current clip must be a play.
        from app.core.models import CLIP_TYPE_PRE_GAME
        pm = mock.Mock()
        pm.current_clip.return_value = Clip(
            session_id="session_001",
            game_subdir="game_001",
            clip_number=0,
            type=CLIP_TYPE_PRE_GAME,
            play_number=None,
            start_session_time_ns=0,
            created_at="2026-04-30T00:00:00+00:00",
        )
        pc = _build_pc_stub(is_recording=True, clip_manager=pm)
        pc.replay_current_play()
        pc._render_at_session_time_ns.assert_not_called()
        pc._emit_state.assert_called_once_with("No play is currently open.")


if __name__ == "__main__":
    unittest.main()
