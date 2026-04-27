"""Unit tests for `app.core.recording_state`."""

from __future__ import annotations

import unittest

from app.core import health_events
from app.core.health_events import HealthEventLog
from app.core.recording_state import RecordingState, make_recording_state_machine


class RecordingStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_log = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = HealthEventLog()
        self.log = health_events._DEFAULT_LOG

    def tearDown(self) -> None:
        health_events._DEFAULT_LOG = self._orig_log

    def test_initial_state_is_not_recording(self) -> None:
        sm = make_recording_state_machine()
        self.assertIs(sm.state, RecordingState.NOT_RECORDING)

    def test_normal_start_stop_path(self) -> None:
        sm = make_recording_state_machine()
        self.assertTrue(sm.transition_to(RecordingState.STARTING_RECORDING))
        self.assertTrue(sm.transition_to(RecordingState.RECORDING))
        self.assertTrue(sm.transition_to(RecordingState.STOPPING_RECORDING))
        self.assertTrue(sm.transition_to(RecordingState.FINALIZING))
        self.assertTrue(sm.transition_to(RecordingState.NOT_RECORDING))

    def test_cannot_skip_starting_step(self) -> None:
        sm = make_recording_state_machine()
        self.assertFalse(sm.transition_to(RecordingState.RECORDING))

    def test_cannot_re_enter_recording_directly_from_recording_error(self) -> None:
        sm = make_recording_state_machine()
        sm.transition_to(RecordingState.STARTING_RECORDING)
        sm.transition_to(RecordingState.RECORDING)
        sm.transition_to(RecordingState.RECORDING_ERROR)
        # Must go through STARTING_RECORDING again, not jump to RECORDING.
        self.assertFalse(sm.transition_to(RecordingState.RECORDING))
        self.assertTrue(sm.transition_to(RecordingState.STARTING_RECORDING))
        self.assertTrue(sm.transition_to(RecordingState.RECORDING))

    def test_recording_started_health_event_emitted(self) -> None:
        sm = make_recording_state_machine()
        sm.transition_to(RecordingState.STARTING_RECORDING)
        sm.transition_to(RecordingState.RECORDING)
        self.assertTrue(
            self.log.has_open_event(category="recording_started", feed_id=None)
        )

    def test_recording_error_health_event_emitted(self) -> None:
        sm = make_recording_state_machine()
        sm.transition_to(RecordingState.STARTING_RECORDING)
        sm.transition_to(RecordingState.RECORDING)
        sm.transition_to(RecordingState.RECORDING_ERROR)
        self.assertTrue(
            self.log.has_open_event(category="recording_error", feed_id=None)
        )

    def test_recovery_clears_error_marker(self) -> None:
        sm = make_recording_state_machine()
        sm.transition_to(RecordingState.STARTING_RECORDING)
        sm.transition_to(RecordingState.RECORDING_ERROR)
        sm.transition_to(RecordingState.STARTING_RECORDING)
        sm.transition_to(RecordingState.RECORDING)
        self.assertFalse(
            self.log.has_open_event(category="recording_error", feed_id=None)
        )


if __name__ == "__main__":
    unittest.main()
