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
        controller._clip_bounds = None
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
        controller._clip_bounds = None
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


class ControllerPropagatesClipMarkTests(unittest.TestCase):
    """The open clip's `marked` flag reaches UiState, so the Mark Play
    button can show the press took effect."""

    def _controller(self, clip_manager) -> PlaybackController:
        from app.core.recording_state import RecordingState

        controller = PlaybackController.__new__(PlaybackController)
        controller._state = UiState()
        controller._state.current_playback_mode = PlaybackMode.LIVE
        controller._latest_live_timestamp = None
        controller._latest_live_overlay = type("F", (), {"feed_id": "f"})()
        controller._playback_session_time_ns = None
        controller._playback_rate = 1.0
        controller._session_clock = None
        controller._clip_manager = clip_manager
        controller._clip_bounds = None
        controller._replay_store = type(
            "S", (), {
                "available_session_time_range": lambda self: (None, None),
            }
        )()
        controller._recording_manager = type(
            "RM", (), {
                "recording_state": type(
                    "S", (), {"state": RecordingState.NOT_RECORDING}
                )()
            }
        )()
        return controller

    def _clip_manager(self, clip):
        return type(
            "CM", (), {
                "current_play_number": lambda self: None,
                "current_clip": lambda self: clip,
            }
        )()

    def test_marked_clip(self) -> None:
        clip = type(
            "C", (), {"clip_number": 3, "type": "play", "marked": True}
        )()
        controller = self._controller(self._clip_manager(clip))
        controller._update_state_timestamps_locked()
        self.assertTrue(controller._state.current_clip_marked)

    def test_unmarked_clip(self) -> None:
        clip = type(
            "C", (), {"clip_number": 3, "type": "play", "marked": False}
        )()
        controller = self._controller(self._clip_manager(clip))
        controller._update_state_timestamps_locked()
        self.assertFalse(controller._state.current_clip_marked)

    def test_no_open_clip_clears_the_flag(self) -> None:
        controller = self._controller(self._clip_manager(None))
        controller._state.current_clip_marked = True
        controller._update_state_timestamps_locked()
        self.assertFalse(controller._state.current_clip_marked)

    def test_no_clip_manager_clears_the_flag(self) -> None:
        controller = self._controller(None)
        controller._state.current_clip_marked = True
        controller._update_state_timestamps_locked()
        self.assertFalse(controller._state.current_clip_marked)

    def test_no_fence_means_no_clip_under_review(self) -> None:
        controller = self._controller(None)
        controller._update_state_timestamps_locked()
        self.assertIsNone(controller._state.review_range_ns)
        self.assertIsNone(controller._state.playback_position_ns)

    def test_fence_and_replay_clock_reach_state(self) -> None:
        # What the seek bar draws from.
        controller = self._controller(None)
        controller._clip_bounds = (10_000_000_000, 20_000_000_000)
        controller._playback_session_time_ns = 12_000_000_000
        controller._update_state_timestamps_locked()
        self.assertEqual(
            controller._state.review_range_ns, (10_000_000_000, 20_000_000_000)
        )
        self.assertEqual(controller._state.playback_position_ns, 12_000_000_000)

    def test_open_ended_fence_runs_to_the_replayable_edge(self) -> None:
        controller = self._controller(None)
        controller._clip_bounds = (10_000_000_000, None)
        controller._replay_store = type(
            "S", (), {
                "available_session_time_range": lambda self: (0, 18_000_000_000),
            }
        )()
        controller._update_state_timestamps_locked()
        self.assertEqual(
            controller._state.review_range_ns, (10_000_000_000, 18_000_000_000)
        )

    def test_lifting_the_fence_clears_the_range(self) -> None:
        controller = self._controller(None)
        controller._clip_bounds = (10_000_000_000, 20_000_000_000)
        controller._update_state_timestamps_locked()
        controller._clip_bounds = None
        controller._update_state_timestamps_locked()
        self.assertIsNone(controller._state.review_range_ns)


if __name__ == "__main__":
    unittest.main()
