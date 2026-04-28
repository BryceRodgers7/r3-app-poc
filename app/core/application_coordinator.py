"""Top-level runtime coordinator for feeds and output sessions."""

from __future__ import annotations

from app.config.settings import AppSettings
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
    mark_dirty_sessions,
    validate_session_segments,
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
        for runtime in self._feed_runtimes.values():
            runtime.pipeline_manager.enable_file_recording(session_paths, feed_id=runtime.feed.feed_id)
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

    def initialize(self) -> None:
        """Start storage, ingest, and playback sessions."""
        if self._session_started:
            return
        # Slice 4.E: scan prior sessions for crash-recovery before
        # creating the new one. Marks unfinished sessions DIRTY (§10.6)
        # and validates/quarantines their segment files (§6.5). Runs
        # synchronously; the cost is bounded by the number of prior
        # sessions on disk and is dominated by `cv2.VideoCapture` open
        # latency.
        self._run_startup_recovery()
        session_paths = self._session_manager.start_new_session(self.feed_registry.build_session_label())
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

    def _run_startup_recovery(self) -> None:
        """Mark dirty sessions and validate their segments before starting up."""
        sessions_root = self._settings.sessions_root
        report = mark_dirty_sessions(sessions_root)
        if report.dirty_sessions_marked:
            # Validate segments for each newly-marked dirty session so
            # corrupt files end up in quarantine before the new session
            # adopts the disk.
            from app.storage.file_manager import FileManager
            file_manager = FileManager(self._settings)
            db = self._session_manager.get_metadata_db()
            for session_id in report.dirty_sessions_marked:
                session_paths = file_manager.session_paths_for_existing(session_id)
                validate_session_segments(session_paths, db)

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
        )
        feed_metrics = telemetry_hub.register(feed.feed_id, feed.display_name)
        feed_metrics.set_pipeline_mode(source.pipeline_mode.value)
        pipeline_manager.set_feed_metrics(feed_metrics)
        feed_state = make_feed_state_machine(feed.feed_id, feed.display_name)
        pipeline_manager.set_feed_state(feed_state)
        telemetry_hub.register_feed_state(feed.feed_id, feed_state)
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
