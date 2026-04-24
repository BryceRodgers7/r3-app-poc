"""Per-output playback orchestration."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence

from PySide6.QtCore import QTimer

from app.core.app_state import AppState
from app.core.models import FrameOverlayInfo, MediaFrame, PlaybackMode, PlaybackOverlayInfo
from app.core.signals import AppSignals
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import MultiFeedOutputRenderer, OutputRenderer
from app.media.recording_manager import RecordingManager
from app.media.replay_buffer import ReplayFrameRef
from app.media.replay_store_manager import ReplayStoreManager

LOGGER = logging.getLogger(__name__)


class PlaybackController:
    """Own playback state for one output session."""

    def __init__(
        self,
        feed_runtimes: Sequence[FeedRuntime],
        output_renderer: OutputRenderer | MultiFeedOutputRenderer,
        recording_manager: RecordingManager,
        replay_store_manager: ReplayStoreManager,
        default_source_name: str,
        session_role: str,
        live_only: bool = False,
    ) -> None:
        if not feed_runtimes:
            raise ValueError("PlaybackController requires at least one FeedRuntime.")
        self._feed_runtimes: tuple[FeedRuntime, ...] = tuple(feed_runtimes)
        self._primary_runtime = self._feed_runtimes[0]
        self._primary_feed_id = self._primary_runtime.feed.feed_id
        self._output_renderer = output_renderer
        self._recording_manager = recording_manager
        self._replay_store_manager = replay_store_manager
        self._replay_buffer = replay_store_manager.get(self._primary_feed_id)
        self._default_source_name = default_source_name
        self._session_role = session_role
        self._live_only = live_only
        self.signals = AppSignals()
        self._state = AppState(current_source_name=default_source_name)
        self._state.current_playback_mode = PlaybackMode.SOURCE_LOST
        self._latest_live_frame: MediaFrame | None = None
        self._latest_live_by_feed: dict[str, MediaFrame] = {}
        self._display_replay_ref: ReplayFrameRef | None = None
        self._latest_live_overlay = FrameOverlayInfo()
        self._latest_live_timestamp: float | None = None
        self._playback_timestamp: float | None = None
        self._playback_rate = 1.0
        self._lock = threading.RLock()
        self._replay_clock_anchor_timestamp: float | None = None
        self._replay_clock_anchor_monotonic: float | None = None
        self._session_id: str | None = None
        self._replay_timer = QTimer(self.signals)
        self._replay_timer.setInterval(40)
        self._replay_timer.timeout.connect(self._on_replay_timer_tick)
        self._overlay_timer = QTimer(self.signals)
        self._overlay_timer.setInterval(250)
        self._overlay_timer.timeout.connect(self._on_overlay_timer_tick)

    def initialize(self, session_id: str) -> None:
        """Activate the controller for started feed runtimes."""
        self._session_id = session_id
        self._state.current_session_id = session_id
        self._state.current_source_name = self._primary_runtime.get_source_name()
        self._sync_all_source_status_locked()
        self._overlay_timer.start()
        for runtime in self._feed_runtimes:
            runtime.add_live_frame_listener(self.on_new_live_frame)
            runtime.add_live_overlay_listener(self.on_live_sample)
        self._refresh_recording_state_locked()
        if all(rt.is_connected() for rt in self._feed_runtimes):
            self.set_source_connected()
        else:
            self.set_source_lost("Unable to connect to source.")

    @property
    def feed_id(self) -> str:
        """Return the primary feed identifier (session / status aggregation)."""
        return self._primary_feed_id

    def pause_playback(self) -> None:
        """Pause only the viewed playback state."""
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        with self._lock:
            self._stop_replay_clock_locked()
            if self._state.current_playback_mode == PlaybackMode.REPLAY and self._playback_timestamp is not None:
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
            self._playback_rate = 0.0
            self._display_replay_ref = pause_ref
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
            ts = base_timestamp
        self._render_all_replay_at_timestamp(ts)
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Move the viewed output back by ten seconds without stopping ingest."""
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
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
                base_timestamp = self._playback_timestamp
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
            self._playback_rate = 1.0
            self._display_replay_ref = replay_ref
            self._state.current_playback_mode = PlaybackMode.REPLAY
            self._state.error_message = None
            self._start_replay_clock_locked(target_timestamp)
            self._update_state_timestamps_locked()
            ts = target_timestamp
        self._render_all_replay_at_timestamp(ts)
        self._emit_state(f"Replay -{self._state.seconds_behind_live:.0f}s")

    def jump_to_live(self) -> None:
        """Return the viewed output to the live edge."""
        with self._lock:
            self._stop_replay_clock_locked()
            self._playback_timestamp = self._latest_live_timestamp
            self._playback_rate = 1.0
            self._display_replay_ref = None
            self._state.current_playback_mode = (
                PlaybackMode.LIVE if self._state.source_connected else PlaybackMode.SOURCE_LOST
            )
            self._state.error_message = None
            self._update_state_timestamps_locked()
        for runtime in self._feed_runtimes:
            frame = self._latest_live_by_feed.get(runtime.feed.feed_id)
            if frame is not None:
                self._output_renderer.show_frame(frame)
        self._emit_state("Returned to live")

    def set_playback_rate(self, playback_rate: float) -> None:
        """Set the replay rate for this output."""
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if self.get_state().current_playback_mode == PlaybackMode.LIVE:
            self.rewind_10_seconds()
        with self._lock:
            self._playback_rate = max(0.0, playback_rate)
            if self._playback_rate == 0.0:
                self._state.current_playback_mode = PlaybackMode.PAUSED
                self._stop_replay_clock_locked()
            else:
                self._state.current_playback_mode = PlaybackMode.REPLAY
                if self._playback_timestamp is not None:
                    self._start_replay_clock_locked(self._playback_timestamp)
            self._update_state_timestamps_locked()
        self._emit_state(f"Replay speed {self._playback_rate:.2f}x")

    def set_source_lost(self, message: str = "Source signal lost.") -> None:
        """Reflect that the live source is no longer available."""
        with self._lock:
            self._stop_replay_clock_locked()
            self._state.source_connected = False
            self._state.current_playback_mode = PlaybackMode.SOURCE_LOST
            self._state.error_message = message
            self._state.warning_message = None
            self._state.ingest_telemetry = None
            self._update_state_timestamps_locked()
        self._output_renderer.show_placeholder_message(message)
        self._emit_state(message)

    def set_source_connected(self) -> None:
        """Reflect that the live source is available again."""
        status_message = "Source connected"
        with self._lock:
            self._state.source_connected = True
            if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST:
                self._state.current_playback_mode = PlaybackMode.LIVE
            self._state.current_source_name = self._primary_runtime.get_source_name()
            self._state.error_message = None
            self._sync_all_source_status_locked()
            self._update_state_timestamps_locked()
            if self._state.warning_message:
                status_message = self._state.warning_message
        self._emit_state(status_message)

    def get_state(self) -> AppState:
        """Return the current application state."""
        with self._lock:
            return self._state

    def refresh_recording_state(self) -> None:
        """Re-read recording flags from the recording manager and notify listeners."""
        with self._lock:
            self._refresh_recording_state_locked()
        self._emit_state()

    def get_display_frame(self) -> MediaFrame | None:
        """Return the frame the UI should currently display (primary feed)."""
        with self._lock:
            if self._state.current_playback_mode == PlaybackMode.LIVE:
                return self._latest_live_frame
            if self._playback_timestamp is None:
                return None
            return self._replay_buffer.get_frame_at_or_before(self._playback_timestamp)

    def shutdown(self) -> None:
        """Stop timers owned by this output session."""
        self._replay_timer.stop()
        self._overlay_timer.stop()

    def on_new_live_frame(self, frame: MediaFrame) -> None:
        """Update controller-owned playback state for a newly ingested live frame."""
        frame_overlay = FrameOverlayInfo.from_media_frame(frame, feed_id=frame.feed_id)
        with self._lock:
            self._latest_live_by_feed[frame.feed_id] = frame
            all_connected = all(rt.is_connected() for rt in self._feed_runtimes)
            self._state.source_connected = all_connected
            if frame.feed_id == self._primary_feed_id:
                self._latest_live_frame = frame
                self._latest_live_overlay = frame_overlay
                self._latest_live_timestamp = frame_overlay.capture_timestamp
                self._state.current_source_name = frame_overlay.source_name
                if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST and all_connected:
                    self._state.current_playback_mode = PlaybackMode.LIVE
                if self._state.current_playback_mode == PlaybackMode.LIVE:
                    self._playback_timestamp = frame_overlay.capture_timestamp
                    self._state.frame_overlay = frame_overlay
            self._refresh_recording_state_locked()
            self._sync_all_source_status_locked()
            if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST and all_connected:
                self._state.current_playback_mode = PlaybackMode.LIVE
            self._update_state_timestamps_locked()
            if self._live_only:
                should_show_frame = True
            else:
                should_show_frame = self._state.current_playback_mode == PlaybackMode.LIVE
        if should_show_frame:
            self._output_renderer.show_frame(frame)
        self._emit_state()

    def on_live_sample(self, frame_overlay: FrameOverlayInfo) -> None:
        """Update controller-owned playback state from the live preview branch."""
        if frame_overlay.feed_id != self._primary_feed_id:
            return
        with self._lock:
            self._latest_live_overlay = frame_overlay
            self._latest_live_timestamp = frame_overlay.capture_timestamp
            self._state.source_connected = all(rt.is_connected() for rt in self._feed_runtimes)
            self._state.current_source_name = frame_overlay.source_name
            self._refresh_recording_state_locked()
            self._sync_all_source_status_locked()
            if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST and self._state.source_connected:
                self._state.current_playback_mode = PlaybackMode.LIVE
            if self._state.current_playback_mode == PlaybackMode.LIVE:
                self._playback_timestamp = frame_overlay.capture_timestamp
                self._state.frame_overlay = frame_overlay
            self._update_state_timestamps_locked()
        self._emit_state()

    def _refresh_recording_state_locked(self) -> None:
        if len(self._feed_runtimes) > 1:
            self._state.is_recording = self._recording_manager.is_any_recording()
        else:
            self._state.is_recording = self._recording_manager.is_recording(self._primary_feed_id)

    def _emit_state(self, status_message: str | None = None) -> None:
        self.signals.state_changed.emit(self._state)
        if status_message:
            self.signals.status_message.emit(status_message)

    def _sync_all_source_status_locked(self) -> None:
        parts = [rt.get_status_message() for rt in self._feed_runtimes]
        messages = [m for m in parts if m]
        self._state.warning_message = " | ".join(messages) if messages else None
        self._state.ingest_telemetry = self._primary_runtime.get_ingest_telemetry()

    def _start_replay_clock_locked(self, playback_timestamp: float) -> None:
        self._replay_clock_anchor_timestamp = playback_timestamp
        self._replay_clock_anchor_monotonic = time.monotonic()
        if self._playback_rate > 0.0 and not self._replay_timer.isActive():
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
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                self._stop_replay_clock_locked()
                return
            anchor_timestamp = self._replay_clock_anchor_timestamp
            anchor_monotonic = self._replay_clock_anchor_monotonic
        if anchor_timestamp is None or anchor_monotonic is None:
            return
        oldest_timestamp, latest_timestamp = self._replay_buffer.get_buffer_range()
        if oldest_timestamp is None or latest_timestamp is None:
            return
        elapsed_seconds = max(0.0, time.monotonic() - anchor_monotonic) * self._playback_rate
        target_timestamp = min(latest_timestamp, max(oldest_timestamp, anchor_timestamp + elapsed_seconds))
        replay_ref = self._replay_buffer.get_frame_ref_at_or_before(target_timestamp)
        if replay_ref is None:
            return
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                return
            self._playback_timestamp = target_timestamp
            self._display_replay_ref = replay_ref
            self._state.error_message = None
            self._update_state_timestamps_locked()
            ts = target_timestamp
        self._render_all_replay_at_timestamp(ts)
        self._emit_state()

    def _on_overlay_timer_tick(self) -> None:
        with self._lock:
            if self._state.current_playback_mode not in {PlaybackMode.PAUSED, PlaybackMode.SOURCE_LOST}:
                return
            self._update_state_timestamps_locked()
        self._emit_state()

    def _update_state_timestamps_locked(self) -> None:
        self._state.last_frame_timestamp = self._latest_live_timestamp
        self._state.replay_buffer_span_seconds = self._replay_buffer.get_available_duration()
        if self._state.current_playback_mode == PlaybackMode.LIVE or self._playback_timestamp is None:
            self._state.seconds_behind_live = 0.0
        else:
            self._state.seconds_behind_live = self._replay_buffer.get_seconds_behind_live(
                self._playback_timestamp
            )
        if self._state.current_playback_mode == PlaybackMode.LIVE:
            self._state.frame_overlay = self._latest_live_overlay
        elif self._display_replay_ref is not None:
            self._state.frame_overlay = self._frame_overlay_from_replay_ref(self._display_replay_ref)
        else:
            self._state.frame_overlay = FrameOverlayInfo(
                feed_id=self._latest_live_overlay.feed_id,
                source_name=self._state.current_source_name,
                capture_timestamp=self._playback_timestamp,
            )

        self._state.playback_overlay = PlaybackOverlayInfo(
            mode=self._state.current_playback_mode,
            playback_timestamp=self._playback_timestamp,
            wall_clock_timestamp=time.time(),
            seconds_behind_live=self._state.seconds_behind_live,
            playback_rate=self._playback_rate,
            status_text=self._build_overlay_status_locked(),
        )

    def _frame_overlay_from_replay_ref(self, replay_ref: ReplayFrameRef) -> FrameOverlayInfo:
        return FrameOverlayInfo(
            feed_id=replay_ref.feed_id,
            source_name=replay_ref.source_name,
            frame_id=replay_ref.frame_id,
            capture_timestamp=replay_ref.timestamp,
        )

    def _build_overlay_status_locked(self) -> str | None:
        if self._state.current_playback_mode == PlaybackMode.PAUSED:
            return "Capture, recording, and replay buffering continue"
        if self._state.current_playback_mode == PlaybackMode.REPLAY:
            return (
                f"Viewing approximately {self._state.seconds_behind_live:.0f}s behind live "
                f"at {self._playback_rate:.2f}x"
            )
        if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST:
            return self._state.error_message or "Waiting for the selected source"
        return self._state.warning_message

    def _render_all_replay_at_timestamp(self, timestamp: float) -> None:
        """Draw each feed at the same logical timeline position (primary clock)."""
        for runtime in self._feed_runtimes:
            feed_id = runtime.feed.feed_id
            buf = self._replay_store_manager.get(feed_id)
            ref = buf.get_frame_ref_at_or_before(timestamp)
            if ref is None:
                continue
            frame = buf.get_frame_at_or_before(ref.timestamp)
            if frame is not None:
                self._output_renderer.show_frame(frame)
