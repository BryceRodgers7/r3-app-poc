"""High-level playback orchestration for the placeholder application."""

from __future__ import annotations

from enum import Enum, auto
import logging
import threading
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer

from app.core.app_state import AppState
from app.core.models import MediaFrame, PlaybackMode
from app.core.signals import AppSignals
from app.media.pipeline_manager import PipelineManager
from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer, ReplayFrameRef, ReplayStore
from app.storage.session_manager import SessionManager

if TYPE_CHECKING:
    from app.ui.video_widget import VideoWidget

LOGGER = logging.getLogger(__name__)


class _PlaybackState(Enum):
    LIVE = auto()
    PAUSED = auto()
    REPLAY = auto()
    SOURCE_LOST = auto()


_ALLOWED_TRANSITIONS: dict[_PlaybackState, set[_PlaybackState]] = {
    _PlaybackState.LIVE: {_PlaybackState.PAUSED, _PlaybackState.REPLAY, _PlaybackState.SOURCE_LOST},
    _PlaybackState.PAUSED: {_PlaybackState.LIVE, _PlaybackState.REPLAY, _PlaybackState.SOURCE_LOST},
    _PlaybackState.REPLAY: {_PlaybackState.LIVE, _PlaybackState.PAUSED, _PlaybackState.SOURCE_LOST},
    _PlaybackState.SOURCE_LOST: {_PlaybackState.LIVE, _PlaybackState.SOURCE_LOST},
}


class PlaybackController:
    """Orchestrates state transitions without owning heavy media logic."""

    def __init__(
        self,
        session_manager: SessionManager,
        pipeline_manager: PipelineManager,
        preview_output: PreviewOutput,
        recorder: Recorder,
        replay_buffer: ReplayStore,
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
        self._playback_state = _PlaybackState.SOURCE_LOST
        self._latest_live_frame: MediaFrame | None = None
        self._display_frame: MediaFrame | None = None
        self._display_replay_ref: ReplayFrameRef | None = None
        self._latest_live_timestamp: float | None = None
        self._playback_timestamp: float | None = None
        self._lock = threading.RLock()
        self._replay_clock_anchor_timestamp: float | None = None
        self._replay_clock_anchor_monotonic: float | None = None
        self._replay_timer = QTimer(self.signals)
        self._replay_timer.setInterval(40)
        self._replay_timer.timeout.connect(self._on_replay_timer_tick)

    def initialize(self) -> None:
        """Create a session and start placeholder background services."""
        connected = self._pipeline_manager.connect_source()
        source_name = self._pipeline_manager.get_source_name()
        session_paths = self._session_manager.start_new_session(source_name)
        self._pipeline_manager.set_live_sample_callback(self.on_live_sample)
        self._pipeline_manager.start_replay_buffer(session_paths)
        self._pipeline_manager.start_recording(session_paths)
        self._pipeline_manager.start_preview()
        self._pipeline_manager.activate_live_output()

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
        pause_ref: ReplayFrameRef | None = None
        with self._lock:
            self._stop_replay_clock_locked()

            if self._playback_state == _PlaybackState.REPLAY and self._playback_timestamp is not None:
                base_timestamp = self._playback_timestamp
            else:
                base_timestamp = self._latest_live_timestamp

            if base_timestamp is None:
                self._state.error_message = "Cannot pause while the source is unavailable."
                self._emit_state()
                return

            pause_ref = self._replay_buffer.get_frame_ref_at_or_before(base_timestamp)
            if pause_ref is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            self._playback_timestamp = base_timestamp
            self._display_replay_ref = pause_ref
            self._display_frame = None
            self._set_playback_state_locked(_PlaybackState.PAUSED)
            self._state.error_message = None
            self._update_state_timestamps_locked()
            selected_pause_ref = pause_ref
        self._pipeline_manager.activate_replay_output(selected_pause_ref, paused=True)
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Move the viewed output back by ten seconds without stopping ingest."""
        replay_ref: ReplayFrameRef | None = None
        target_timestamp: float | None = None
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

            if self._playback_state == _PlaybackState.LIVE:
                base_timestamp = self._latest_live_timestamp
            else:
                if self._playback_timestamp is not None:
                    base_timestamp = self._playback_timestamp
                else:
                    base_timestamp = None

            if base_timestamp is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            target_timestamp = max(oldest_timestamp, base_timestamp - 10.0)
            replay_ref = self._replay_buffer.get_frame_ref_at_or_before(target_timestamp)
            if replay_ref is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            self._playback_timestamp = target_timestamp
            self._display_replay_ref = replay_ref
            self._display_frame = None
            self._set_playback_state_locked(_PlaybackState.REPLAY)
            self._state.error_message = None
            self._start_replay_clock_locked(target_timestamp)
            self._update_state_timestamps_locked()
            selected_replay_ref = replay_ref
        self._pipeline_manager.activate_replay_output(selected_replay_ref, paused=False)
        self._emit_state(f"Replay -{self._state.seconds_behind_live:.0f}s")

    def jump_to_live(self) -> None:
        """Return the viewed output to the live edge."""
        with self._lock:
            self._stop_replay_clock_locked()
            self._playback_timestamp = self._latest_live_timestamp
            self._display_replay_ref = None
            self._set_playback_state_locked(
                _PlaybackState.LIVE if self._state.source_connected else _PlaybackState.SOURCE_LOST
            )
            self._state.error_message = None
            self._display_frame = None
            self._update_state_timestamps_locked()
        self._pipeline_manager.activate_live_output()
        self._emit_state("Returned to live")

    def attach_preview_widget(self, video_widget: VideoWidget) -> None:
        """Bind the shared embedded video surface to the media pipelines."""
        self._pipeline_manager.set_video_window_handle(video_widget.get_video_surface_handle())
        video_widget.video_surface_resized.connect(self._pipeline_manager.refresh_active_video_output)

    def set_source_lost(self, message: str = "Source signal lost.") -> None:
        """Reflect that the live source is no longer available."""
        with self._lock:
            self._stop_replay_clock_locked()
            self._state.source_connected = False
            self._set_playback_state_locked(_PlaybackState.SOURCE_LOST)
            self._state.error_message = message
            self._state.warning_message = None
        self._emit_state(message)

    def set_source_connected(self) -> None:
        """Reflect that the live source is available again."""
        activate_live_output = False
        status_message = "Source connected"
        with self._lock:
            self._state.source_connected = True
            if self._playback_state == _PlaybackState.SOURCE_LOST:
                self._set_playback_state_locked(_PlaybackState.LIVE)
                activate_live_output = True
            self._state.current_source_name = self._pipeline_manager.get_source_name()
            self._state.error_message = None
            self._sync_source_status_locked()
            if self._state.warning_message:
                status_message = self._state.warning_message
        if activate_live_output:
            self._pipeline_manager.activate_live_output()
        self._emit_state(status_message)

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
        self._replay_timer.stop()
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
            self._sync_source_status_locked()

            if self._playback_state == _PlaybackState.SOURCE_LOST:
                self._set_playback_state_locked(_PlaybackState.LIVE)

            if self._playback_state == _PlaybackState.LIVE:
                self._playback_timestamp = frame.timestamp
                self._display_frame = frame

            self._update_state_timestamps_locked()
        self._emit_state()

    def on_live_sample(self, timestamp: float, source_name: str) -> None:
        """Update controller-owned playback state from the live preview branch."""
        with self._lock:
            self._latest_live_timestamp = timestamp
            self._state.source_connected = True
            self._state.current_source_name = source_name
            self._state.is_recording = self._recorder.is_recording()
            self._sync_source_status_locked()

            if self._playback_state == _PlaybackState.SOURCE_LOST:
                self._set_playback_state_locked(_PlaybackState.LIVE)

            if self._playback_state == _PlaybackState.LIVE:
                self._playback_timestamp = timestamp
                self._display_frame = None

            self._update_state_timestamps_locked()
        self._emit_state()

    def _emit_state(self, status_message: str | None = None) -> None:
        self.signals.state_changed.emit(self._state)
        if status_message:
            self.signals.status_message.emit(status_message)

    def _sync_source_status_locked(self) -> None:
        self._state.warning_message = self._pipeline_manager.get_source_status_message()

    def _start_replay_clock_locked(self, playback_timestamp: float) -> None:
        self._replay_clock_anchor_timestamp = playback_timestamp
        self._replay_clock_anchor_monotonic = time.monotonic()
        if not self._replay_timer.isActive():
            self._replay_timer.start()
        LOGGER.info("Replay clock started at %.3f", playback_timestamp)

    def _stop_replay_clock_locked(self) -> None:
        self._replay_clock_anchor_timestamp = None
        self._replay_clock_anchor_monotonic = None
        if self._replay_timer.isActive():
            self._replay_timer.stop()
            LOGGER.info("Replay clock stopped")

    def _on_replay_timer_tick(self) -> None:
        with self._lock:
            if self._playback_state != _PlaybackState.REPLAY:
                self._stop_replay_clock_locked()
                return

            anchor_timestamp = self._replay_clock_anchor_timestamp
            anchor_monotonic = self._replay_clock_anchor_monotonic

        if anchor_timestamp is None or anchor_monotonic is None:
            return

        oldest_timestamp, latest_timestamp = self._replay_buffer.get_buffer_range()
        if oldest_timestamp is None or latest_timestamp is None:
            return

        elapsed_seconds = max(0.0, time.monotonic() - anchor_monotonic)
        target_timestamp = min(latest_timestamp, max(oldest_timestamp, anchor_timestamp + elapsed_seconds))
        replay_ref = self._replay_buffer.get_frame_ref_at_or_before(target_timestamp)
        if replay_ref is None:
            return

        with self._lock:
            if (
                self._playback_state != _PlaybackState.REPLAY
                or self._replay_clock_anchor_timestamp != anchor_timestamp
                or self._replay_clock_anchor_monotonic != anchor_monotonic
            ):
                return

            self._playback_timestamp = target_timestamp
            self._display_replay_ref = replay_ref
            self._display_frame = None
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._emit_state()

    def _set_playback_state_locked(self, new_state: _PlaybackState) -> None:
        if self._playback_state != new_state and new_state not in _ALLOWED_TRANSITIONS[self._playback_state]:
            raise RuntimeError(
                f"Invalid playback state transition: {self._playback_state.name} -> {new_state.name}"
            )

        if self._playback_state != new_state:
            LOGGER.info(
                "Playback state transition %s -> %s",
                self._playback_state.name,
                new_state.name,
            )
        self._playback_state = new_state
        self._state.current_playback_mode = PlaybackMode[new_state.name]

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
