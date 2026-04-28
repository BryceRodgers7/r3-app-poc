"""Coordinator for the global recording state machine."""

from __future__ import annotations

from app.core.recording_state import RecordingState, make_recording_state_machine
from app.core.state_machine import StateMachine


class RecordingManager:
    """Owns the global `RecordingState` machine.

    Slice 4.D removed per-feed `Recorder` instances — segmented recording
    is now handled inside each feed's `PipelineManager` via `splitmuxsink`.
    What remains is the cross-feed flag that the operator's toggle button,
    diagnostics widget, and replay-eligibility checks all share.
    """

    def __init__(
        self,
        *,
        recording_state: StateMachine[RecordingState] | None = None,
    ) -> None:
        self.recording_state = (
            recording_state if recording_state is not None else make_recording_state_machine()
        )

    def is_recording(self, feed_id: str | None = None) -> bool:
        """Return whether long-form recording is currently active.

        `feed_id` is accepted for symmetry with earlier per-feed APIs but
        ignored — recording starts and stops on every feed at once.
        """
        return self.recording_state.state == RecordingState.RECORDING

    def is_any_recording(self) -> bool:
        """Return whether long-form recording is currently active."""
        return self.recording_state.state == RecordingState.RECORDING
