"""Unit tests for `app.core.application_state.compute_app_state`."""

from __future__ import annotations

import unittest

from app.core.application_state import AppState, compute_app_state
from app.core.feed_state import FeedState
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState


class ComputeAppStateTests(unittest.TestCase):
    def test_no_feeds_returns_starting(self) -> None:
        result = compute_app_state(
            feed_states=[],
            recording_state=RecordingState.NOT_RECORDING,
            replay_state=None,
        )
        self.assertIs(result, AppState.STARTING)

    def test_connecting_feeds_yield_starting(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.CONNECTING, FeedState.CONNECTING],
            recording_state=RecordingState.NOT_RECORDING,
            replay_state=ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        )
        self.assertIs(result, AppState.STARTING)

    def test_disabled_feeds_yield_idle(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.DISABLED, FeedState.DISABLED],
            recording_state=RecordingState.NOT_RECORDING,
            replay_state=ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        )
        self.assertIs(result, AppState.IDLE)

    def test_live_feed_no_recording_is_previewing(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE],
            recording_state=RecordingState.NOT_RECORDING,
            replay_state=ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        )
        self.assertIs(result, AppState.PREVIEWING)

    def test_recording_state_yields_recording(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE],
            recording_state=RecordingState.RECORDING,
            replay_state=ReplayState.LIVE_WHILE_RECORDING,
        )
        self.assertIs(result, AppState.RECORDING)

    def test_replay_states_take_precedence_over_recording(self) -> None:
        for rs, expected in [
            (ReplayState.REPLAYING, AppState.REPLAYING),
            (ReplayState.PAUSED, AppState.PAUSED),
            (ReplayState.SLOW_MOTION, AppState.SLOW_MOTION),
        ]:
            with self.subTest(replay=rs):
                result = compute_app_state(
                    feed_states=[FeedState.LIVE],
                    recording_state=RecordingState.RECORDING,
                    replay_state=rs,
                )
                self.assertIs(result, expected)

    def test_recording_error_takes_precedence_over_replay(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE],
            recording_state=RecordingState.RECORDING_ERROR,
            replay_state=ReplayState.REPLAYING,
        )
        self.assertIs(result, AppState.ERROR)

    def test_degraded_feed_overrides_recording_when_no_active_replay(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE, FeedState.DEGRADED],
            recording_state=RecordingState.RECORDING,
            replay_state=ReplayState.LIVE_WHILE_RECORDING,
        )
        self.assertIs(result, AppState.DEGRADED)

    def test_active_replay_overrides_degraded_so_user_action_is_visible(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE, FeedState.DEGRADED],
            recording_state=RecordingState.RECORDING,
            replay_state=ReplayState.REPLAYING,
        )
        self.assertIs(result, AppState.REPLAYING)

    def test_replay_degraded_yields_degraded_when_no_user_action(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE],
            recording_state=RecordingState.RECORDING,
            replay_state=ReplayState.REPLAY_DEGRADED,
        )
        self.assertIs(result, AppState.DEGRADED)

    def test_shutting_down_overrides_everything(self) -> None:
        result = compute_app_state(
            feed_states=[FeedState.LIVE],
            recording_state=RecordingState.RECORDING_ERROR,
            replay_state=ReplayState.REPLAYING,
            shutting_down=True,
        )
        self.assertIs(result, AppState.SHUTTING_DOWN)


if __name__ == "__main__":
    unittest.main()
