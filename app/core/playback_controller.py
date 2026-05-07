"""Per-output playback orchestration."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer

from app.core.app_state import UiState
from app.core.models import FrameOverlayInfo, MediaFrame, PlaybackMode, PlaybackOverlayInfo
from app.core.recording_state import RecordingState
from app.core.replay_state import ACTIVE_REPLAY_STATES, ReplayState, make_replay_state_machine
from app.core.session_clock import SessionClock
from app.core.signals import AppSignals
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import MultiFeedOutputRenderer, OutputRenderer
from app.media.recording_manager import RecordingManager
from app.media.segment_decoder import SegmentDecoder
from app.storage.segment_replay_store import RecordingSegmentReplayStore

if TYPE_CHECKING:
    from app.core.play_manager import PlayManager

LOGGER = logging.getLogger(__name__)

# Instant-replay shortcut from §19. Slice 5.C: the 30s button was
# removed in favor of repeated 10s clicks accumulating from the
# current playback position.
_REWIND_10S_NS = 10 * 1_000_000_000

# Phase 12.A default — 30 fps. The coordinator overrides this from
# `AppSettings.target_fps` in 12.B; tests and any caller that doesn't
# configure it fall back to the 30 fps assumption.
_DEFAULT_FRAME_PERIOD_NS = 33_333_333


class PlaybackController:
    """Own playback state for one output session.

    Slice 5.C: the replay clock operates in **session-time** — the
    `SessionClock`-anchored timeline that every feed maps onto via
    `pts_to_session_offset_ns` (slice 5.A). Multi-feed render uses
    `RecordingSegmentReplayStore.nearest_frame_location` per feed
    (slice 5.B) so every tile renders something on every tick: feeds
    with coverage at the target time render that exact frame, feeds
    without coverage freeze on their nearest available frame per the
    §8.6.1 rule.

    Each feed gets its own `SegmentDecoder` so per-feed `cv2.VideoCapture`
    state doesn't fight across tiles. `live_only` outputs (program
    window) skip allocation entirely — they never replay.

    Rewind anchoring (slice 5.C): a Rewind 10s click anchors on the
    operator's *current playback position* when in REPLAY/PAUSED so
    repeated clicks accumulate (10s + 10s = -20s). From LIVE the
    anchor is the latest replayable session time across all feeds, so
    the first click from live always lands at "now − 10s".
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
        session_clock: SessionClock | None = None,
        play_manager: "PlayManager | None" = None,
        frame_period_ns: int = _DEFAULT_FRAME_PERIOD_NS,
    ) -> None:
        if not feed_runtimes:
            raise ValueError("PlaybackController requires at least one FeedRuntime.")
        self._feed_runtimes: tuple[FeedRuntime, ...] = tuple(feed_runtimes)
        self._primary_runtime = self._feed_runtimes[0]
        self._primary_feed_id = self._primary_runtime.feed.feed_id
        self._output_renderer = output_renderer
        self._recording_manager = recording_manager
        self._replay_store = replay_store
        # Slice 5.A clock is reused here for the smooth "behind live"
        # counter — `seconds_behind_live` reads from
        # `clock.now_session_time_ns()` rather than from the latest
        # finalized segment so the indicator grows steadily during
        # pause / slow-motion instead of jumping in segment-duration
        # quanta. Falls back to the segment-derived metric when no
        # clock is attached (test fixtures, headless tooling).
        self._session_clock = session_clock
        # Phase 7.H.3: read-only access to the PlayManager so
        # `_update_state_timestamps_locked` can populate the
        # current play number on UiState + PlaybackOverlayInfo.
        self._play_manager = play_manager
        self._default_source_name = default_source_name
        self._session_role = session_role
        self._live_only = live_only
        # Phase 12.A: replay clock delta per step button press. Set once
        # at construction (the coordinator computes from
        # `settings.target_fps`); `step_frames` multiplies by the
        # operator's `frame_delta`.
        self._frame_period_ns = max(1, int(frame_period_ns))
        # Phase 10.F: set by `shutdown()` so transport methods that race
        # the teardown become no-ops instead of touching half-torn-down
        # decoders / timers.
        self._shutting_down = False
        self.signals = AppSignals()
        self._state = UiState(current_source_name=default_source_name)
        self._state.current_playback_mode = PlaybackMode.SOURCE_LOST
        self._latest_live_frame: MediaFrame | None = None
        self._latest_live_by_feed: dict[str, MediaFrame] = {}
        self._latest_live_overlay = FrameOverlayInfo()
        self._latest_live_timestamp: float | None = None
        # Replay clock state — session-time-ns (slice 5.C).
        self._playback_session_time_ns: int | None = None
        self._playback_rate = 1.0
        self._lock = threading.RLock()
        self._replay_clock_anchor_session_time_ns: int | None = None
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
        # Slice 5.C: one SegmentDecoder per feed. Each owns its own
        # `cv2.VideoCapture` so per-feed seek state doesn't collide
        # when the multi-feed render decodes all tiles on a single
        # replay tick. Live-only outputs (program window) skip the
        # allocation — they never replay.
        if live_only:
            self._segment_decoders: dict[str, SegmentDecoder] = {}
        else:
            decoder_factory = decoder_factory or (
                lambda feed_id, source_name: SegmentDecoder(feed_id, source_name)
            )
            self._segment_decoders = {
                runtime.feed.feed_id: decoder_factory(
                    runtime.feed.feed_id, runtime.feed.display_name
                )
                for runtime in self._feed_runtimes
            }

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
        if getattr(self, "_shutting_down", False):
            return
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
            self._playback_session_time_ns = base_pts_ns
            self._playback_rate = 0.0
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
        # Render the freeze frame outside the lock so the renderer can
        # emit Qt signals without re-entering the controller.
        self._render_at_session_time_ns(base_pts_ns)
        self._emit_state("Playback paused")

    def rewind_10_seconds(self) -> None:
        """Rewind 10s in session-time (slice 5.C).

        From LIVE/SOURCE_LOST: anchor on `latest_replayable_session_time`
        across all feeds. The first click from live always lands at
        `now − 10s`.

        From REPLAY/PAUSED: anchor on the operator's current
        `_playback_session_time_ns`. Repeated clicks accumulate —
        click twice for `−20s` from live, three times for `−30s`, etc.
        """
        if getattr(self, "_shutting_down", False):
            return
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        with self._lock:
            target_session_time_ns = self._resolve_rewind_target_locked(_REWIND_10S_NS)
            if target_session_time_ns is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return
            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.SEEKING)
            self.replay_state.transition_to(ReplayState.REPLAYING)
            self._playback_session_time_ns = target_session_time_ns
            self._playback_rate = 1.0
            self._state.current_playback_mode = PlaybackMode.REPLAY
            self._state.error_message = None
            self._start_replay_clock_locked(target_session_time_ns)
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        self._emit_state(f"Replay -{self._state.seconds_behind_live:.0f}s")

    def replay_current_play(self) -> None:
        """Phase 7.H.4: seek to the start of the currently-open play.

        Operator's "Replay Play" button. The currently-open play's
        `start_session_time_ns` is the seek target — playback resumes
        at 1.0x from there. To go further back, the operator stacks
        Rewind 10s clicks.

        No-op when:
          - this is the live-only program output
          - replay isn't available (recording not active)
          - no PlayManager is attached (older test paths)
          - no play is currently open (defensive — Play #1 should
            always be open when recording is active)
        """
        if getattr(self, "_shutting_down", False):
            return
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        if self._play_manager is None:
            return
        current = self._play_manager.current_play()
        if current is None:
            self._emit_state("No play is currently open.")
            return
        target_session_time_ns = current.start_session_time_ns
        # Defensive clamp to the per-game replay scope's earliest, in
        # case the play marker drifted slightly before the first
        # finalized segment of the game (Phase 5.C / §8.6.1 handles
        # the freeze-frame fallback if we end up before any coverage).
        earliest, _ = self._replay_store.available_session_time_range()
        if earliest is not None and target_session_time_ns < earliest:
            target_session_time_ns = earliest
        with self._lock:
            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.SEEKING)
            self.replay_state.transition_to(ReplayState.REPLAYING)
            self._playback_session_time_ns = target_session_time_ns
            self._playback_rate = 1.0
            self._state.current_playback_mode = PlaybackMode.REPLAY
            self._state.error_message = None
            self._start_replay_clock_locked(target_session_time_ns)
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        self._emit_state(f"Replaying Play #{current.play_number}")

    def jump_to_live(self) -> None:
        """Return the viewed output to the live edge."""
        if getattr(self, "_shutting_down", False):
            return
        with self._lock:
            self._stop_replay_clock_locked()
            if self.replay_state is not None and self.replay_state.state in ACTIVE_REPLAY_STATES:
                self.replay_state.transition_to(ReplayState.JUMPING_TO_LIVE)
                if self._recording_manager.recording_state.state == RecordingState.RECORDING:
                    self.replay_state.transition_to(ReplayState.LIVE_WHILE_RECORDING)
                else:
                    self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
            self._playback_session_time_ns = None
            self._playback_rate = 1.0
            self._state.current_playback_mode = (
                PlaybackMode.LIVE if self._state.source_connected else PlaybackMode.SOURCE_LOST
            )
            self._state.error_message = None
            # Phase 6: leaving replay clears all freeze badges.
            self._state.feeds_in_freeze_frame = ()
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

        From LIVE/SOURCE_LOST, the slow buttons snap into REPLAY at the
        latest replayable session-time **without** any rewind. The
        operator can then watch from the leading edge at the requested
        slow speed and start to fall behind live as playback progresses.
        Anchoring at `latest_replayable` rather than at "now" avoids
        landing in the in-progress segment (which has no replay
        coverage).
        """
        if getattr(self, "_shutting_down", False):
            return
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        with self._lock:
            assert self.replay_state is not None
            entering_from_live = (
                self._state.current_playback_mode
                in {PlaybackMode.LIVE, PlaybackMode.SOURCE_LOST}
                or self._playback_session_time_ns is None
            )
            # If we're entering replay from LIVE/SOURCE_LOST, snap the
            # playback position to the latest replayable session-time
            # so the operator starts at "now" (minus segment-finalize
            # lag) rather than at -10s.
            if entering_from_live:
                _, latest = self._replay_store.available_session_time_range()
                if latest is None:
                    self._state.error_message = "Replay frame is not available yet."
                    self._emit_state()
                    return
                self._playback_session_time_ns = latest
            self._playback_rate = max(0.0, playback_rate)
            # The replay state machine only allows direct entry to
            # PAUSED/REPLAYING from LIVE_WHILE_RECORDING (no direct
            # path to SLOW_MOTION). When entering from live, bounce
            # through SEEKING → REPLAYING first so the final transition
            # to SLOW_MOTION/PAUSED is valid. Matches the path
            # rewind_10_seconds takes.
            if entering_from_live and self.replay_state.state == ReplayState.LIVE_WHILE_RECORDING:
                self.replay_state.transition_to(ReplayState.SEEKING)
                self.replay_state.transition_to(ReplayState.REPLAYING)
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
                self._start_replay_clock_locked(self._playback_session_time_ns)
            self._state.error_message = None
            self._update_state_timestamps_locked()
            target_session_time = self._playback_session_time_ns
        # Render a frame at the snapped position outside the lock so
        # `_render_at_session_time_ns` can re-acquire it for state
        # updates without deadlocking.
        if target_session_time is not None:
            self._render_at_session_time_ns(target_session_time)
        self._emit_state(f"Replay speed {self._playback_rate:.2f}x")

    def step_frames(self, frame_delta: int) -> None:
        """Phase 12.A: jog the replay clock by `frame_delta` frames.

        Positive `frame_delta` advances forward; negative retreats.
        Frame duration is `frame_period_ns` (set at construction from
        `settings.target_fps`). Always lands in PAUSED — slow-motion is
        a separate gesture. Targets outside the replayable range clamp
        to the boundary and surface a "held at edge" status so the
        operator sees why a click did nothing.

        Entry behavior by current `ReplayState`:
          - LIVE_WHILE_RECORDING: snap to `latest_replayable` (matching
            `set_playback_rate`'s entering-from-live snap), bounce to
            PAUSED, then apply the delta.
          - REPLAYING / SLOW_MOTION / SEEKING: bounce to PAUSED, apply.
          - PAUSED: stay in PAUSED (same-state transition is a no-op),
            apply.
          - JUMPING_TO_LIVE / REPLAY_DEGRADED: rejected — those have no
            direct path to PAUSED in `_REPLAY_TRANSITIONS` and aren't
            states the operator should be jogging from.
        """
        if getattr(self, "_shutting_down", False):
            return
        if frame_delta == 0:
            return
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        clamped_at_live_edge = False
        clamped_at_start = False
        with self._lock:
            assert self.replay_state is not None
            current_replay = self.replay_state.state
            if current_replay in {
                ReplayState.JUMPING_TO_LIVE,
                ReplayState.REPLAY_DEGRADED,
            }:
                self._state.error_message = "Replay degraded; step unavailable."
                self._emit_state()
                return
            # Anchor: from LIVE_WHILE_RECORDING (or any state without an
            # operator playback position yet) snap to latest_replayable
            # so the first step from live lands at "now − frame_delta
            # frames". From any active replay state, anchor on the
            # operator's current position so repeated step clicks
            # accumulate.
            if (
                current_replay == ReplayState.LIVE_WHILE_RECORDING
                or self._playback_session_time_ns is None
            ):
                _, latest = self._replay_store.available_session_time_range()
                if latest is None:
                    self._state.error_message = "Replay frame is not available yet."
                    self._emit_state()
                    return
                anchor_session_time_ns = latest
            else:
                anchor_session_time_ns = self._playback_session_time_ns
            target_session_time_ns = (
                anchor_session_time_ns + frame_delta * self._frame_period_ns
            )
            earliest, latest = self._replay_store.available_session_time_range()
            if latest is not None and target_session_time_ns > latest:
                target_session_time_ns = latest
                clamped_at_live_edge = True
            if earliest is not None and target_session_time_ns < earliest:
                target_session_time_ns = earliest
                clamped_at_start = True
            # Bounce the FSM to PAUSED. LIVE_WHILE_RECORDING / SEEKING /
            # REPLAYING / SLOW_MOTION → PAUSED is allowed directly per
            # `_REPLAY_TRANSITIONS`; PAUSED → PAUSED is a no-op handled
            # by `StateMachine.transition_to`.
            self.replay_state.transition_to(ReplayState.PAUSED)
            self._playback_session_time_ns = target_session_time_ns
            self._playback_rate = 0.0
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._stop_replay_clock_locked()
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        if clamped_at_live_edge:
            self._emit_state("Step held at live edge")
        elif clamped_at_start:
            self._emit_state("Step held at start of recording")
        else:
            sign = "+" if frame_delta > 0 else "-"
            self._emit_state(f"Step {sign}{abs(frame_delta)} frames")

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
        """Re-read recording flags from the recording manager and notify listeners.

        Always rebuilds the playback-overlay snapshot before emitting
        state, regardless of which internal branch ran. This is
        defense-in-depth — without it, clicking Start Recording on a
        controller whose `_sync_replay_state_with_recording_locked`
        early-returns (live_only controllers, or recording-active
        path) leaves stale overlay text on screen until the next live
        frame fires.
        """
        with self._lock:
            self._refresh_recording_state_locked()
            self._update_state_timestamps_locked()
        self._emit_state()

    def get_display_frame(self) -> MediaFrame | None:
        """Return the frame the UI should currently display (primary feed)."""
        with self._lock:
            return self._latest_live_frame

    def shutdown(self) -> None:
        """Stop timers owned by this output session and close per-feed decoders.

        Sets `_shutting_down = True` first so any in-flight transport
        method that wakes up after teardown becomes a no-op (Phase
        10.F). Decoders and timers are released afterward.
        """
        self._shutting_down = True
        self._replay_timer.stop()
        self._overlay_timer.stop()
        for decoder in self._segment_decoders.values():
            decoder.close()

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
        """Pick a session-time to freeze on when the operator presses Pause.

        - In REPLAY mode, freeze at the current playback position.
        - In LIVE/SOURCE_LOST modes, freeze at the latest replayable
          session-time across all feeds so the operator sees something
          concrete (the freshest fully-finalized segment frame).
        """
        if (
            self._state.current_playback_mode == PlaybackMode.REPLAY
            and self._playback_session_time_ns is not None
        ):
            return self._playback_session_time_ns
        _, latest = self._replay_store.available_session_time_range()
        return latest

    def _resolve_rewind_target_locked(self, rewind_ns: int) -> int | None:
        """Compute a session-time target `rewind_ns` behind the right anchor.

        Slice 5.C: the anchor depends on the current playback mode.
        From REPLAY/PAUSED, anchor on the operator's current
        `_playback_session_time_ns` so repeated Rewind 10s clicks
        accumulate. From LIVE/SOURCE_LOST, anchor on the latest
        replayable session time across all feeds.

        Result is clamped to the earliest replayable session time so
        a long rewind from a short recording lands at the start of
        the timeline rather than going negative.
        """
        if (
            self._state.current_playback_mode in {PlaybackMode.REPLAY, PlaybackMode.PAUSED}
            and self._playback_session_time_ns is not None
        ):
            anchor = self._playback_session_time_ns
        else:
            _, latest = self._replay_store.available_session_time_range()
            if latest is None:
                return None
            anchor = latest
        earliest, _ = self._replay_store.available_session_time_range()
        target = anchor - rewind_ns
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
        - Always rebuild the playback-overlay snapshot at the end so
          the operator UI reflects the current state immediately —
          without this, the overlay pill keeps stale text from before
          the recording-state change until the next live frame fires.
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
        else:
            if current in ACTIVE_REPLAY_STATES:
                self.replay_state.transition_to(ReplayState.JUMPING_TO_LIVE)
                self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
                self._stop_replay_clock_locked()
                self._playback_session_time_ns = None
                self._playback_rate = 1.0
                self._state.current_playback_mode = (
                    PlaybackMode.LIVE
                    if self._state.source_connected
                    else PlaybackMode.SOURCE_LOST
                )
                self._state.feeds_in_freeze_frame = ()
            elif current == ReplayState.LIVE_WHILE_RECORDING:
                self.replay_state.transition_to(ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
        # Always rebuild the playback-overlay snapshot, regardless of
        # which branch ran (recording active or not). Without this,
        # clicking Start Recording while the overlay was last refreshed
        # in some pre-recording state leaves stale data on screen until
        # the next live-frame callback fires.
        self._update_state_timestamps_locked()

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
        self._replay_clock_anchor_session_time_ns = playback_pts_ns
        self._replay_clock_anchor_monotonic = time.monotonic()
        if self._playback_rate > 0.0 and not self._replay_timer.isActive():
            self._replay_timer.start()
        LOGGER.info("Replay clock started at pts=%dns", playback_pts_ns)

    def _stop_replay_clock_locked(self) -> None:
        self._replay_clock_anchor_session_time_ns = None
        self._replay_clock_anchor_monotonic = None
        if self._replay_timer.isActive():
            self._replay_timer.stop()
            LOGGER.info("Replay clock stopped")

    def _on_replay_timer_tick(self) -> None:
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                self._stop_replay_clock_locked()
                return
            anchor_session_time_ns = self._replay_clock_anchor_session_time_ns
            anchor_monotonic = self._replay_clock_anchor_monotonic
            rate = self._playback_rate
        if anchor_session_time_ns is None or anchor_monotonic is None:
            return
        elapsed_ns = max(0.0, time.monotonic() - anchor_monotonic) * rate * 1_000_000_000
        target_session_time_ns = anchor_session_time_ns + int(elapsed_ns)
        # Clamp to the cross-feed latest replayable so we don't run
        # past the live edge. This is the operator-visible upper bound.
        _, latest = self._replay_store.available_session_time_range()
        if latest is not None:
            target_session_time_ns = min(target_session_time_ns, latest)
        with self._lock:
            if self._state.current_playback_mode != PlaybackMode.REPLAY:
                return
            self._playback_session_time_ns = target_session_time_ns
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        self._emit_state()

    def _on_overlay_timer_tick(self) -> None:
        with self._lock:
            if self._state.current_playback_mode not in {PlaybackMode.PAUSED, PlaybackMode.SOURCE_LOST}:
                return
            self._update_state_timestamps_locked()
        self._emit_state()

    def _render_at_session_time_ns(self, target_session_time_ns: int) -> None:
        """Decode and display the nearest-available frame on EVERY feed (slice 5.C).

        Per §8.6.1, every operator tile renders something on every
        tick during replay. For each enabled feed:

        - Ask the replay store for `nearest_frame_location(t)` —
          returns an exact match when in coverage, the feed's earliest
          frame as a freeze when before any coverage, or the latest
          frame ending at-or-before `t` as a freeze when in a gap or
          after coverage.
        - Decode via the per-feed `SegmentDecoder` and push the frame
          into the renderer (which routes by `frame.feed_id`).

        Feeds that have no replayable segment yet (the `nearest_frame_location`
        returns `None`) keep showing whatever frame they last received.
        That's the only blank-tile state during replay — it only happens
        before any segment for that feed has finalized.

        No-op for `live_only` outputs.
        """
        if not self._segment_decoders:
            return
        recording_state = self._recording_manager.recording_state.state
        feeds_in_freeze: list[str] = []
        for runtime in self._feed_runtimes:
            decoder = self._segment_decoders.get(runtime.feed.feed_id)
            if decoder is None:
                continue
            location = self._replay_store.nearest_frame_location(
                feed_id=runtime.feed.feed_id,
                session_time_ns=target_session_time_ns,
                recording_state=recording_state,
            )
            if location is None:
                continue
            if location.is_freeze:
                feeds_in_freeze.append(runtime.feed.feed_id)
            frame = decoder.decode(location)
            if frame is None:
                continue
            self._output_renderer.show_frame(frame)
        # Phase 6: surface per-tile freeze state for the operator UI.
        with self._lock:
            self._state.feeds_in_freeze_frame = tuple(feeds_in_freeze)

    def _update_state_timestamps_locked(self) -> None:
        self._state.last_frame_timestamp = self._latest_live_timestamp
        # Slice 5.C: replay coverage is now the cross-feed session-time
        # bound (§8.6) — earliest start across all feeds, latest
        # complete end across all feeds. Operators reason in session
        # time so the operator UI bound is the union, not the primary
        # feed's slice.
        earliest_session_time_ns, latest_session_time_ns = (
            self._replay_store.available_session_time_range()
        )
        if (
            latest_session_time_ns is not None
            and earliest_session_time_ns is not None
            and latest_session_time_ns >= earliest_session_time_ns
        ):
            self._state.replay_buffer_span_seconds = (
                latest_session_time_ns - earliest_session_time_ns
            ) / 1_000_000_000.0
        else:
            self._state.replay_buffer_span_seconds = 0.0
        # Phase 7.B: surfaces for the status bar + diagnostics. Computed
        # alongside the existing `replay_buffer_span_seconds` so the
        # whole UiState replay block updates atomically. `replay_available`
        # requires both: (a) at least one segment finalized in the
        # current game (the store's per-game filter has already excluded
        # prior games' segments from `latest_session_time_ns`), and (b)
        # recording is active per §10.4. Without the recording-state
        # gate, the status bar would advertise stale ranges in the
        # interval between Stop and the next Start press.
        self._state.latest_replayable_session_time_ns = latest_session_time_ns
        is_recording = (
            self._recording_manager.recording_state.state
            == RecordingState.RECORDING
        )
        self._state.replay_available = (
            latest_session_time_ns is not None and is_recording
        )
        # Phase 7.H.3: pull the currently-open play number from the
        # PlayManager. None when no game is being recorded (PlayManager
        # has no open play between Stop and the next Start).
        self._state.current_play_number = (
            self._play_manager.current_play_number()
            if self._play_manager is not None
            else None
        )
        if latest_session_time_ns is None:
            self._state.live_lag_behind_replayable_seconds = 0.0
        elif self._session_clock is not None:
            self._state.live_lag_behind_replayable_seconds = max(
                0.0,
                (
                    self._session_clock.now_session_time_ns()
                    - latest_session_time_ns
                )
                / 1_000_000_000.0,
            )
        else:
            # No clock attached (test fixtures): fall back to 0 — there
            # is no monotonic "now" to compare against the latest
            # finalized segment.
            self._state.live_lag_behind_replayable_seconds = 0.0
        if (
            self._state.current_playback_mode == PlaybackMode.LIVE
            or self._playback_session_time_ns is None
        ):
            self._state.seconds_behind_live = 0.0
        elif self._session_clock is not None:
            # Smooth path: `now − playback` grows continuously with
            # real time. During pause it grows at 1s/s; during slow
            # motion at (1 − rate)/s; during REPLAY at 1.0x it stays
            # constant. Independent of segment finalization cadence.
            self._state.seconds_behind_live = max(
                0.0,
                (
                    self._session_clock.now_session_time_ns()
                    - self._playback_session_time_ns
                )
                / 1_000_000_000.0,
            )
        elif latest_session_time_ns is not None:
            # Fallback for fixtures / tooling without a SessionClock —
            # uses the latest finalized segment, which jumps in
            # segment-duration quanta but is better than 0.
            self._state.seconds_behind_live = max(
                0.0,
                (latest_session_time_ns - self._playback_session_time_ns)
                / 1_000_000_000.0,
            )
        else:
            self._state.seconds_behind_live = 0.0
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
                self._playback_session_time_ns / 1_000_000_000.0
                if self._playback_session_time_ns is not None
                else None
            ),
            wall_clock_timestamp=time.time(),
            seconds_behind_live=self._state.seconds_behind_live,
            playback_rate=self._playback_rate,
            status_text=self._build_overlay_status_locked(),
            is_recording=self._state.is_recording,
            current_play_number=self._state.current_play_number,
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
