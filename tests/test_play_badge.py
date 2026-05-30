"""Phase 7.H.3: `Play #N` propagation into UiState + status bar.

The on-video playback overlay that once rendered this badge was
removed (no chrome overlays the video feed); the play number still
flows onto `UiState`/`PlaybackOverlayInfo` and into the diagnostic
status bar, which is what these tests cover.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.core.app_state import UiState
from app.core.models import PlaybackMode
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.ui.status_bar_widget import _format_play_badge


def _build_pc_stub(
    *,
    is_recording: bool,
    clip_manager,
) -> PlaybackController:
    """Stub PlaybackController exercising `_update_state_timestamps_locked`."""
    pc = PlaybackController.__new__(PlaybackController)
    pc._state = UiState()
    pc._state.is_recording = is_recording
    pc._state.current_playback_mode = PlaybackMode.LIVE
    pc._latest_live_timestamp = None
    pc._latest_live_overlay = type("F", (), {"feed_id": "f"})()
    pc._playback_session_time_ns = None
    pc._playback_rate = 1.0
    pc._session_clock = None
    pc._clip_manager = clip_manager  # ClipManager (Phase 14.A)
    pc._replay_store = type(
        "S", (), {"available_session_time_range": lambda self: (None, None)}
    )()
    rm = type("RM", (), {})()
    rm.recording_state = type("S", (), {})()
    rm.recording_state.state = (
        RecordingState.RECORDING if is_recording else RecordingState.NOT_RECORDING
    )
    pc._recording_manager = rm
    return pc


class ControllerPropagatesPlayNumberTests(unittest.TestCase):
    def test_play_number_propagates_when_clip_manager_has_open_play(self) -> None:
        cm = mock.Mock()
        cm.current_play_number.return_value = 3
        pc = _build_pc_stub(is_recording=True, clip_manager=cm)
        pc._update_state_timestamps_locked()
        self.assertEqual(pc._state.current_play_number, 3)
        self.assertEqual(pc._state.playback_overlay.current_play_number, 3)

    def test_play_number_none_when_no_open_play(self) -> None:
        # ClipManager exists but reports no open play (between games,
        # or during a pre-game clip before the first Next Play press).
        cm = mock.Mock()
        cm.current_play_number.return_value = None
        pc = _build_pc_stub(is_recording=False, clip_manager=cm)
        pc._update_state_timestamps_locked()
        self.assertIsNone(pc._state.current_play_number)
        self.assertIsNone(pc._state.playback_overlay.current_play_number)

    def test_play_number_none_when_no_clip_manager(self) -> None:
        # Older test paths construct PlaybackController without one.
        pc = _build_pc_stub(is_recording=False, clip_manager=None)
        pc._update_state_timestamps_locked()
        self.assertIsNone(pc._state.current_play_number)
        self.assertIsNone(pc._state.playback_overlay.current_play_number)


class StatusBarPlayRowTests(unittest.TestCase):
    def test_dash_when_no_play_number(self) -> None:
        s = UiState(current_play_number=None)
        self.assertEqual(_format_play_badge(s), "—")

    def test_play_number_rendered(self) -> None:
        s = UiState(current_play_number=7)
        self.assertEqual(_format_play_badge(s), "Play #7")


if __name__ == "__main__":
    unittest.main()
