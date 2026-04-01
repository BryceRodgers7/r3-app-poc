"""High-level playback orchestration for the placeholder application."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from app.core.app_state import AppState
from app.core.models import MediaFrame, PlaybackMode
from app.core.signals import AppSignals
from app.media.pipeline_manager import PipelineManager
from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer
from app.storage.session_manager import SessionManager

if TYPE_CHECKING:
    from app.ui.video_widget import VideoWidget


class PlaybackController:
    """Orchestrates state transitions without owning heavy media logic."""

    def __init__(
        self,
        session_manager: SessionManager,
        pipeline_manager: PipelineManager,
        preview_output: PreviewOutput,
        recorder: Recorder,
        replay_buffer: ReplayBuffer,
        default_source_name: str,
    ) -> None:
        del preview_output
        self._session_manager = session_manager
        self._pipeline_manager = pipeline_manager
        self._recorder = recorder
        self._replay_buffer = replay_buffer
        self._default_source_name = default_source_name
        self.signals = AppSignals()
        self._state = AppState(current_source_name=default_source_name)
        self._latest_live_frame: MediaFrame | None = None
        self._display_frame: MediaFrame | None = None
        self._latest_live_timestamp: float | None = None
        self._playback_timestamp: float | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """Create a session and start placeholder background services."""
        connected = self._pipeline_manager.connect_source()
        source_name = self._pipeline_manager.get_source_name()
        session_paths = self._session_manager.start_new_session(source_name)
        self._pipeline_manager.set_live_sample_callback(self.on_live_sample)
        self._pipeline_manager.start_replay_buffer(session_paths)
        self._pipeline_manager.start_recording(session_paths)
        self._pipeline_manager.start_preview()

        self._state.current_session_id = session_paths.session_id
        self._state.current_source_name = source_name
        self._state.is_recording = self._recorder.is_recording()
        self._state.replay_buffer_span_seconds = self._replay_buffer.get_available_duration()

        if connected:
            self.set_source_connected()
        else:
            self.set_source_lost("Unable to connect to source.")

    def pause_playback(self) -> None:
        """Pause only the viewed playback state."""
        with self._lock:
            if self._display_frame is not None:
                base_timestamp = self._display_frame.timestamp
            else:
                base_timestamp = self._latest_live_timestamp

            if base_timestamp is None:
                self._state.error_message = "Cannot pause while the source is unavailable."
                self._emit_state()
                return

            pause_frame = self._replay_buffer.get_frame_at_or_before(base_timestamp) or self._display_frame
            if pause_frame is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            self._playback_timestamp = pause_frame.timestamp
            self._display_frame = pause_frame
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Move the viewed output back by ten seconds without stopping ingest."""
        with self._lock:
            if not self._replay_buffer.is_running():
                self._state.error_message = "Replay buffer is not active."
                self._emit_state()
                return

            oldest_timestamp, _ = self._replay_buffer.get_buffer_range()
            if oldest_timestamp is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            if self._state.current_playback_mode == PlaybackMode.LIVE:
                base_timestamp = self._latest_live_timestamp
            else:
                if self._playback_timestamp is not None:
                    base_timestamp = self._playback_timestamp
                elif self._display_frame is not None:
                    base_timestamp = self._display_frame.timestamp
                else:
                    base_timestamp = None

            if base_timestamp is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            target_timestamp = max(oldest_timestamp, base_timestamp - 10.0)
            replay_frame = self._replay_buffer.get_frame_at_or_before(target_timestamp)
            if replay_frame is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            self._playback_timestamp = replay_frame.timestamp
            self._display_frame = replay_frame
            self._state.current_playback_mode = PlaybackMode.REPLAY
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._emit_state(f"Replay -{self._state.seconds_behind_live:.0f}s")

    def jump_to_live(self) -> None:
        """Return the viewed output to the live edge."""
        with self._lock:
            self._playback_timestamp = self._latest_live_timestamp
            self._state.current_playback_mode = (
                PlaybackMode.LIVE if self._state.source_connected else PlaybackMode.SOURCE_LOST
            )
            self._state.error_message = None
            self._display_frame = None
            self._update_state_timestamps_locked()
        self._emit_state("Returned to live")

    def attach_preview_widget(self, video_widget: VideoWidget) -> None:
        """Bind the live preview sink to the widget's native child surface."""
        self._pipeline_manager.set_preview_window_handle(video_widget.get_live_video_handle())
        video_widget.live_surface_resized.connect(self._pipeline_manager.refresh_preview_overlay)

    def set_source_lost(self, message: str = "Source signal lost.") -> None:
        """Reflect that the live source is no longer available."""
        with self._lock:
            self._state.source_connected = False
            self._state.current_playback_mode = PlaybackMode.SOURCE_LOST
            self._state.error_message = message
        self._emit_state(message)

    def set_source_connected(self) -> None:
        """Reflect that the live source is available again."""
        with self._lock:
            self._state.source_connected = True
            if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST:
                self._state.current_playback_mode = PlaybackMode.LIVE
            self._state.current_source_name = self._pipeline_manager.get_source_name()
            self._state.error_message = None
        self._emit_state("Source connected")

    def get_state(self) -> AppState:
        """Return the current application state."""
        with self._lock:
            return self._state

    def get_display_frame(self) -> MediaFrame | None:
        """Return the frame the UI should currently display."""
        with self._lock:
            return self._display_frame

    def shutdown(self) -> None:
        """Stop placeholder services and release storage resources."""
        self._pipeline_manager.stop_all()
        self._recorder.stop()
        self._replay_buffer.stop()
        self._session_manager.close()

    def on_new_live_frame(self, frame: MediaFrame) -> None:
        """Update controller-owned playback state for a newly ingested live frame."""
        with self._lock:
            self._latest_live_frame = frame
            self._latest_live_timestamp = frame.timestamp
            self._state.source_connected = True
            self._state.current_source_name = frame.source_name
            self._state.is_recording = self._recorder.is_recording()

            if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST:
                self._state.current_playback_mode = PlaybackMode.LIVE

            if self._state.current_playback_mode == PlaybackMode.LIVE:
                self._playback_timestamp = frame.timestamp
                self._display_frame = frame
            elif self._state.current_playback_mode == PlaybackMode.REPLAY:
                if self._playback_timestamp is not None and self._display_frame is None:
                    self._display_frame = self._replay_buffer.get_frame_at_or_before(
                        self._playback_timestamp
                    )

            self._update_state_timestamps_locked()
        self._emit_state()

    def on_live_sample(self, timestamp: float, source_name: str) -> None:
        """Update controller-owned playback state from the live preview branch."""
        with self._lock:
            self._latest_live_timestamp = timestamp
            self._state.source_connected = True
            self._state.current_source_name = source_name
            self._state.is_recording = self._recorder.is_recording()

            if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST:
                self._state.current_playback_mode = PlaybackMode.LIVE

            if self._state.current_playback_mode == PlaybackMode.LIVE:
                self._playback_timestamp = timestamp
                self._display_frame = None
            elif self._state.current_playback_mode == PlaybackMode.REPLAY:
                if self._playback_timestamp is not None and self._display_frame is None:
                    self._display_frame = self._replay_buffer.get_frame_at_or_before(
                        self._playback_timestamp
                    )

            self._update_state_timestamps_locked()
        self._emit_state()

    def _emit_state(self, status_message: str | None = None) -> None:
        self.signals.state_changed.emit(self._state)
        if status_message:
            self.signals.status_message.emit(status_message)

    def _update_state_timestamps_locked(self) -> None:
        self._state.last_frame_timestamp = self._latest_live_timestamp
        self._state.replay_buffer_span_seconds = self._replay_buffer.get_available_duration()

        if (
            self._state.current_playback_mode == PlaybackMode.LIVE
            or self._playback_timestamp is None
            or self._latest_live_timestamp is None
        ):
            self._state.seconds_behind_live = 0.0
            return

        self._state.seconds_behind_live = self._replay_buffer.get_seconds_behind_live(
            self._playback_timestamp
        )
