"""Unit tests for `app.core.replay_state`."""

from __future__ import annotations

import unittest

from app.core import health_events
from app.core.health_events import HealthEventLog
from app.core.replay_state import ACTIVE_REPLAY_STATES, ReplayState, make_replay_state_machine


class ReplayStateTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_log = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = HealthEventLog()
        self.log = health_events._DEFAULT_LOG

    def tearDown(self) -> None:
        health_events._DEFAULT_LOG = self._orig_log

    def test_initial_state_is_replay_unavailable(self) -> None:
        sm = make_replay_state_machine()
        self.assertIs(sm.state, ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)

    def test_recording_starts_unlocks_live_while_recording(self) -> None:
        sm = make_replay_state_machine()
        self.assertTrue(sm.transition_to(ReplayState.LIVE_WHILE_RECORDING))

    def test_seeking_can_complete_to_replaying(self) -> None:
        sm = make_replay_state_machine()
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        sm.transition_to(ReplayState.SEEKING)
        sm.transition_to(ReplayState.REPLAYING)
        self.assertIs(sm.state, ReplayState.REPLAYING)

    def test_replaying_can_drop_to_slow_motion_and_back(self) -> None:
        sm = make_replay_state_machine()
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        sm.transition_to(ReplayState.SEEKING)
        sm.transition_to(ReplayState.REPLAYING)
        sm.transition_to(ReplayState.SLOW_MOTION)
        sm.transition_to(ReplayState.REPLAYING)
        self.assertIs(sm.state, ReplayState.REPLAYING)

    def test_jump_to_live_returns_to_live_while_recording(self) -> None:
        sm = make_replay_state_machine()
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        sm.transition_to(ReplayState.SEEKING)
        sm.transition_to(ReplayState.REPLAYING)
        sm.transition_to(ReplayState.JUMPING_TO_LIVE)
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        self.assertIs(sm.state, ReplayState.LIVE_WHILE_RECORDING)

    def test_recording_stop_path_goes_via_jumping_to_live(self) -> None:
        sm = make_replay_state_machine()
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        sm.transition_to(ReplayState.SEEKING)
        sm.transition_to(ReplayState.REPLAYING)
        sm.transition_to(ReplayState.JUMPING_TO_LIVE)
        sm.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
        self.assertIs(sm.state, ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)

    def test_active_replay_states_set(self) -> None:
        self.assertIn(ReplayState.SEEKING, ACTIVE_REPLAY_STATES)
        self.assertIn(ReplayState.REPLAYING, ACTIVE_REPLAY_STATES)
        self.assertIn(ReplayState.PAUSED, ACTIVE_REPLAY_STATES)
        self.assertIn(ReplayState.SLOW_MOTION, ACTIVE_REPLAY_STATES)
        self.assertNotIn(ReplayState.LIVE_WHILE_RECORDING, ACTIVE_REPLAY_STATES)
        self.assertNotIn(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING, ACTIVE_REPLAY_STATES)

    def test_replay_unavailable_cannot_jump_to_seeking(self) -> None:
        sm = make_replay_state_machine()
        # No recording yet -> SEEKING is not reachable from this state.
        self.assertFalse(sm.transition_to(ReplayState.SEEKING))

    def test_replay_degraded_emits_health_event(self) -> None:
        sm = make_replay_state_machine()
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        sm.transition_to(ReplayState.REPLAY_DEGRADED)
        self.assertTrue(self.log.has_open_event(category="replay_degraded", feed_id=None))

    def test_recovery_clears_replay_degraded_marker(self) -> None:
        sm = make_replay_state_machine()
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        sm.transition_to(ReplayState.REPLAY_DEGRADED)
        sm.transition_to(ReplayState.LIVE_WHILE_RECORDING)
        self.assertFalse(self.log.has_open_event(category="replay_degraded", feed_id=None))


if __name__ == "__main__":
    unittest.main()
