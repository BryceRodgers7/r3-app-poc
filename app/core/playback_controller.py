"""High-level playback orchestration for the placeholder application."""

from __future__ import annotations

from app.core.app_state import AppState
from app.core.models import PlaybackMode
from app.core.signals import AppSignals
from app.media.pipeline_manager import PipelineManager
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer
from app.storage.session_manager import SessionManager


class PlaybackController:
    """Orchestrates state transitions without owning heavy media logic."""

    def __init__(
        self,
        session_manager: SessionManager,
        pipeline_manager: PipelineManager,
        recorder: Recorder,
        replay_buffer: ReplayBuffer,
        default_source_name: str,
    ) -> None:
        self._session_manager = session_manager
        self._pipeline_manager = pipeline_manager
        self._recorder = recorder
        self._replay_buffer = replay_buffer
        self._default_source_name = default_source_name
        self.signals = AppSignals()
        self._state = AppState(current_source_name=default_source_name)

    def initialize(self) -> None:
        """Create a session and start placeholder background services."""
        session_paths = self._session_manager.start_new_session(self._default_source_name)
        self._recorder.start(session_paths)
        self._replay_buffer.start(session_paths)
        self._pipeline_manager.start_preview()
        self._pipeline_manager.start_recording()
        self._pipeline_manager.start_replay_buffer()

        self._state.current_session_id = session_paths.session_id
        self._state.current_source_name = self._default_source_name
        self._state.is_recording = self._recorder.is_recording()
        self.set_source_connected()

    def pause_playback(self) -> None:
        """Pause only the viewed playback state."""
        if not self._state.source_connected:
            self._state.error_message = "Cannot pause while the source is unavailable."
            self._emit_state()
            return

        self._state.current_playback_mode = PlaybackMode.PAUSED
        self._state.error_message = None
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Move the viewed output back by ten seconds without stopping ingest."""
        if not self._replay_buffer.is_running():
            self._state.error_message = "Replay buffer is not active."
            self._emit_state()
            return

        current_offset = self._replay_buffer.get_seconds_behind_live()
        self._state.seconds_behind_live = self._replay_buffer.seek_seconds_behind_live(
            current_offset + 10.0
        )
        self._state.current_playback_mode = PlaybackMode.REPLAY
        self._state.error_message = None
        self._emit_state("Rewound 10 seconds")

    def jump_to_live(self) -> None:
        """Return the viewed output to the live edge."""
        self._replay_buffer.jump_to_live()
        self._state.seconds_behind_live = 0.0
        self._state.current_playback_mode = (
            PlaybackMode.LIVE if self._state.source_connected else PlaybackMode.SOURCE_LOST
        )
        self._state.error_message = None
        self._emit_state("Returned to live")

    def set_source_lost(self, message: str = "Source signal lost.") -> None:
        """Reflect that the live source is no longer available."""
        self._state.source_connected = False
        self._state.current_playback_mode = PlaybackMode.SOURCE_LOST
        self._state.error_message = message
        self._emit_state(message)

    def set_source_connected(self) -> None:
        """Reflect that the live source is available again."""
        connected = self._pipeline_manager.connect_source()
        self._state.source_connected = connected
        self._state.current_playback_mode = PlaybackMode.LIVE if connected else PlaybackMode.SOURCE_LOST
        self._state.error_message = None if connected else "Unable to connect to source."
        self._emit_state("Source connected" if connected else "Source unavailable")

    def get_state(self) -> AppState:
        """Return the current application state."""
        return self._state

    def shutdown(self) -> None:
        """Stop placeholder services and release storage resources."""
        self._pipeline_manager.stop_all()
        self._recorder.stop()
        self._replay_buffer.stop()
        self._session_manager.close()

    def _emit_state(self, status_message: str | None = None) -> None:
        self.signals.state_changed.emit(self._state)
        if status_message:
            self.signals.status_message.emit(status_message)
