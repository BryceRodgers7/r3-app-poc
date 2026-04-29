"""Top-level runtime coordinator for feeds and output sessions."""

from __future__ import annotations

import logging

from app.config.settings import AppSettings

LOGGER = logging.getLogger(__name__)
from app.core.application_state import AppState, compute_app_state
from app.core.feed_registry import FeedRegistry
from app.core.feed_state import make_feed_state_machine
from app.core.health_events import default_log as default_health_log
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
    find_next_fragment_index,
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
    ) -> None:
        self._settings = settings
        self._session_manager = session_manager
        self.feed_registry = feed_registry
        self._feed_runtimes = feed_runtimes
        self._recording_manager = recording_manager
        self.telemetry_hub = telemetry_hub
        self.operator_renderer = operator_renderer
        self.program_renderer = program_renderer
        # Slice 4.B: shared per-app in-memory segment index. Each feed's
        # PipelineManager writes finalized segments here as they close.
        self.segment_index = segment_index if segment_index is not None else SegmentIndex()
        # Slice 4.C: replay query layer over the segment index. Read-only
        # consumers (operator transport methods, future export tooling)
        # use this rather than touching the index directly so eligibility
        # checks (§10.4 / §6.6) stay in one place.
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)

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
        )
        self.program_controller = PlaybackController(
            feed_runtimes=enabled_runtimes,
            output_renderer=program_renderer,
            recording_manager=recording_manager,
            replay_store=self.replay_store,
            default_source_name=primary_feed.display_name,
            session_role="program",
            live_only=True,
        )
        self._session_started = False
        self._shutting_down = False

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
        QImage path and don't have native preview sinks to bind).
        """
        runtime = self._feed_runtimes.get(feed_id)
        if runtime is None:
            return
        if runtime.source.pipeline_mode != PipelineMode.NATIVE:
            return
        runtime.pipeline_manager.set_native_preview_window_handle(role, window_handle)

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
            if session_sm is not None and session_sm.state == SessionState.RECORDING:
                session_sm.transition_to(SessionState.STOPPED)
            self.operator_controller.refresh_recording_state()
            self.program_controller.refresh_recording_state()
            self.operator_controller.signals.status_message.emit("Game recording stopped.")
            return
        recording_sm.transition_to(RecordingState.STARTING_RECORDING)
        db = self._session_manager.get_metadata_db()
        for runtime in self._feed_runtimes.values():
            # Always seed past the high-water mark. For a new session
            # this starts at 0; for a resumed session it starts past
            # pre-crash files (and any quarantined-but-DB-present rows
            # whose fragment_index would otherwise collide); for any
            # Stop/Start cycle, it starts past whatever the previous
            # cycle wrote, so segment files never collide.
            feed_paths = session_paths.get_feed_paths(runtime.feed.feed_id)
            start_index = find_next_fragment_index(
                feed_paths.recording_dir,
                db=db,
                session_id=session_paths.session_id,
                feed_id=runtime.feed.feed_id,
            )
            runtime.pipeline_manager.enable_file_recording(
                session_paths,
                feed_id=runtime.feed.feed_id,
                start_fragment_index=start_index,
            )
        recording_sm.transition_to(RecordingState.RECORDING)
        if session_sm is not None and session_sm.state == SessionState.CREATED:
            session_sm.transition_to(SessionState.RECORDING)
        self.operator_controller.refresh_recording_state()
        self.program_controller.refresh_recording_state()
        self.operator_controller.signals.status_message.emit("Game recording started.")

    def advance_short_segments(self) -> None:
        """Close the current rally clip and start the next on every feed.

        Slice 4.D: short-clip "advance segment" was driven by the legacy
        `Recorder.advance_short_segment()` writer-rotation. Recording now
        rotates segments inside `splitmuxsink` on a fixed cadence
        (§6.5 — `recording_segment_duration_seconds`); manual advancement
        will be re-introduced via splitmuxsink's `split-now` action signal
        in a later slice. For now this method only emits status.
        """
        if not self._recording_manager.is_any_recording():
            self.operator_controller.signals.status_message.emit("Start game recording before starting a clip.")
            return
        # TODO(post-4.D): emit `splitmuxsink.split-now` per-feed when
        # short-clip boundaries are reattached to the operator UI.
        self.operator_controller.signals.status_message.emit(
            "Short-clip advance is paused while §6.5 segment cadence is splitmuxsink-driven."
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
        else:
            session_paths = self._session_manager.start_new_session(
                self.feed_registry.build_session_label()
            )
        default_health_log().open(
            session_paths.root_dir / "logs" / "health_events.jsonl",
            session_paths.session_id,
        )
        for runtime in self._feed_runtimes.values():
            runtime.start(session_paths)
        self.operator_controller.initialize(session_paths.session_id)
        self.program_controller.initialize(session_paths.session_id)
        self.telemetry_hub.set_disk_path(self._settings.base_data_dir)
        self.telemetry_hub.start(_qt_periodic_registrar)
        self._session_started = True

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
    recording_manager = RecordingManager()
    telemetry_hub = TelemetryHub()
    segment_index = SegmentIndex()
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
    )


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
