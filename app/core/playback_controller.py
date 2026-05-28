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
    from app.core.clip_manager import ClipManager

LOGGER = logging.getLogger(__name__)

# Phase 14.C default — referee window's Rewind button duration in
# seconds. Pre-14.C this was hardcoded at 10s; the spec ships at 5s.
# Coordinator overrides via `settings.replay_rewind_seconds`; tests
# and any caller that doesn't configure it fall back to this default.
_DEFAULT_REWIND_SECONDS = 5

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
        clip_manager: "ClipManager | None" = None,
        frame_period_ns: int = _DEFAULT_FRAME_PERIOD_NS,
        rewind_seconds: int = _DEFAULT_REWIND_SECONDS,
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
        # Phase 14.A: read-only access to the ClipManager so
        # `_update_state_timestamps_locked` can populate the
        # current play number on UiState + PlaybackOverlayInfo.
        self._clip_manager = clip_manager
        self._default_source_name = default_source_name
        self._session_role = session_role
        self._live_only = live_only
        # Phase 12.A: replay clock delta per step button press. Set once
        # at construction (the coordinator computes from
        # `settings.target_fps`); `step_frames` multiplies by the
        # operator's `frame_delta`.
        self._frame_period_ns = max(1, int(frame_period_ns))
        # Phase 14.C: rewind-button duration in session-time-ns. Set
        # once at construction from `settings.replay_rewind_seconds`
        # (see `application_coordinator`). Tests / older callers fall
        # back to the module default.
        self._rewind_ns = max(1, int(rewind_seconds)) * 1_000_000_000
        # Phase 14.D: challenge-lockout fence. When set, all replay
        # primitives clamp their target session-time to this range
        # and force PAUSED on out-of-bounds. `end` may be None when
        # the play just closed but `end_session_time_ns` hasn't been
        # finalized yet — the helper treats None as "use the segment
        # store's `latest` as the upper edge", which absorbs the brief
        # window before the close-clip write lands.
        self._clip_bounds: tuple[int, int | None] | None = None
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
        # replay tick. Live-only outputs (operator window) skip the
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

    def rewind_configured_seconds(self) -> None:
        """Rewind by `settings.replay_rewind_seconds` in session-time.

        Phase 14.C rename from `rewind_10_seconds`. The duration is
        config-driven (`[replay] rewind_seconds`, default 5s) so
        operators can retune without code changes.

        From LIVE/SOURCE_LOST: anchor on `latest_replayable_session_time`
        across all feeds. The first click from live always lands at
        `now − Ns`.

        From REPLAY/PAUSED: anchor on the operator's current
        `_playback_session_time_ns`. Repeated clicks accumulate —
        click twice for `−2Ns` from live, three times for `−3Ns`, etc.
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
            target_session_time_ns = self._resolve_rewind_target_locked(self._rewind_ns)
            if target_session_time_ns is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return
            assert self.replay_state is not None
            # Phase 14.D: if the rewind crosses the challenge fence,
            # clamp to the fence edge and bounce to PAUSED instead of
            # REPLAYING.
            (
                target_session_time_ns,
                fence_low,
                fence_high,
            ) = self._apply_clip_bounds_locked(target_session_time_ns)
            self.replay_state.transition_to(ReplayState.SEEKING)
            if fence_low or fence_high:
                self.replay_state.transition_to(ReplayState.PAUSED)
                self._stop_replay_clock_locked()
                self._playback_session_time_ns = target_session_time_ns
                self._playback_rate = 0.0
                self._state.current_playback_mode = PlaybackMode.PAUSED
            else:
                self.replay_state.transition_to(ReplayState.REPLAYING)
                self._playback_session_time_ns = target_session_time_ns
                self._playback_rate = 1.0
                self._state.current_playback_mode = PlaybackMode.REPLAY
                self._start_replay_clock_locked(target_session_time_ns)
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        if fence_low:
            self._emit_state("Held at start of play (challenge)")
        elif fence_high:
            self._emit_state("Held at end of play (challenge)")
        else:
            self._emit_state(f"Replay -{self._state.seconds_behind_live:.0f}s")

    def seek_to_session_time(self, target_session_time_ns: int) -> None:
        """Phase 14.C: seek the replay clock to an absolute session-time.

        Phase 14.F: no longer driven by any UI surface (the Phase 14.C
        scrubber slider was removed; the new jog wheel uses `step_frames`
        instead). The primitive lives on because Phase 14.D's challenge-
        lockout tests cover its clip-bounds clamping, and a future
        absolute-time seek API can route through here. Clamps the target
        against the replayable range (same rule as
        `_resolve_rewind_target_locked`), bounces through SEEKING and
        lands in PAUSED. The operator can then press Play to resume.

        Same gating as the other replay primitives — live-only outputs
        and pre-recording state both no-op with a status message.
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
            earliest, latest = self._replay_store.available_session_time_range()
            if earliest is None or latest is None:
                self._state.error_message = "Replay frame is not available yet."
                self._emit_state()
                return
            clamped = max(earliest, min(latest, int(target_session_time_ns)))
            # Phase 14.D: fence clamp follows range clamp. seek_to_session_time
            # already lands in PAUSED, so the fence-clamp doesn't change
            # the FSM target — but the status message differentiates a
            # plain scrub from one that hit the fence.
            clamped, fence_low, fence_high = self._apply_clip_bounds_locked(clamped)
            assert self.replay_state is not None
            self.replay_state.transition_to(ReplayState.SEEKING)
            self.replay_state.transition_to(ReplayState.PAUSED)
            self._stop_replay_clock_locked()
            self._playback_session_time_ns = clamped
            self._playback_rate = 0.0
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(clamped)
        if fence_low:
            self._emit_state("Held at start of play (challenge)")
        elif fence_high:
            self._emit_state("Held at end of play (challenge)")
        else:
            self._emit_state("Scrubbed")

    def set_clip_bounds(
        self,
        start_session_time_ns: int,
        end_session_time_ns: int | None,
    ) -> None:
        """Phase 14.D: install a replay fence (challenge lockout).

        While bounds are installed, every replay primitive clamps its
        target session-time to `[start, end]` and forces PAUSED on
        out-of-bounds. `end` may be None initially — the close-clip
        write hasn't landed yet — in which case the upper edge falls
        back to the segment store's `latest` until a later
        `set_clip_bounds` updates it.

        Idempotent; replaces any existing fence.
        """
        with self._lock:
            self._clip_bounds = (
                int(start_session_time_ns),
                None if end_session_time_ns is None else int(end_session_time_ns),
            )

    def clear_clip_bounds(self) -> None:
        """Phase 14.D: remove the replay fence; full range available again."""
        with self._lock:
            self._clip_bounds = None

    def replay_current_play(
        self,
        start_session_time_ns: int,
        end_session_time_ns: int | None,
    ) -> None:
        """Phase 14.D challenge-open hook (repurposed from Phase 7.H.4).

        Snap the playback clock to `start_session_time_ns`, install
        bounds `(start, end)`, bounce to PAUSED, and render. The
        coordinator calls this on the referee controller with the
        bounds of the just-closed play clip when the operator presses
        Challenge.

        Pre-14.D this primitive took no args and resumed at 1.0× from
        the currently-open play's start. The new contract:
          - explicit bounds (caller already looked up the play)
          - lands in PAUSED so the referee chooses when to resume
          - installs the fence the rest of the controller respects

        No-op when:
          - this is the live-only program output
          - replay isn't available (recording not active)
        """
        if getattr(self, "_shutting_down", False):
            return
        if self._live_only:
            self._emit_state("This output is locked to live.")
            return
        if not self._replay_actions_allowed():
            self._emit_state("Replay unavailable: start game recording first.")
            return
        # Defensive clamp to the per-game replay scope's earliest, in
        # case the play marker drifted slightly before the first
        # finalized segment of the game (Phase 5.C / §8.6.1 handles
        # the freeze-frame fallback if we end up before any coverage).
        earliest, _ = self._replay_store.available_session_time_range()
        target_session_time_ns = int(start_session_time_ns)
        if earliest is not None and target_session_time_ns < earliest:
            target_session_time_ns = earliest
        with self._lock:
            assert self.replay_state is not None
            self._clip_bounds = (
                int(start_session_time_ns),
                None if end_session_time_ns is None else int(end_session_time_ns),
            )
            self.replay_state.transition_to(ReplayState.SEEKING)
            self.replay_state.transition_to(ReplayState.PAUSED)
            self._stop_replay_clock_locked()
            self._playback_session_time_ns = target_session_time_ns
            self._playback_rate = 0.0
            self._state.current_playback_mode = PlaybackMode.PAUSED
            self._state.error_message = None
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        self._emit_state("Reviewing play (challenge)")

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
                # Phase 14.D: a challenge fence overrides the live-edge
                # snap. During a lockout, "Play from live" doesn't make
                # sense — start at the fence's lower edge so the
                # referee watches the play from its beginning.
                if self._clip_bounds is not None:
                    self._playback_session_time_ns = self._clip_bounds[0]
                else:
                    self._playback_session_time_ns = latest
            # Phase 14.D: also fence-clamp the current playback
            # position. If the operator's last replay position is
            # outside the fence (only possible if bounds were just
            # installed), snap back inside before applying the rate.
            (
                clamped_target,
                fence_low,
                fence_high,
            ) = self._apply_clip_bounds_locked(
                self._playback_session_time_ns or 0
            )
            if (fence_low or fence_high) and self._playback_session_time_ns is not None:
                self._playback_session_time_ns = clamped_target
            self._playback_rate = max(0.0, playback_rate)
            # The replay state machine only allows direct entry to
            # PAUSED/REPLAYING from LIVE_WHILE_RECORDING (no direct
            # path to SLOW_MOTION). When entering from live, bounce
            # through SEEKING → REPLAYING first so the final transition
            # to SLOW_MOTION/PAUSED is valid. Matches the path
            # rewind_configured_seconds takes.
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
            # Phase 14.D: fence-clamp after the range clamp. Step
            # already lands in PAUSED, so the only change is the
            # status string differentiating a fence hit from a
            # boundary hit.
            (
                target_session_time_ns,
                fence_low,
                fence_high,
            ) = self._apply_clip_bounds_locked(target_session_time_ns)
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
        if fence_low:
            self._emit_state("Held at start of play (challenge)")
        elif fence_high:
            self._emit_state("Held at end of play (challenge)")
        elif clamped_at_live_edge:
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

    def available_session_time_range(self) -> tuple[int | None, int | None]:
        """Pass through the replay store's available range.

        Originally added for the Phase 14.C scrubber slider; the slider
        is gone (Phase 14.F) but the accessor is still useful for any
        caller that needs the replayable bounds without reaching
        through `controller._replay_store`.
        """
        return self._replay_store.available_session_time_range()

    def get_playback_session_time_ns(self) -> int | None:
        """Current replay clock position in session-time.

        None when on the live edge (`_playback_session_time_ns` is None
        outside REPLAY/PAUSED). Originally added for the Phase 14.C
        scrubber handle; the slider is gone (Phase 14.F) but the
        accessor stays as a read-only window into the replay clock for
        tests and any future caller.
        """
        with self._lock:
            return self._playback_session_time_ns

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

    def _apply_clip_bounds_locked(
        self, target_session_time_ns: int
    ) -> tuple[int, bool, bool]:
        """Phase 14.D: clamp `target` to the active fence, if any.

        Returns `(clamped_target_ns, was_clamped_low, was_clamped_high)`.
        Callers use the `was_clamped_*` flags to decide whether to force
        PAUSED and emit a "held at" status. When no fence is installed
        the input is returned unchanged with both flags False.

        `_clip_bounds.end` is None during the brief window after Next
        Play opens a clip but before its close-clip write lands; in
        that case we fall back to the segment store's `latest` so the
        fence still bounds the upper edge.
        """
        if self._clip_bounds is None:
            return target_session_time_ns, False, False
        start_ns, end_ns = self._clip_bounds
        if end_ns is None:
            _, latest = self._replay_store.available_session_time_range()
            end_ns = latest if latest is not None else start_ns
        clamped_low = target_session_time_ns < start_ns
        clamped_high = target_session_time_ns > end_ns
        if clamped_low:
            return start_ns, True, False
        if clamped_high:
            return end_ns, False, True
        return target_session_time_ns, False, False

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
        the timeline rather than going negative. Phase 14.D adds the
        challenge-fence clamp at the end so a Rewind that would cross
        the play boundary lands at the boundary instead.
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
            # Phase 14.D: if the natural-rate advance crossed the
            # challenge fence's upper edge, snap to the edge and
            # PAUSE. The lower edge isn't reachable from a forward
            # advance — we entered the fence inside its range and
            # natural playback moves forward.
            (
                target_session_time_ns,
                _fence_low,
                fence_high,
            ) = self._apply_clip_bounds_locked(target_session_time_ns)
            self._playback_session_time_ns = target_session_time_ns
            self._state.error_message = None
            if fence_high and self.replay_state is not None:
                self.replay_state.transition_to(ReplayState.PAUSED)
                self._playback_rate = 0.0
                self._state.current_playback_mode = PlaybackMode.PAUSED
                self._stop_replay_clock_locked()
            self._update_state_timestamps_locked()
        self._render_at_session_time_ns(target_session_time_ns)
        if fence_high:
            self._emit_state("Held at end of play (challenge)")
        else:
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
        # Phase 14.A: pull the most-recent play number from the
        # ClipManager. None when no game is being recorded, OR
        # during a pre-game clip before the first Next Play press.
        # During timeout / challenge clips this returns the last
        # opened play's number so the overlay still shows "Play N".
        if self._clip_manager is not None:
            self._state.current_play_number = (
                self._clip_manager.current_play_number()
            )
            # Phase 14.B: clip number + type drive the operator
            # counter overlay and the operator-controls gating.
            current_clip = self._clip_manager.current_clip()
            self._state.current_clip_number = (
                current_clip.clip_number if current_clip is not None else None
            )
            self._state.current_clip_type = (
                current_clip.type if current_clip is not None else None
            )
        else:
            self._state.current_play_number = None
            self._state.current_clip_number = None
            self._state.current_clip_type = None
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
