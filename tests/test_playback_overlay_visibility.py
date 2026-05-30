"""The controller threads `is_recording` onto its PlaybackOverlayInfo.

The recording flag drives downstream UI gating (e.g. the referee
pause/play label). This locks down that `_update_state_timestamps_locked`
copies the recording state onto the overlay info it builds.
"""

from __future__ import annotations

import unittest

from app.core.app_state import UiState
from app.core.models import PlaybackMode
from app.core.playback_controller import PlaybackController


class ControllerPropagatesIsRecordingTests(unittest.TestCase):
    """`_update_state_timestamps_locked` must thread `is_recording`
    onto the PlaybackOverlayInfo it builds, so the live/replay overlay
    appears the moment recording starts."""

    def test_propagates_is_recording_true(self) -> None:
        controller = PlaybackController.__new__(PlaybackController)
        controller._state = UiState()
        controller._state.is_recording = True
        controller._state.current_playback_mode = PlaybackMode.LIVE
        controller._latest_live_timestamp = None
        controller._latest_live_overlay = type("F", (), {"feed_id": "f"})()
        controller._playback_session_time_ns = None
        controller._playback_rate = 1.0
        controller._session_clock = None
        controller._clip_manager = None
        controller._replay_store = type(
            "S", (), {
                "available_session_time_range": lambda self: (None, None),
            }
        )()
        controller._recording_manager = type(
            "RM", (), {
                "recording_state": type(
                    "S", (), {"state": type("E", (), {"value": ""})()}
                )()
            }
        )()
        # Bypass the controller's recording-state gate; we're testing
        # that whatever is_recording is on UiState ends up on the
        # PlaybackOverlayInfo.
        from app.core.recording_state import RecordingState
        controller._recording_manager.recording_state.state = RecordingState.RECORDING

        controller._update_state_timestamps_locked()
        self.assertTrue(controller._state.playback_overlay.is_recording)

    def test_propagates_is_recording_false(self) -> None:
        controller = PlaybackController.__new__(PlaybackController)
        controller._state = UiState()
        controller._state.is_recording = False
        controller._state.current_playback_mode = PlaybackMode.LIVE
        controller._latest_live_timestamp = None
        controller._latest_live_overlay = type("F", (), {"feed_id": "f"})()
        controller._playback_session_time_ns = None
        controller._playback_rate = 1.0
        controller._session_clock = None
        controller._clip_manager = None
        controller._replay_store = type(
            "S", (), {
                "available_session_time_range": lambda self: (None, None),
            }
        )()
        from app.core.recording_state import RecordingState
        controller._recording_manager = type(
            "RM", (), {
                "recording_state": type(
                    "S", (), {"state": RecordingState.NOT_RECORDING}
                )()
            }
        )()

        controller._update_state_timestamps_locked()
        self.assertFalse(controller._state.playback_overlay.is_recording)


if __name__ == "__main__":
    unittest.main()
