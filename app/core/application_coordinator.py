"""Top-level runtime coordinator for feeds and output sessions."""

from __future__ import annotations

import logging

from dataclasses import dataclass
from pathlib import Path

from app.config.settings import AppSettings
from app.core.session_clock import SessionClock

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class _ResumeContinuation:
    """Phase 7.D: state captured on resume so the first Start press
    after resume continues the crashed game in the same folder.

    Set by `_setup_resume_continuation` when the operator picks
    Resume in the recovery dialog AND there is a viable crashed game
    to continue (most-recent `game_NNN/` with at least one segment
    carrying populated session-time fields). Consumed and cleared by
    the first Start path of `toggle_long_session_recording`.
    """

    game_subdir: str
    game_start_session_time_ns: int
from app.core.application_state import AppState, compute_app_state
from app.core.disk_budget import (
    BudgetVerdict,
    DiskBudgetAssessment,
    assess_disk_budget,
)
from app.core.feed_registry import FeedRegistry
from app.core.feed_state import make_feed_state_machine
from app.core.health_events import HealthSeverity, default_log as default_health_log
from app.core.play_manager import PlayManager
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.session_state import SessionState
from app.core.telemetry import TelemetryHub
from app.media.feed_runtime import FeedRuntime
from app.media.source_interface import PipelineMode
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import RecordingSegmentReplayStore
from app.media.output_renderer import MultiFeedOutputRenderer
from app.media.pipeline_manager import PipelineManager
from app.media.preview_output import PreviewOutput
from app.media.recording_manager import RecordingManager
from app.media.source_factory import build_source_for_feed
from app.storage.session_manager import SessionManager
from app.storage.session_recovery import (
    GAME_DIR_FORMAT,
    find_next_fragment_index,
    find_next_game_index,
    load_segment_index_for_session,
)


class ApplicationCoordinator:
    """Own the full application runtime graph."""

    def __init__(
        self,
        settings: AppSettings,
        session_manager: SessionManager,
        feed_registry: FeedRegistry,
        feed_runtimes: dict[str, FeedRuntime],
        recording_manager: RecordingManager,
        telemetry_hub: TelemetryHub,
        operator_renderer: MultiFeedOutputRenderer,
        program_renderer: MultiFeedOutputRenderer,
        segment_index: SegmentIndex | None = None,
        session_clock: SessionClock | None = None,
        disk_budget: DiskBudgetAssessment | None = None,
        play_manager: PlayManager | None = None,
    ) -> None:
        self._settings = settings
        self._session_manager = session_manager
        self.feed_registry = feed_registry
        self._feed_runtimes = feed_runtimes
        self._recording_manager = recording_manager
        self.telemetry_hub = telemetry_hub
        self.operator_renderer = operator_renderer
        self.program_renderer = program_renderer
        # Phase 7.A: stashed for `DiagnosticsWidget` and the health
        # event emitted from `initialize()`. None when the coordinator
        # was constructed without budget validation (older test paths).
        self.disk_budget = disk_budget
        # Slice 4.B: shared per-app in-memory segment index. Each feed's
        # PipelineManager writes finalized segments here as they close.
        self.segment_index = segment_index if segment_index is not None else SegmentIndex()
        # Slice 4.C: replay query layer over the segment index. Read-only
        # consumers (operator transport methods, future export tooling)
        # use this rather than touching the index directly so eligibility
        # checks (§10.4 / §6.6) stay in one place.
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)
        # Shared session clock — defaults to a fresh instance if the
        # caller didn't supply one (covers older test callers). The
        # builder below threads in the same instance the
        # PipelineManagers got, so the controller's `seconds_behind_live`
        # math is anchored to the same monotonic origin as the segment
        # rows it queries.
        self.session_clock = session_clock if session_clock is not None else SessionClock()
        # Phase 7.H.1: per-game play boundary tracker. None when the
        # coordinator was constructed without one (older test paths
        # that bypass `build_default_application_coordinator`). Must
        # be set BEFORE the PlaybackControllers below so they can
        # read it.
        self.play_manager = play_manager

        primary_feed = feed_registry.get_primary_feed()
        enabled_feeds = feed_registry.get_enabled_feeds()
        enabled_runtimes = [feed_runtimes[f.feed_id] for f in enabled_feeds]
        self.operator_controller = PlaybackController(
            feed_runtimes=enabled_runtimes,
            output_renderer=operator_renderer,
            recording_manager=recording_manager,
            replay_store=self.replay_store,
            default_source_name=primary_feed.display_name,
            session_role="operator",
            live_only=False,
            session_clock=self.session_clock,
            play_manager=self.play_manager,
        )
        self.program_controller = PlaybackController(
            feed_runtimes=enabled_runtimes,
            output_renderer=program_renderer,
            recording_manager=recording_manager,
            replay_store=self.replay_store,
            default_source_name=primary_feed.display_name,
            session_role="program",
            live_only=True,
            session_clock=self.session_clock,
            play_manager=self.play_manager,
        )
        self._session_started = False
        self._shutting_down = False
        # Phase 7.D: set on initialize() if the operator chose Resume
        # AND a viable crashed game exists; consumed by the first
        # toggle_long_session_recording start path.
        self._resume_continuation: _ResumeContinuation | None = None

    def get_feed_pipeline_mode(self, feed_id: str) -> PipelineMode:
        """Return the configured ingest pipeline mode for `feed_id`.

        Returns `PYTHON_PUSH` if the feed is not registered (safe default
        — unknown feeds shouldn't accidentally trigger native rendering).
        """
        runtime = self._feed_runtimes.get(feed_id)
        if runtime is None:
            return PipelineMode.PYTHON_PUSH
        return runtime.source.pipeline_mode

    def bind_native_preview_window_handle(
        self, role: str, feed_id: str, window_handle: int
    ) -> None:
        """Bind a Qt window handle to the per-feed native preview sink.

        `role` is `"operator"` or `"program"`. No-op when the feed's source
        runs in `python_push` mode (those sources render via the legacy
        QImage path and don't have native preview sinks to bind), or when
        the 3.A.3 `force_python_push_preview` escape hatch is on.
        """
        runtime = self._feed_runtimes.get(feed_id)
        if runtime is None:
            return
        if runtime.source.pipeline_mode != PipelineMode.NATIVE:
            return
        if self._settings.force_python_push_preview:
            return
        runtime.pipeline_manager.set_native_preview_window_handle(role, window_handle)

    def is_native_preview_active(self, feed_id: str) -> bool:
        """Return True when `feed_id` is using the d3d11 native-preview path."""
        runtime = self._feed_runtimes.get(feed_id)
        if runtime is None:
            return False
        if self._settings.force_python_push_preview:
            return False
        return runtime.source.pipeline_mode == PipelineMode.NATIVE

    def get_app_state(self) -> AppState:
        """Aggregate the four sub-state machines into the top-level `AppState`."""
        feed_states = [
            runtime.feed_state.state for runtime in self._feed_runtimes.values()
        ]
        replay_state = (
            self.operator_controller.replay_state.state
            if self.operator_controller.replay_state is not None
            else None
        )
        return compute_app_state(
            feed_states=feed_states,
            recording_state=self._recording_manager.recording_state.state,
            replay_state=replay_state,
            shutting_down=self._shutting_down,
        )

    def toggle_long_session_recording(self) -> None:
        """Start or stop game-length recording on every feed (operator control)."""
        session_paths = self._session_manager.get_active_session_paths()
        if session_paths is None:
            self.operator_controller.signals.status_message.emit("No active session; cannot record.")
            return
        recording_sm = self._recording_manager.recording_state
        session_sm = self._session_manager.get_active_session_state()
        if self._recording_manager.is_any_recording():
            recording_sm.transition_to(RecordingState.STOPPING_RECORDING)
            for runtime in self._feed_runtimes.values():
                runtime.pipeline_manager.disable_file_recording()
            recording_sm.transition_to(RecordingState.FINALIZING)
            recording_sm.transition_to(RecordingState.NOT_RECORDING)
            # Phase 7.H.1: close the currently-open play. Captured
            # AFTER recording stops so the play's end aligns with the
            # last frame the operator saw recorded.
            if self.play_manager is not None and self.session_clock is not None:
                self.play_manager.stop_game(
                    self.session_clock.now_session_time_ns()
                )
            # Phase 7.B-ext: clear the per-game replay scope so a future
            # Start re-captures it. (The recording-state gate already
            # idles replay queries during the gap, but clearing avoids
            # stale filter state.)
            self.replay_store.set_current_game_start_session_time(None)
            if session_sm is not None and session_sm.state == SessionState.RECORDING:
                session_sm.transition_to(SessionState.STOPPED)
            self.operator_controller.refresh_recording_state()
            self.program_controller.refresh_recording_state()
            self.operator_controller.signals.status_message.emit("Game recording stopped.")
            return
        recording_sm.transition_to(RecordingState.STARTING_RECORDING)
        # Phase 7.D: if the operator picked Resume on launch and this
        # is the first Start press of the run, continue the crashed
        # game in its existing folder rather than allocating a fresh
        # `game_NNN/`. The continuation also carries the per-game
        # filter value (the crashed game's earliest start_session_time)
        # so pre-crash segments stay visible for replay.
        continuation = self._resume_continuation
        if continuation is not None:
            game_subdir = continuation.game_subdir
            game_start_session_time_ns: int | None = (
                continuation.game_start_session_time_ns
            )
            self._resume_continuation = None
            LOGGER.info(
                "continuing crashed game %s (game_start_ns=%d)",
                game_subdir,
                game_start_session_time_ns,
            )
        else:
            # Allocate a fresh `game_NNN` subdir under <session>/recording/
            # so each press of "Start game recording" gets its own folder.
            # The index is one past the highest existing `game_NNN` on
            # disk — see `find_next_game_index`. All feeds share the
            # same subdir so a game's segments stay grouped across the
            # camera fan-out.
            game_subdir = GAME_DIR_FORMAT.format(
                find_next_game_index(session_paths.recording_dir)
            )
            game_start_session_time_ns = (
                self.session_clock.now_session_time_ns()
                if self.session_clock is not None
                else None
            )
        game_dir = session_paths.recording_dir / game_subdir
        for runtime in self._feed_runtimes.values():
            # Per-game folder layout means segment names can safely
            # restart at 0 each game — there's no cross-game collision
            # risk because each game has its own folder. Scope
            # `find_next_fragment_index` to the per-feed game folder so
            # a fresh game returns 0 (folder doesn't exist yet) and a
            # resumed game (folder exists with pre-crash files)
            # continues past whatever's already there.
            feed_game_dir = game_dir / runtime.feed.feed_id
            start_index = find_next_fragment_index(feed_game_dir)
            runtime.pipeline_manager.enable_file_recording(
                session_paths,
                feed_id=runtime.feed.feed_id,
                start_fragment_index=start_index,
                game_subdir=game_subdir,
            )
        # Phase 7.B-ext: scope replay queries to this game. For the
        # resume continuation case (Phase 7.D), this is the crashed
        # game's earliest start so pre-crash segments stay visible.
        if game_start_session_time_ns is not None:
            self.replay_store.set_current_game_start_session_time(
                game_start_session_time_ns
            )
        # Phase 7.H.1: open the next play of this game. For a fresh
        # game that's Play #1; for a Phase 7.D resume continuation
        # (where the crashed game's pre-existing plays were
        # auto-closed by `_setup_resume_continuation`) it's
        # `max(existing) + 1`. The play's start session_time is the
        # NOW reading (game_start_session_time_ns is the crashed
        # game's earliest in resume; for a fresh game both align).
        if (
            self.play_manager is not None
            and self.session_clock is not None
        ):
            play_start_ns = (
                self.session_clock.now_session_time_ns()
                if continuation is not None
                else game_start_session_time_ns
            )
            if play_start_ns is not None:
                self.play_manager.start_game(
                    session_id=session_paths.session_id,
                    game_subdir=game_subdir,
                    start_session_time_ns=play_start_ns,
                )
        recording_sm.transition_to(RecordingState.RECORDING)
        # Drive the session manifest forward on every Start press, not
        # just the first. After Stop, the session is in STOPPED; the
        # second game's Start needs to flip it back to RECORDING so
        # `session.json` accurately reflects "a game is being recorded
        # right now" — important for the dirty-detection rule on next
        # launch and for any external forensic tool reading the manifest.
        if session_sm is not None and session_sm.state in {
            SessionState.CREATED,
            SessionState.STOPPED,
        }:
            session_sm.transition_to(SessionState.RECORDING)
        self.operator_controller.refresh_recording_state()
        self.program_controller.refresh_recording_state()
        self.operator_controller.signals.status_message.emit("Game recording started.")

    def mark_next_play(self) -> None:
        """Close the currently-open play and open the next one (Phase 7.H.1).

        The eventual "Next Play" operator button is wired to this in
        Phase 7.H.2. Captures `session_clock.now_session_time_ns()` at
        the press moment so the play boundary aligns with what the
        operator just saw on screen.

        No-op when:
          - recording isn't active (no current play to advance)
          - the coordinator was constructed without a `PlayManager`
            (older test paths)
          - `session_clock` isn't attached
        """
        if not self._recording_manager.is_any_recording():
            self.operator_controller.signals.status_message.emit(
                "Start game recording before marking a play boundary."
            )
            return
        if self.play_manager is None or self.session_clock is None:
            return
        next_play = self.play_manager.mark_next_play(
            self.session_clock.now_session_time_ns()
        )
        if next_play is not None:
            self.operator_controller.signals.status_message.emit(
                f"Play #{next_play.play_number} started."
            )

    def initialize(self, *, resume_session_id: str | None = None) -> None:
        """Start storage, ingest, and playback sessions.

        Crash-recovery scan + §11.4 dirty-session prompt run *before*
        this method (in the application bootstrap) so the operator can
        resolve each dirty session before a new one is created. By the
        time `initialize` runs, all manifests on disk are in
        `finalized` / `created` / `archived` states.

        `resume_session_id`: when set, the coordinator adopts the named
        session instead of creating a new one. The §11.4 Resume action
        wires this through. The pre-crash `SegmentIndex` is rebuilt
        from SQLite so replay queries can address surviving segments,
        and each feed's `PipelineManager` is told to start its segment
        counter past the highest existing `segment_NNNNN.mkv` on disk
        so resumed recording doesn't collide.
        """
        if self._session_started:
            return
        if resume_session_id is not None:
            session_paths = self._session_manager.adopt_session(resume_session_id)
            self._populate_segment_index_from_resume(session_paths.session_id)
            # Phase 7.D: detect the crashed game and rebase the new
            # SessionClock past its latest session_time so the first
            # Start press can continue the crashed game in the same
            # folder, with pre-crash + post-resume segments treated
            # as one continuous game by the per-game filter.
            self._setup_resume_continuation(session_paths)
        else:
            session_paths = self._session_manager.start_new_session(
                self.feed_registry.build_session_label()
            )
        assert session_paths.logs_dir is not None  # set by SessionPaths.__post_init__
        default_health_log().open(
            session_paths.logs_dir / "health_events.jsonl",
            session_paths.session_id,
        )
        self._emit_disk_budget_health_event()
        for runtime in self._feed_runtimes.values():
            runtime.start(session_paths)
        self.operator_controller.initialize(session_paths.session_id)
        self.program_controller.initialize(session_paths.session_id)
        self.telemetry_hub.set_disk_path(self._settings.base_data_dir)
        self.telemetry_hub.start(_qt_periodic_registrar)
        self._session_started = True

    def _emit_disk_budget_health_event(self) -> None:
        """Surface a Phase 7.A disk-budget WARN / OVER verdict as a health event.

        The verdict was computed during `build_default_application_coordinator`
        (so the log line beats any feed startup noise); the JSONL log
        isn't open until `initialize()` has called `default_log().open(...)`,
        so the event itself is recorded here.
        """
        assessment = self.disk_budget
        if assessment is None or assessment.verdict is BudgetVerdict.OK:
            return
        category = (
            "disk_budget_over"
            if assessment.verdict is BudgetVerdict.OVER_BUDGET
            else "disk_budget_warn"
        )
        severity = (
            HealthSeverity.ERROR
            if assessment.verdict is BudgetVerdict.OVER_BUDGET
            else HealthSeverity.WARNING
        )
        default_health_log().record(
            severity=severity,
            category=category,
            message=(
                f"estimated recording write throughput {assessment.estimated_mb_s:.1f} MB/s "
                f"vs budget {assessment.budget_mb_s:.1f} MB/s "
                f"({assessment.feed_count} feed(s))"
            ),
            metadata={
                "estimated_mb_s": round(assessment.estimated_mb_s, 3),
                "budget_mb_s": round(assessment.budget_mb_s, 3),
                "feed_count": assessment.feed_count,
            },
        )

    # Small gap appended to the rebase anchor so post-resume session
    # times are STRICTLY greater than any pre-crash value. 1ms is more
    # than enough to dodge floating-point or coarse-clock equality.
    _RESUME_REBASE_GAP_NS = 1_000_000

    def _setup_resume_continuation(self, session_paths) -> None:
        """Phase 7.D: prepare the first Start after Resume to continue
        the crashed game.

        Walks the session's `recording/` directory for the highest
        `game_NNN/` folder (the crashed game), filters loaded
        segments to that folder, and computes:

        - earliest `start_session_time_ns` across the crashed game's
          segments — used as the per-game filter value when the
          operator presses Start, so pre-crash content stays visible
          to replay.
        - latest `end_session_time_ns` across the crashed game's
          segments — used to rebase the new `SessionClock` past it,
          so post-resume session_time is strictly greater than
          pre-crash and integer comparison stays meaningful.

        No-op (continuation stays None) if any of:
        - the recording dir doesn't exist or has no `game_NNN/` folder
        - no loaded segment lives under the crashed game folder
        - none of those segments have populated session-time fields
          (legacy / pre-5.A rows can't anchor the rebase)
        - no SessionClock is attached (test fixtures only)
        """
        if self.session_clock is None:
            return
        recording_root = session_paths.recording_dir
        if not recording_root.exists():
            return
        crashed_game_index = find_next_game_index(recording_root) - 1
        if crashed_game_index < 1:
            return
        crashed_game_subdir = GAME_DIR_FORMAT.format(crashed_game_index)
        crashed_game_segments = list(
            self._segments_under_game_subdir(crashed_game_subdir)
        )
        if not crashed_game_segments:
            return
        starts = [
            s.start_session_time_ns
            for s in crashed_game_segments
            if s.start_session_time_ns is not None
        ]
        ends = [
            s.end_session_time_ns
            for s in crashed_game_segments
            if s.end_session_time_ns is not None
        ]
        if not starts or not ends:
            return
        game_start_ns = min(starts)
        game_end_ns = max(ends)
        rebase_anchor_ns = game_end_ns + self._RESUME_REBASE_GAP_NS
        self.session_clock.rebase(rebase_anchor_ns)
        self._resume_continuation = _ResumeContinuation(
            game_subdir=crashed_game_subdir,
            game_start_session_time_ns=game_start_ns,
        )
        # Phase 7.H.1: auto-close any play whose end_session_time_ns
        # is NULL (the operator never marked the close because the
        # app crashed). Use the latest finalized segment's end as
        # the fallback. This must run BEFORE the next `start_game`
        # so the UNIQUE(session_id, game_subdir, play_number) check
        # has a clean view.
        if self.play_manager is not None:
            self.play_manager.auto_close_open_plays_for_session(
                session_id=session_paths.session_id,
                fallback_end_session_time_ns=game_end_ns,
            )
        LOGGER.info(
            "resume continuation: game=%s pre_crash_segments=%d game_start_ns=%d "
            "rebased_clock_to_ns=%d",
            crashed_game_subdir,
            len(crashed_game_segments),
            game_start_ns,
            rebase_anchor_ns,
        )

    def _segments_under_game_subdir(self, game_subdir: str):
        """Yield loaded segments whose file_path lies under `game_subdir`.

        Matches by path component to avoid the `game_001` / `game_0011`
        substring trap. Segments under the legacy flat layout
        (`recording/<feed_id>/...`) never match — pre-Phase-4.E
        sessions can't be resumed into the per-game continuation flow,
        which is correct because they have no game folder to continue.
        """
        for feed_id in self.segment_index.feed_ids():
            for segment in self.segment_index.all_for_feed(feed_id):
                if game_subdir in Path(segment.file_path).parts:
                    yield segment

    def _populate_segment_index_from_resume(self, session_id: str) -> None:
        """Seed the in-memory `SegmentIndex` from SQLite for an adopted session.

        Without this, replay queries against the resumed session would
        only see segments produced *after* resume — pre-crash segments
        would be invisible until they were re-added by some other path.
        Loads `complete` and `dirty` rows; quarantined / corrupt rows
        stay excluded.
        """
        db = self._session_manager.get_metadata_db()
        loaded = load_segment_index_for_session(db, session_id)
        for feed_id in loaded.feed_ids():
            for segment in loaded.all_for_feed(feed_id):
                self.segment_index.add(segment)

    def shutdown(self) -> None:
        """Stop playback sessions and feed runtimes."""
        self._shutting_down = True
        self.telemetry_hub.stop()
        self.operator_controller.shutdown()
        self.program_controller.shutdown()
        for runtime in self._feed_runtimes.values():
            runtime.stop()
        self._session_manager.close()
        default_health_log().close()


def build_default_application_coordinator(
    settings: AppSettings,
    session_manager: SessionManager,
    *,
    operator_renderer: MultiFeedOutputRenderer,
    program_renderer: MultiFeedOutputRenderer,
) -> ApplicationCoordinator:
    """Build the default coordinator graph for the current app."""
    feed_registry = FeedRegistry.build_default(settings)
    enabled_feeds = feed_registry.get_enabled_feeds()

    # Phase 7.A: estimate aggregate disk-write throughput against the
    # configured budget and log it before any feed starts. Health-event
    # emission is deferred until `initialize()` opens the JSONL log.
    disk_budget = assess_disk_budget(enabled_feeds, settings)
    _log_disk_budget_assessment(disk_budget)

    recording_manager = RecordingManager()
    telemetry_hub = TelemetryHub()
    segment_index = SegmentIndex()
    # Slice 5.A: one monotonic session clock for the whole app run. Each
    # PipelineManager uses it to stamp `pts_to_session_offset_ns` on
    # finalized segments so the replay layer can resolve session-time
    # to a per-feed `(segment, offset)` pair.
    session_clock = SessionClock()
    # Phase 7.H.1: per-game play boundary tracker. Owns the
    # currently-open play and persists boundary transitions to the
    # `plays` table.
    play_manager = PlayManager(session_manager.get_metadata_db())
    feed_runtimes: dict[str, FeedRuntime] = {}

    # Slice 3.B: tell the telemetry hub how to drive saturation-based
    # transitions before any feed registers a sampler.
    telemetry_hub.register_recording_state(recording_manager.recording_state)

    for feed in feed_registry.get_enabled_feeds():
        source = build_source_for_feed(settings, feed)
        preview_output = PreviewOutput()
        pipeline_manager = PipelineManager(
            source=source,
            preview_output=preview_output,
            audio_enabled=settings.enable_embedded_audio,
            live_audio_monitor_enabled=settings.live_audio_monitor_enabled,
            recording_enabled=settings.recording_enabled,
            recording_segment_duration_seconds=settings.recording_segment_duration_seconds,
            recording_codec=settings.recording_codec,
            recording_container=settings.recording_container,
            recording_audio_enabled=settings.recording_audio_enabled,
            audio_bitrate=settings.audio_bitrate,
            force_python_push_preview=settings.force_python_push_preview,
        )
        feed_metrics = telemetry_hub.register(feed.feed_id, feed.display_name)
        feed_metrics.set_pipeline_mode(source.pipeline_mode.value)
        pipeline_manager.set_feed_metrics(feed_metrics)
        feed_state = make_feed_state_machine(feed.feed_id, feed.display_name)
        pipeline_manager.set_feed_state(feed_state)
        telemetry_hub.register_feed_state(feed.feed_id, feed_state)
        # Slice 3.B: hub samples queue depths from this pipeline once
        # per log tick; the bound-method captures the pipeline_manager.
        telemetry_hub.register_queue_depth_sampler(
            feed.feed_id, pipeline_manager.sample_queue_depths
        )
        # Slice 3.C: visible startup log line per feed. Production-mode
        # warning is emitted by the diagnostics widget once the UI is
        # up; the log line below is enough to make the transitional
        # state visible from a tail of stdout/log file too.
        mode = source.pipeline_mode.value
        suffix = " (transitional)" if mode == "python_push" else " (clean)"
        LOGGER.info(
            "feed=%s pipeline=%s%s app_mode=%s",
            feed.feed_id,
            mode,
            suffix,
            settings.app_mode,
        )
        if settings.app_mode == "production" and mode == "python_push":
            LOGGER.warning(
                "feed=%s is using the transitional python_push pipeline "
                "while app_mode=production; preview path is GIL-bound. "
                "See Phase 3.A.3 in docs/r3_app_architecture.md.",
                feed.feed_id,
            )
        # Slice 4.B: each PipelineManager writes finalized segment rows
        # into the shared SQLite + in-memory index when its splitmuxsink
        # rotates files (or recording stops).
        pipeline_manager.set_metadata_db(session_manager.get_metadata_db())
        pipeline_manager.set_segment_index(segment_index)
        pipeline_manager.set_session_clock(session_clock)
        runtime = FeedRuntime(
            feed=feed,
            source=source,
            pipeline_manager=pipeline_manager,
            feed_state=feed_state,
        )
        feed_runtimes[feed.feed_id] = runtime

    return ApplicationCoordinator(
        settings=settings,
        session_manager=session_manager,
        feed_registry=feed_registry,
        feed_runtimes=feed_runtimes,
        recording_manager=recording_manager,
        telemetry_hub=telemetry_hub,
        operator_renderer=operator_renderer,
        program_renderer=program_renderer,
        segment_index=segment_index,
        session_clock=session_clock,
        disk_budget=disk_budget,
        play_manager=play_manager,
    )


def _log_disk_budget_assessment(assessment: DiskBudgetAssessment) -> None:
    """Emit one log line per Phase 7.A budget verdict.

    OK → INFO, WARN → WARNING, OVER_BUDGET → ERROR. Recording is still
    allowed under WARN/OVER (operator-overridable per §13); the
    diagnostics widget surfaces the verdict so it stays visible.
    """
    msg = (
        "disk budget: estimated %.1f MB/s vs budget %.1f MB/s "
        "across %d feed(s) — %s"
    )
    args = (
        assessment.estimated_mb_s,
        assessment.budget_mb_s,
        assessment.feed_count,
        assessment.verdict.value,
    )
    if assessment.verdict is BudgetVerdict.OVER_BUDGET:
        LOGGER.error(msg, *args)
    elif assessment.verdict is BudgetVerdict.WARN:
        LOGGER.warning(msg, *args)
    else:
        LOGGER.info(msg, *args)


def _qt_periodic_registrar(interval_seconds: float, callback) -> "callable":
    """`QTimer`-backed periodic registrar for `TelemetryHub.start()`."""
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setInterval(int(interval_seconds * 1000))
    timer.timeout.connect(callback)
    timer.start()

    def cancel() -> None:
        timer.stop()
        timer.timeout.disconnect(callback)

    return cancel
