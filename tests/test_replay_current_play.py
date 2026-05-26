"""Phase 14.D: `replay_current_play` as the challenge-open hook.

Pre-14.D this primitive took no args, looked up the currently-open
play via ClipManager, and resumed at 1.0× from its start. Phase 14.D
repurposed it: callers (specifically `ApplicationCoordinator.mark_challenge`)
pass explicit `(start_ns, end_ns)` bounds; the controller snaps to
`start`, installs the bounds as a replay fence, and lands in PAUSED
so the referee chooses when to resume.

These tests pin the new contract.
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.core.app_state import UiState
from app.core.models import PlaybackMode
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState, make_replay_state_machine
from app.core.signals import AppSignals


def _build_pc_stub(
    *,
    is_recording: bool,
    live_only: bool = False,
    earliest_replayable_ns: int | None = 0,
    latest_replayable_ns: int | None = 20_000_000_000,
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
    pc._clip_manager = None
    pc._live_only = live_only
    pc._clip_bounds = None
    pc._replay_store = mock.Mock()
    pc._replay_store.available_session_time_range.return_value = (
        earliest_replayable_ns,
        latest_replayable_ns,
    )
    pc._replay_store.is_replay_available.return_value = is_recording
    rm = mock.Mock()
    rm.recording_state = type("S", (), {})()
    rm.recording_state.state = (
        RecordingState.RECORDING if is_recording else RecordingState.NOT_RECORDING
    )
    pc._recording_manager = rm
    pc.replay_state = make_replay_state_machine(role="operator")
    if is_recording:
        pc.replay_state.transition_to(ReplayState.LIVE_WHILE_RECORDING)
    pc.signals = AppSignals()
    pc._render_at_session_time_ns = mock.Mock()
    pc._stop_replay_clock_locked = mock.Mock()
    pc._update_state_timestamps_locked = mock.Mock()
    pc._emit_state = mock.Mock()
    return pc


class ChallengeOpenHookHappyPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_snaps_to_start_lands_paused_and_installs_fence(self) -> None:
        pc = _build_pc_stub(is_recording=True)
        pc.replay_current_play(8_000_000_000, 12_000_000_000)
        self.assertEqual(pc._playback_session_time_ns, 8_000_000_000)
        self.assertEqual(pc._state.current_playback_mode, PlaybackMode.PAUSED)
        self.assertEqual(pc._playback_rate, 0.0)
        self.assertEqual(pc._clip_bounds, (8_000_000_000, 12_000_000_000))
        # Render the freeze frame at the snap target.
        pc._render_at_session_time_ns.assert_called_once_with(8_000_000_000)
        # Replay clock is NOT started — challenge starts paused.
        pc._stop_replay_clock_locked.assert_called_once()

    def test_clamps_to_earliest_replayable_when_start_is_before_coverage(
        self,
    ) -> None:
        # Defensive: bounds start at 7s but earliest replayable is 9s.
        # Seek should clamp to 9s; the §8.6.1 freeze-frame rule renders
        # the first frame as a freeze. The fence itself stays at the
        # caller-provided bounds so subsequent seeks see the true play
        # window.
        pc = _build_pc_stub(is_recording=True, earliest_replayable_ns=9_000_000_000)
        pc.replay_current_play(7_000_000_000, 12_000_000_000)
        self.assertEqual(pc._playback_session_time_ns, 9_000_000_000)
        self.assertEqual(pc._clip_bounds, (7_000_000_000, 12_000_000_000))
        pc._render_at_session_time_ns.assert_called_once_with(9_000_000_000)

    def test_emits_challenge_status_message(self) -> None:
        pc = _build_pc_stub(is_recording=True)
        pc.replay_current_play(5_000_000_000, 9_000_000_000)
        pc._emit_state.assert_called_once_with("Reviewing play (challenge)")

    def test_end_none_accepted_for_brief_close_window(self) -> None:
        # The play may not have a finalized end yet at the moment of
        # the Challenge press — the spec allows `end=None` and the
        # fence clamp falls back to the segment store's `latest`.
        pc = _build_pc_stub(is_recording=True)
        pc.replay_current_play(8_000_000_000, None)
        self.assertEqual(pc._clip_bounds, (8_000_000_000, None))


class ChallengeOpenHookNoOpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_no_op_when_live_only(self) -> None:
        pc = _build_pc_stub(is_recording=True, live_only=True)
        pc.replay_current_play(0, 4_000_000_000)
        pc._render_at_session_time_ns.assert_not_called()
        # No fence installed on the live-only controller.
        self.assertIsNone(pc._clip_bounds)
        pc._emit_state.assert_called_once_with("This output is locked to live.")

    def test_no_op_when_not_recording(self) -> None:
        pc = _build_pc_stub(is_recording=False)
        pc.replay_current_play(0, 4_000_000_000)
        pc._render_at_session_time_ns.assert_not_called()
        self.assertIsNone(pc._clip_bounds)
        pc._emit_state.assert_called_once_with(
            "Replay unavailable: start game recording first."
        )


if __name__ == "__main__":
    unittest.main()
