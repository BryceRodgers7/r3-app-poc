"""Coordinator for per-feed recording services."""

from __future__ import annotations

from app.core.recording_state import RecordingState, make_recording_state_machine
from app.core.state_machine import StateMachine
from app.media.recorder import Recorder


class RecordingManager:
    """Track recorder instances by feed identifier and the global recording state."""

    def __init__(
        self,
        *,
        recording_state: StateMachine[RecordingState] | None = None,
    ) -> None:
        self._recorders: dict[str, Recorder] = {}
        self.recording_state = (
            recording_state if recording_state is not None else make_recording_state_machine()
        )

    def register(self, feed_id: str, recorder: Recorder) -> None:
        """Register a recorder for a feed."""
        self._recorders[feed_id] = recorder

    def get(self, feed_id: str) -> Recorder:
        """Return the recorder for a feed."""
        return self._recorders[feed_id]

    def is_recording(self, feed_id: str) -> bool:
        """Return whether long-form recording is currently active.

        Per-feed recording isn't tracked separately in Phase 4.A — the
        operator's toggle drives all feeds together via the global
        `RecordingState` machine. Returns the same value as
        `is_any_recording`. Slice 4.A bypassed the legacy
        `Recorder.is_recording()` flag, so reading from there would
        always say 'idle' even mid-recording.
        """
        return self.recording_state.state == RecordingState.RECORDING

    def is_any_recording(self) -> bool:
        """Return whether long-form recording is currently active."""
        return self.recording_state.state == RecordingState.RECORDING

    def stop_all(self) -> None:
        """Stop all registered recorders."""
        for recorder in self._recorders.values():
            recorder.stop()
