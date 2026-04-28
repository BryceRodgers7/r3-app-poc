"""Per-output playback orchestration."""

from __future__ import annotations

from collections.abc import Callable
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
from app.media.segment_decoder import SegmentDecoder
from app.storage.segment_replay_store import RecordingSegmentReplayStore

LOGGER = logging.getLogger(__name__)

# Instant-replay shortcuts from §19. The 10s button is the primary
# transport; the 30s button is mainly for testing the §11.4 Resume
# flow (lets the operator seek into pre-crash segments without holding
# down the rewind button).
_REWIND_10S_NS = 10 * 1_000_000_000
_REWIND_30S_NS = 30 * 1_000_000_000


class PlaybackController:
    """Own playback state for one output session.

    Slice 4.C.tail: replay rendering now decodes the recorded segment
    files via `SegmentDecoder` (one per controller, scoped to the
    primary feed). The replay clock operates in PTS-nanoseconds — the
    same time domain `SegmentIndex` and `RecordingSegmentReplayStore`
    use — and converts to "seconds behind live" for the operator UI by
    diffing against the latest replayable PTS.

    Multi-feed synchronized replay (rewinding all feeds at the same
    logical timeline position) requires the Phase-5 `SessionClock` to
    bridge per-feed PTS origins; for now only the primary feed renders
    rewound video. Other feeds keep showing whatever frame they most
    recently received (the operator UI will see a "frozen" live frame
    on the secondary tiles during replay, which is acceptable for the
    MJPEG-MKV milestone).
    """

    def __init__(
        self,
        feed_runtimes: Sequence[FeedRuntime],
        output_renderer: OutputRenderer | MultiFeedOutputRenderer,
        recording_manager: RecordingManager,
        replay_store: RecordingSegmentReplayStore,
        default_source_name: str,
        session_role: str,
        live_only: bool = False,
        decoder_factory: Callable[[str, str], SegmentDecoder] | None = None,
    ) -> None:
        if not feed_runtimes:
            raise ValueError("PlaybackController requires at least one FeedRuntime.")
        self._feed_runtimes: tuple[FeedRuntime, ...] = tuple(feed_runtimes)
        self._primary_runtime = self._feed_runtimes[0]
        self._primary_feed_id = self._primary_runtime.feed.feed_id
        self._output_renderer = output_renderer
        self._recording_manager = recording_manager
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
        # Replay clock state — PTS-ns space (slice 4.C.tail).
        self._playback_pts_ns: int | None = None
        self._playback_rate = 1.0
        self._lock = threading.RLock()
        self._replay_clock_anchor_pts_ns: int | None = None
        self._replay_clock_anchor_monotonic: float | None = None
        self._session_id: str | None = None
        self._replay_timer = QTimer(self.signals)
        self._replay_timer.setInterval(40)
        self._replay_timer.timeout.connect(self._on_replay_timer_tick)
        self._overlay_timer = QTimer(self.signals)
        self._overlay_timer.setInterval(250)
        self._overlay_timer.timeout.connect(self._on_overlay_timer_tick)
        self.replay_state = (
            None if live_only else make_replay_state_machine(role=session_role)
        )
        # Segment decoder for the primary feed. Live-only outputs (the
        # program window) don't replay so they don't allocate one.
        if live_only:
            self._segment_decoder: SegmentDecoder | None = None
        else:
            decoder_factory = decoder_factory or (
                lambda feed_id, source_name: SegmentDecoder(feed_id, source_name)
            )
            self._segment_decoder = decoder_factory(
                self._primary_feed_id,
                self._primary_runtime.feed.display_name,
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
        """Pause replay at the current playback position."""
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        with self._lock:
            self._stop_replay_clock_locked()
            base_pts_ns = self._resolve_pause_anchor_locked()
            if base_pts_ns is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return
            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.PAUSED)
            self._playback_pts_ns = base_pts_ns
            self._playback_rate = 0.0
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
        # Render the freeze frame outside the lock so the renderer can
        # emit Qt signals without re-entering the controller.
        self._render_at_pts_ns(base_pts_ns)
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Move the viewed output back 10 seconds in the segment timeline."""
        self._rewind_by_ns(_REWIND_10S_NS)

    def rewind_30_seconds(self) -> None:
        """Move the viewed output back 30 seconds in the segment timeline.

        Same machinery as `rewind_10_seconds`; just a different anchor.
        Useful for stepping into pre-crash segments after a §11.4 Resume.
        """
        self._rewind_by_ns(_REWIND_30S_NS)

    def _rewind_by_ns(self, rewind_ns: int) -> None:
        """Common rewind implementation parameterized by jump distance."""
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        with self._lock:
            target_pts_ns = self._resolve_rewind_target_locked(rewind_ns)
            if target_pts_ns is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return
            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.SEEKING)
            self.replay_state.transition_to(ReplayState.REPLAYING)
            self._playback_pts_ns = target_pts_ns
            self._playback_rate = 1.0
            self._state.current_playback_mode = PlaybackMode.REPLAY
            self._state.error_message = None
            self._start_replay_clock_locked(target_pts_ns)
            self._update_state_timestamps_locked()
        self._render_at_pts_ns(target_pts_ns)
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
            self._playback_pts_ns = None
            self._playback_rate = 1.0
            self._state.current_playback_mode = (
                PlaybackMode.LIVE if self._state.source_connected else PlaybackMode.SOURCE_LOST
            )
            self._state.error_message = None
            self._update_state_timestamps_locked()
        # Re-show the latest live frame for every bound feed. The
        # renderer remembers the most recent frame per feed_id so
        # secondary tiles snap back to live alongside the primary.
        for runtime in self._feed_runtimes:
            frame = self._latest_live_by_feed.get(runtime.feed.feed_id)
            if frame is not None:
                self._output_renderer.show_frame(frame)
        self._emit_state("Returned to live")

    def set_playback_rate(self, playback_rate: float) -> None:
        """Set the replay rate for this output.

        Slow motion (0.5, 0.25) advances the replay clock at the
        requested fraction; the decoder is rate-agnostic. A rate of 0.0
        is treated as a pause (replay clock stops, no further decode
        ticks).
        """
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
                if self._playback_pts_ns is not None:
                    self._start_replay_clock_locked(self._playback_pts_ns)
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
        """Return the frame the UI should currently display (primary feed)."""
        with self._lock:
            return self._latest_live_frame

    def shutdown(self) -> None:
        """Stop timers owned by this output session."""
        self._replay_timer.stop()
        self._overlay_timer.stop()
        if self._segment_decoder is not None:
            self._segment_decoder.close()

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
                    self._state.error_message = None
                if self._state.current_playback_mode == PlaybackMode.LIVE:
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
                self._state.error_message = None
            if self._state.current_playback_mode == PlaybackMode.LIVE:
                self._state.frame_overlay = frame_overlay
            self._update_state_timestamps_locked()
        self._emit_state()

    def _resolve_pause_anchor_locked(self) -> int | None:
        """Pick a reasonable PTS to freeze on when the operator presses Pause.

        - In REPLAY mode, freeze at the current playback position.
        - In LIVE/SOURCE_LOST modes, freeze at the latest replayable PTS
          so the operator sees something concrete (the freshest fully-
          finalized segment frame).
        """
        if (
            self._state.current_playback_mode == PlaybackMode.REPLAY
            and self._playback_pts_ns is not None
        ):
            return self._playback_pts_ns
        return self._replay_store.latest_replayable_pts(self._primary_feed_id)

    def _resolve_rewind_target_locked(self, rewind_ns: int) -> int | None:
        """Compute a target PTS `rewind_ns` behind the latest replayable point."""
        latest = self._replay_store.latest_replayable_pts(self._primary_feed_id)
        if latest is None:
            return None
        earliest = self._replay_store.earliest_pts(self._primary_feed_id)
        target = latest - rewind_ns
        if earliest is not None:
            target = max(target, earliest)
        return target

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
        if current in ACTIVE_REPLAY_STATES:
            self.replay_state.transition_to(ReplayState.JUMPING_TO_LIVE)
            self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
            self._stop_replay_clock_locked()
            self._playback_pts_ns = None
            self._playback_rate = 1.0
            self._state.current_playback_mode = (
                PlaybackMode.LIVE
                if self._state.source_connected
                else PlaybackMode.SOURCE_LOST
            )
        elif current == ReplayState.LIVE_WHILE_RECORDING:
            self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)

    def _replay_actions_allowed(self) -> bool:
        """Return True when replay transport is permitted by recording state."""
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

    def _start_replay_clock_locked(self, playback_pts_ns: int) -> None:
        self._replay_clock_anchor_pts_ns = playback_pts_ns
        self._replay_clock_anchor_monotonic = time.monotonic()
        if self._playback_rate > 0.0 and not self._replay_timer.isActive():
            self._replay_timer.start()
        LOGGER.info("Replay clock started at pts=%dns", playback_pts_ns)

    def _stop_replay_clock_locked(self) -> None:
        self._replay_clock_anchor_pts_ns = None
        self._replay_clock_anchor_monotonic = None
        if self._replay_timer.isActive():
            self._replay_timer.stop()
            LOGGER.info("Replay clock stopped")

    def _on_replay_timer_tick(self) -> None:
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                self._stop_replay_clock_locked()
                return
            anchor_pts_ns = self._replay_clock_anchor_pts_ns
            anchor_monotonic = self._replay_clock_anchor_monotonic
            rate = self._playback_rate
        if anchor_pts_ns is None or anchor_monotonic is None:
            return
        elapsed_ns = max(0.0, time.monotonic() - anchor_monotonic) * rate * 1_000_000_000
        target_pts_ns = anchor_pts_ns + int(elapsed_ns)
        latest = self._replay_store.latest_replayable_pts(self._primary_feed_id)
        if latest is not None:
            target_pts_ns = min(target_pts_ns, latest)
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                return
            self._playback_pts_ns = target_pts_ns
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._render_at_pts_ns(target_pts_ns)
        self._emit_state()

    def _on_overlay_timer_tick(self) -> None:
        with self._lock:
            if self._state.current_playback_mode not in {PlaybackMode.PAUSED, PlaybackMode.SOURCE_LOST}:
                return
            self._update_state_timestamps_locked()
        self._emit_state()

    def _render_at_pts_ns(self, target_pts_ns: int) -> None:
        """Decode and display the segment frame containing `target_pts_ns`.

        Resolves through `replay_store` so eligibility (recording-state
        gate) and segment-state filtering (only `complete` segments) stay
        in one place. No-op for `live_only` outputs.
        """
        if self._segment_decoder is None:
            return
        location = self._replay_store.resolve(
            feed_id=self._primary_feed_id,
            target_pts_ns=target_pts_ns,
            recording_state=self._recording_manager.recording_state.state,
        )
        if location is None:
            return
        frame = self._segment_decoder.decode(location)
        if frame is None:
            return
        self._output_renderer.show_frame(frame)

    def _update_state_timestamps_locked(self) -> None:
        self._state.last_frame_timestamp = self._latest_live_timestamp
        latest_pts_ns = self._replay_store.latest_replayable_pts(self._primary_feed_id)
        earliest_pts_ns = self._replay_store.earliest_pts(self._primary_feed_id)
        if (
            latest_pts_ns is not None
            and earliest_pts_ns is not None
            and latest_pts_ns >= earliest_pts_ns
        ):
            self._state.replay_buffer_span_seconds = (
                latest_pts_ns - earliest_pts_ns
            ) / 1_000_000_000.0
        else:
            self._state.replay_buffer_span_seconds = 0.0
        if (
            self._state.current_playback_mode == PlaybackMode.LIVE
            or self._playback_pts_ns is None
            or latest_pts_ns is None
        ):
            self._state.seconds_behind_live = 0.0
        else:
            self._state.seconds_behind_live = max(
                0.0, (latest_pts_ns - self._playback_pts_ns) / 1_000_000_000.0
            )
        if self._state.current_playback_mode == PlaybackMode.LIVE:
            self._state.frame_overlay = self._latest_live_overlay
        else:
            self._state.frame_overlay = FrameOverlayInfo(
                feed_id=self._latest_live_overlay.feed_id,
                source_name=self._state.current_source_name,
                capture_timestamp=None,
            )

        self._state.playback_overlay = PlaybackOverlayInfo(
            mode=self._state.current_playback_mode,
            playback_timestamp=(
                self._playback_pts_ns / 1_000_000_000.0
                if self._playback_pts_ns is not None
                else None
            ),
            wall_clock_timestamp=time.time(),
            seconds_behind_live=self._state.seconds_behind_live,
            playback_rate=self._playback_rate,
            status_text=self._build_overlay_status_locked(),
        )

    def _build_overlay_status_locked(self) -> str | None:
        if self._state.current_playback_mode == PlaybackMode.PAUSED:
            return "Capture and recording continue while replay is paused"
        if self._state.current_playback_mode == PlaybackMode.REPLAY:
            return (
                f"Viewing approximately {self._state.seconds_behind_live:.0f}s behind live "
                f"at {self._playback_rate:.2f}x"
            )
        if self._state.current_playback_mode == PlaybackMode.SOURCE_LOST:
            return self._state.error_message or "Waiting for the selected source"
        return self._state.warning_message
