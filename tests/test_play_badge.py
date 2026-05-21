"""Phase 7.H.3: `Play #N` badge in the playback overlay + status bar."""

from __future__ import annotations

import unittest
from unittest import mock

from app.core.app_state import UiState
from app.core.models import PlaybackMode, PlaybackOverlayInfo
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.media.frame_overlay import build_playback_overlay_lines
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


class OverlayLinesPlayBadgeTests(unittest.TestCase):
    """The `Play #N` line appears just under the mode badge — between
    `LIVE`/`REPLAY`/`PAUSED` and the timestamps — when present."""

    def test_play_line_present_when_play_number_set(self) -> None:
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.LIVE,
            wall_clock_timestamp=1234567890.0,
            is_recording=True,
            current_play_number=2,
        )
        lines = build_playback_overlay_lines(overlay)
        self.assertEqual(lines[0], "LIVE")
        self.assertEqual(lines[1], "Play #2")

    def test_play_line_absent_when_play_number_none(self) -> None:
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.LIVE,
            wall_clock_timestamp=1234567890.0,
            is_recording=True,
            current_play_number=None,
        )
        lines = build_playback_overlay_lines(overlay)
        # Mode is first; no Play line should appear.
        self.assertEqual(lines[0], "LIVE")
        self.assertFalse(any(line.startswith("Play #") for line in lines))

    def test_play_line_skipped_when_overlay_hidden(self) -> None:
        # Outside recording the whole overlay is hidden; play badge
        # should not appear in any form.
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.LIVE,
            wall_clock_timestamp=1234567890.0,
            is_recording=False,
            current_play_number=2,
        )
        self.assertEqual(build_playback_overlay_lines(overlay), [])

    def test_play_line_appears_in_replay_too(self) -> None:
        # Mode = REPLAY shouldn't suppress the badge.
        overlay = PlaybackOverlayInfo(
            mode=PlaybackMode.REPLAY,
            wall_clock_timestamp=1234567890.0,
            is_recording=True,
            current_play_number=4,
            seconds_behind_live=5.0,
        )
        lines = build_playback_overlay_lines(overlay)
        self.assertIn("Play #4", lines)


class StatusBarPlayRowTests(unittest.TestCase):
    def test_dash_when_no_play_number(self) -> None:
        s = UiState(current_play_number=None)
        self.assertEqual(_format_play_badge(s), "—")

    def test_play_number_rendered(self) -> None:
        s = UiState(current_play_number=7)
        self.assertEqual(_format_play_badge(s), "Play #7")


if __name__ == "__main__":
    unittest.main()
