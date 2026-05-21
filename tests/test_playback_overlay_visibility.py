"""Overlay visibility is gated on `is_recording`.

The operator transport overlay (LIVE / REPLAY / PAUSED badge plus the
behind-live counter and rate) only appears while a game is being
recorded. Outside of recording, replay is unavailable per §10.4 / §15.2
and a static LIVE badge has no actionable meaning, so we hide entirely.
"""

from __future__ import annotations

import unittest

from app.core.app_state import UiState
from app.core.models import PlaybackMode, PlaybackOverlayInfo
from app.core.playback_controller import PlaybackController
from app.media.frame_overlay import build_playback_overlay_lines


class BuildLinesGatingTests(unittest.TestCase):
    def test_returns_empty_when_not_recording(self) -> None:
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.LIVE,
            wall_clock_timestamp=1234567890.0,
            is_recording=False,
        )
        self.assertEqual(build_playback_overlay_lines(overlay), [])

    def test_returns_empty_when_not_recording_even_in_replay(self) -> None:
        # Defensive: REPLAY mode without `is_recording=True` shouldn't
        # be reachable in practice (the controller flips back to LIVE
        # when recording stops), but lock it down so the gate is
        # purely on `is_recording`.
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.REPLAY,
            wall_clock_timestamp=1234567890.0,
            seconds_behind_live=4.0,
            is_recording=False,
        )
        self.assertEqual(build_playback_overlay_lines(overlay), [])

    def test_includes_mode_when_recording_in_live(self) -> None:
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.LIVE,
            wall_clock_timestamp=1234567890.0,
            is_recording=True,
        )
        lines = build_playback_overlay_lines(overlay)
        self.assertIn("LIVE", lines)

    def test_includes_behind_live_in_replay_when_recording(self) -> None:
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.REPLAY,
            wall_clock_timestamp=1234567890.0,
            seconds_behind_live=4.0,
            playback_rate=0.5,
            is_recording=True,
        )
        lines = build_playback_overlay_lines(overlay)
        self.assertIn("REPLAY", lines)
        self.assertTrue(any("Behind live: 4.0s" in line for line in lines))
        self.assertTrue(any("Rate: 0.50x" in line for line in lines))


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
