"""Top-level runtime coordinator for feeds and output sessions."""

from __future__ import annotations

from app.config.settings import AppSettings
from app.core.feed_registry import FeedRegistry
from app.core.playback_controller import PlaybackController
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
        operator_renderer: MultiFeedOutputRenderer,
        program_renderer: MultiFeedOutputRenderer,
    ) -> None:
        self._settings = settings
        self._session_manager = session_manager
        self.feed_registry = feed_registry
        self._feed_runtimes = feed_runtimes
        self._recording_manager = recording_manager
        self._replay_store_manager = replay_store_manager
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

    def initialize(self) -> None:
        """Start storage, ingest, and playback sessions."""
        if self._session_started:
            return
        session_paths = self._session_manager.start_new_session(self.feed_registry.build_session_label())
        for runtime in self._feed_runtimes.values():
            runtime.start(session_paths)
        self.operator_controller.initialize(session_paths.session_id)
        self.program_controller.initialize(session_paths.session_id)
        self._session_started = True

    def shutdown(self) -> None:
        """Stop playback sessions and feed runtimes."""
        self.operator_controller.shutdown()
        self.program_controller.shutdown()
        for runtime in self._feed_runtimes.values():
            runtime.stop()
        self._recording_manager.stop_all()
        self._replay_store_manager.stop_all()
        self._session_manager.close()


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
    feed_runtimes: dict[str, FeedRuntime] = {}

    for feed in feed_registry.get_enabled_feeds():
        source = build_source_for_feed(settings, feed)
        recorder = Recorder(settings=settings)
        replay_store = ReplayBuffer(
            buffer_duration_seconds=settings.replay_buffer_seconds,
            jpeg_quality=settings.replay_buffer_jpeg_quality,
        )
        preview_output = PreviewOutput()
        pipeline_manager = PipelineManager(
            source=source,
            preview_output=preview_output,
            recorder=recorder,
            replay_buffer=replay_store,
        )
        runtime = FeedRuntime(
            feed=feed,
            source=source,
            pipeline_manager=pipeline_manager,
            recorder=recorder,
            replay_store=replay_store,
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
        operator_renderer=operator_renderer,
        program_renderer=program_renderer,
    )
