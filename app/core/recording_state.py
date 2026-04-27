"""Long-recording lifecycle state (§10.3).

`RecordingState` replaces the scattered `is_recording: bool` checks scattered
across the controllers and UI. The state machine is owned by
`RecordingManager`; producers are `ApplicationCoordinator.toggle_long_session_recording`
(driving the start/stop transitions) and `Recorder` itself (signalling
finalize completion or hard errors).
"""

from __future__ import annotations

from enum import Enum

from app.core.health_events import HealthSeverity, default_log, record_health_event
from app.core.state_machine import StateMachine


class RecordingState(str, Enum):
    NOT_RECORDING = "not_recording"
    STARTING_RECORDING = "starting_recording"
    RECORDING = "recording"
    STOPPING_RECORDING = "stopping_recording"
    FINALIZING = "finalizing"
    RECORDING_ERROR = "recording_error"


_RECORDING_TRANSITIONS: dict[RecordingState, set[RecordingState]] = {
    RecordingState.NOT_RECORDING: {
        RecordingState.STARTING_RECORDING,
    },
    RecordingState.STARTING_RECORDING: {
        RecordingState.RECORDING,
        RecordingState.RECORDING_ERROR,
        RecordingState.NOT_RECORDING,
    },
    RecordingState.RECORDING: {
        RecordingState.STOPPING_RECORDING,
        RecordingState.RECORDING_ERROR,
    },
    RecordingState.STOPPING_RECORDING: {
        RecordingState.FINALIZING,
        RecordingState.RECORDING_ERROR,
    },
    RecordingState.FINALIZING: {
        RecordingState.NOT_RECORDING,
        RecordingState.RECORDING_ERROR,
    },
    RecordingState.RECORDING_ERROR: {
        RecordingState.NOT_RECORDING,
        RecordingState.STARTING_RECORDING,
    },
}


def make_recording_state_machine() -> StateMachine[RecordingState]:
    """Build a `StateMachine[RecordingState]` with health-event hooks."""

    def on_enter(old: RecordingState, new: RecordingState) -> None:
        log = default_log()
        if new == RecordingState.RECORDING_ERROR:
            if not log.has_open_event(category="recording_error", feed_id=None):
                record_health_event(
                    severity=HealthSeverity.ERROR,
                    category="recording_error",
                    message=f"Recording entered error state from {old.name}.",
                    metadata={"from": old.name, "to": new.name},
                )
            return
        if new == RecordingState.RECORDING:
            record_health_event(
                severity=HealthSeverity.INFO,
                category="recording_started",
                message=f"Recording started ({old.name} -> RECORDING).",
            )
            log.clear_open_event(category="recording_error", feed_id=None)
            return
        if new == RecordingState.NOT_RECORDING and old == RecordingState.FINALIZING:
            record_health_event(
                severity=HealthSeverity.INFO,
                category="recording_stopped",
                message="Recording finalized cleanly.",
            )
            return

    return StateMachine[RecordingState](
        initial=RecordingState.NOT_RECORDING,
        transitions=_RECORDING_TRANSITIONS,
        name="recording_state",
        on_enter=on_enter,
    )
