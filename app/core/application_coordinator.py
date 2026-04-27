"""Top-level runtime coordinator for feeds and output sessions."""

from __future__ import annotations

from app.config.settings import AppSettings
from app.core.feed_registry import FeedRegistry
from app.core.feed_state import make_feed_state_machine
from app.core.health_events import default_log as default_health_log
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.session_state import SessionState
from app.core.telemetry import TelemetryHub
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import MultiFeedOutputRenderer
from app.media.pipeline_manager import PipelineManager
from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.recording_manager import RecordingManager
from app.media.replay_buffer import ReplayBuffer
from app.media.replay_store_manager import ReplayStoreManager
from app.media.source_factory import build_source_for_feed
from app.storage.session_manager import SessionManager


class ApplicationCoordinator:
    """Own the full application runtime graph."""

    def __init__(
        self,
        settings: AppSettings,
        session_manager: SessionManager,
        feed_registry: FeedRegistry,
        feed_runtimes: dict[str, FeedRuntime],
        recording_manager: RecordingManager,
        replay_store_manager: ReplayStoreManager,
        telemetry_hub: TelemetryHub,
        operator_renderer: MultiFeedOutputRenderer,
        program_renderer: MultiFeedOutputRenderer,
    ) -> None:
        self._settings = settings
        self._session_manager = session_manager
        self.feed_registry = feed_registry
        self._feed_runtimes = feed_runtimes
        self._recording_manager = recording_manager
        self._replay_store_manager = replay_store_manager
        self.telemetry_hub = telemetry_hub
        self.operator_renderer = operator_renderer
        self.program_renderer = program_renderer

        primary_feed = feed_registry.get_primary_feed()
        enabled_feeds = feed_registry.get_enabled_feeds()
        enabled_runtimes = [feed_runtimes[f.feed_id] for f in enabled_feeds]
        self.operator_controller = PlaybackController(
            feed_runtimes=enabled_runtimes,
            output_renderer=operator_renderer,
            recording_manager=recording_manager,
            replay_store_manager=replay_store_manager,
            default_source_name=primary_feed.display_name,
            session_role="operator",
            live_only=False,
        )
        self.program_controller = PlaybackController(
            feed_runtimes=enabled_runtimes,
            output_renderer=program_renderer,
            recording_manager=recording_manager,
            replay_store_manager=replay_store_manager,
            default_source_name=primary_feed.display_name,
            session_role="program",
            live_only=True,
        )
        self._session_started = False

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
        """Close the current rally clip and start the next on every feed."""
        if not self._recording_manager.is_any_recording():
            self.operator_controller.signals.status_message.emit("Start game recording before starting a clip.")
            return
        for runtime in self._feed_runtimes.values():
            runtime.recorder.advance_short_segment()
        self.operator_controller.signals.status_message.emit("Started next clip.")

    def initialize(self) -> None:
        """Start storage, ingest, and playback sessions."""
        if self._session_started:
            return
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

    def shutdown(self) -> None:
        """Stop playback sessions and feed runtimes."""
        self.telemetry_hub.stop()
        self.operator_controller.shutdown()
        self.program_controller.shutdown()
        for runtime in self._feed_runtimes.values():
            runtime.stop()
        self._recording_manager.stop_all()
        self._replay_store_manager.stop_all()
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
    replay_store_manager = ReplayStoreManager()
    telemetry_hub = TelemetryHub()
    feed_runtimes: dict[str, FeedRuntime] = {}

    for feed in feed_registry.get_enabled_feeds():
        source = build_source_for_feed(settings, feed)
        recorder = Recorder(settings=settings)
        replay_store = ReplayBuffer(
            buffer_duration_seconds=settings.replay_buffer_seconds,
            jpeg_quality=settings.replay_buffer_jpeg_quality,
            audio_segment_seconds=settings.replay_audio_segment_seconds,
            audio_bitrate=settings.audio_bitrate,
        )
        preview_output = PreviewOutput()
        pipeline_manager = PipelineManager(
            source=source,
            preview_output=preview_output,
            recorder=recorder,
            replay_buffer=replay_store,
            audio_enabled=settings.enable_embedded_audio,
            live_audio_monitor_enabled=settings.live_audio_monitor_enabled,
        )
        feed_metrics = telemetry_hub.register(feed.feed_id, feed.display_name)
        pipeline_manager.set_feed_metrics(feed_metrics)
        feed_state = make_feed_state_machine(feed.feed_id, feed.display_name)
        pipeline_manager.set_feed_state(feed_state)
        telemetry_hub.register_feed_state(feed.feed_id, feed_state)
        runtime = FeedRuntime(
            feed=feed,
            source=source,
            pipeline_manager=pipeline_manager,
            recorder=recorder,
            replay_store=replay_store,
            feed_state=feed_state,
        )
        feed_runtimes[feed.feed_id] = runtime
        recording_manager.register(feed.feed_id, recorder)
        replay_store_manager.register(feed.feed_id, replay_store)

    return ApplicationCoordinator(
        settings=settings,
        session_manager=session_manager,
        feed_registry=feed_registry,
        feed_runtimes=feed_runtimes,
        recording_manager=recording_manager,
        replay_store_manager=replay_store_manager,
        telemetry_hub=telemetry_hub,
        operator_renderer=operator_renderer,
        program_renderer=program_renderer,
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
