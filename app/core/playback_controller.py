"""Per-output playback orchestration."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence

from PySide6.QtCore import QTimer

from app.core.app_state import UiState
from app.core.models import FrameOverlayInfo, MediaFrame, PlaybackMode, PlaybackOverlayInfo
from app.core.recording_state import RecordingState
from app.core.replay_state import ACTIVE_REPLAY_STATES, ReplayState, make_replay_state_machine
from app.core.signals import AppSignals
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import MultiFeedOutputRenderer, OutputRenderer
from app.media.recording_manager import RecordingManager
from app.storage.segment_replay_store import RecordingSegmentReplayStore

LOGGER = logging.getLogger(__name__)


class PlaybackController:
    """Own playback state for one output session."""

    def __init__(
        self,
        feed_runtimes: Sequence[FeedRuntime],
        output_renderer: OutputRenderer | MultiFeedOutputRenderer,
        recording_manager: RecordingManager,
        replay_store: RecordingSegmentReplayStore,
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
        # Slice 4.D: replaced the legacy `ReplayStoreManager` /
        # `ReplayBuffer` rolling-frame store with the segment-index-backed
        # `RecordingSegmentReplayStore`. Eligibility checks use the new
        # store's `is_replay_available` gate; actual segment-file replay
        # rendering is deferred to slice 4.C.tail (see `r3_app_architecture.md`).
        self._replay_store = replay_store
        self._default_source_name = default_source_name
        self._session_role = session_role
        self._live_only = live_only
        self.signals = AppSignals()
        self._state = UiState(current_source_name=default_source_name)
        self._state.current_playback_mode = PlaybackMode.SOURCE_LOST
        self._latest_live_frame: MediaFrame | None = None
        self._latest_live_by_feed: dict[str, MediaFrame] = {}
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
        # Operator-only replay state machine. The program controller leaves
        # this None and continues to behave purely on `live_only`.
        self.replay_state = (
            None if live_only else make_replay_state_machine(role=session_role)
        )

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
        """Pause replay (state-machine only — segment-file rendering pending 4.C.tail)."""
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
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
            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.PAUSED)
            self._playback_timestamp = base_timestamp
            self._playback_rate = 0.0
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
        # TODO(4.C.tail): render the paused frame from the segment file at
        # `base_timestamp`. For now the renderer keeps showing the most
        # recent live frame.
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Move the viewed output back by ten seconds without stopping ingest.

        Slice 4.D: uses `replay_store.is_replay_available` as the eligibility
        gate but does not yet decode segment files. Frame rendering for the
        rewound timeline lands in slice 4.C.tail. Until then this method
        only drives the replay state machine and the playback clock — the
        renderer continues to display the latest live frame.
        """
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        with self._lock:
            if self._state.current_playback_mode == PlaybackMode.LIVE:
                base_timestamp = self._latest_live_timestamp
            else:
                base_timestamp = self._playback_timestamp
            if base_timestamp is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return

            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.SEEKING)
            target_timestamp = base_timestamp - 10.0
            self.replay_state.transition_to(ReplayState.REPLAYING)
            self._playback_timestamp = target_timestamp
            self._playback_rate = 1.0
            self._state.current_playback_mode = PlaybackMode.REPLAY
            self._state.error_message = None
            self._start_replay_clock_locked(target_timestamp)
            self._update_state_timestamps_locked()
        # TODO(4.C.tail): seek the segment-file player to `target_timestamp`
        # and render frames from there.
        self._emit_state(f"Replay -{self._state.seconds_behind_live:.0f}s")

    def jump_to_live(self) -> None:
        """Return the viewed output to the live edge."""
        with self._lock:
            self._stop_replay_clock_locked()
            if self.replay_state is not None and self.replay_state.state in ACTIVE_REPLAY_STATES:
                self.replay_state.transition_to(ReplayState.JUMPING_TO_LIVE)
                if self._recording_manager.recording_state.state == RecordingState.RECORDING:
                    self.replay_state.transition_to(ReplayState.LIVE_WHILE_RECORDING)
                else:
                    self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
            self._playback_timestamp = self._latest_live_timestamp
            self._playback_rate = 1.0
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
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        if self.get_state().current_playback_mode == PlaybackMode.LIVE:
            self.rewind_10_seconds()
        with self._lock:
            assert self.replay_state is not None
            self._playback_rate = max(0.0, playback_rate)
            if self._playback_rate == 0.0:
                self.replay_state.transition_to(ReplayState.PAUSED)
                self._state.current_playback_mode = PlaybackMode.PAUSED
                self._stop_replay_clock_locked()
            else:
                target = (
                    ReplayState.SLOW_MOTION
                    if self._playback_rate < 1.0
                    else ReplayState.REPLAYING
                )
                self.replay_state.transition_to(target)
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

    def get_state(self) -> UiState:
        """Return the current application state."""
        with self._lock:
            return self._state

    def refresh_recording_state(self) -> None:
        """Re-read recording flags from the recording manager and notify listeners."""
        with self._lock:
            self._refresh_recording_state_locked()
        self._emit_state()

    def get_display_frame(self) -> MediaFrame | None:
        """Return the frame the UI should currently display (primary feed).

        Slice 4.D: replay-mode frame retrieval is stubbed pending 4.C.tail
        — the rolling JPEG buffer is gone and segment-file replay is not
        yet wired in. For now we always return the latest live frame; the
        operator UI will continue to render live pixels even while the
        controller is in REPLAY/PAUSED state. The replay overlay still
        reports the seek target via `playback_overlay`.
        """
        with self._lock:
            return self._latest_live_frame

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
                    # Clear the startup "Unable to connect to source" placeholder
                    # once frames actually start arriving.
                    self._state.error_message = None
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
                # Clear the startup "Unable to connect to source" placeholder
                # once frames actually start arriving.
                self._state.error_message = None
            if self._state.current_playback_mode == PlaybackMode.LIVE:
                self._playback_timestamp = frame_overlay.capture_timestamp
                self._state.frame_overlay = frame_overlay
            self._update_state_timestamps_locked()
        self._emit_state()

    def _refresh_recording_state_locked(self) -> None:
        # Slice 2.B introduced the RecordingState machine; slice 4.A bypassed
        # the legacy `Recorder.is_recording()` path that the older check
        # used. Read the recording flag from the state machine — it's the
        # one piece of state both the toggle button and the diagnostics
        # widget agree on.
        self._state.is_recording = (
            self._recording_manager.recording_state.state == RecordingState.RECORDING
        )
        self._sync_replay_state_with_recording_locked()

    def _sync_replay_state_with_recording_locked(self) -> None:
        """Mirror long-recording transitions into the replay state machine.

        - Enter `LIVE_WHILE_RECORDING` when long recording starts.
        - Snap any active replay back to live and then to
          `REPLAY_UNAVAILABLE_NOT_RECORDING` when long recording stops.
        """
        if self.replay_state is None:
            return
        recording_active = (
            self._recording_manager.recording_state.state == RecordingState.RECORDING
        )
        current = self.replay_state.state
        if recording_active:
            if current == ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING:
                self.replay_state.transition_to(ReplayState.LIVE_WHILE_RECORDING)
            return
        # Recording is not active.
        if current in ACTIVE_REPLAY_STATES:
            self.replay_state.transition_to(ReplayState.JUMPING_TO_LIVE)
            self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
            self._stop_replay_clock_locked()
            self._playback_timestamp = self._latest_live_timestamp
            self._playback_rate = 1.0
            self._state.current_playback_mode = (
                PlaybackMode.LIVE
                if self._state.source_connected
                else PlaybackMode.SOURCE_LOST
            )
        elif current == ReplayState.LIVE_WHILE_RECORDING:
            self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)

    def _replay_actions_allowed(self) -> bool:
        """Return True when replay transport is permitted by recording state.

        Per §10.4 / §15.2: replay is bound to long recording. The
        `RecordingSegmentReplayStore` mirrors this rule via
        `is_replay_available(recording_state=...)`.
        """
        if self.replay_state is None:
            return False
        return self._replay_store.is_replay_available(
            recording_state=self._recording_manager.recording_state.state
        )

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
        """Advance the replay-clock playback timestamp.

        Slice 4.D: the clock advances and `playback_overlay` reports
        progress, but no frames are decoded — that wires up in 4.C.tail.
        """
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                self._stop_replay_clock_locked()
                return
            anchor_timestamp = self._replay_clock_anchor_timestamp
            anchor_monotonic = self._replay_clock_anchor_monotonic
        if anchor_timestamp is None or anchor_monotonic is None:
            return
        elapsed_seconds = max(0.0, time.monotonic() - anchor_monotonic) * self._playback_rate
        target_timestamp = anchor_timestamp + elapsed_seconds
        if self._latest_live_timestamp is not None:
            target_timestamp = min(target_timestamp, self._latest_live_timestamp)
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                return
            self._playback_timestamp = target_timestamp
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._emit_state()

    def _on_overlay_timer_tick(self) -> None:
        with self._lock:
            if self._state.current_playback_mode not in {PlaybackMode.PAUSED, PlaybackMode.SOURCE_LOST}:
                return
            self._update_state_timestamps_locked()
        self._emit_state()

    def _update_state_timestamps_locked(self) -> None:
        self._state.last_frame_timestamp = self._latest_live_timestamp
        # Slice 4.D: rolling-buffer span no longer exists. The
        # segment-index store tracks coverage in PTS nanoseconds; mapping
        # that to wall-clock seconds for the operator UI lands in
        # 4.C.tail. Report 0.0 until then.
        self._state.replay_buffer_span_seconds = 0.0
        if self._state.current_playback_mode == PlaybackMode.LIVE or self._playback_timestamp is None:
            self._state.seconds_behind_live = 0.0
        elif self._latest_live_timestamp is not None:
            self._state.seconds_behind_live = max(
                0.0, self._latest_live_timestamp - self._playback_timestamp
            )
        else:
            self._state.seconds_behind_live = 0.0
        if self._state.current_playback_mode == PlaybackMode.LIVE:
            self._state.frame_overlay = self._latest_live_overlay
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
