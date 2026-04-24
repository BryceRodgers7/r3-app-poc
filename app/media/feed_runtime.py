"""Per-feed ingest runtime and listener fan-out."""

from __future__ import annotations

from collections.abc import Callable

from app.core.models import FeedDefinition, FrameOverlayInfo, IngestTelemetry, MediaFrame, SessionPaths
from app.media.pipeline_manager import PipelineManager
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayStore
from app.media.source_interface import SourceInterface


class FeedRuntime:
    """Own the media services for one configured feed."""

    def __init__(
        self,
        feed: FeedDefinition,
        source: SourceInterface,
        pipeline_manager: PipelineManager,
        recorder: Recorder,
        replay_store: ReplayStore,
    ) -> None:
        self.feed = feed
        self.source = source
        self.pipeline_manager = pipeline_manager
        self.recorder = recorder
        self.replay_store = replay_store
        self._live_frame_listeners: list[Callable[[MediaFrame], None]] = []
        self._live_overlay_listeners: list[Callable[[FrameOverlayInfo], None]] = []
        self._latest_live_frame: MediaFrame | None = None
        self._latest_live_overlay = FrameOverlayInfo(feed_id=feed.feed_id, source_name=feed.display_name)
        self._started = False

    def start(self, session_paths: SessionPaths) -> bool:
        """Start the feed ingest pipeline and persistence services."""
        self.pipeline_manager.set_frame_callback(self._on_live_frame)
        self.pipeline_manager.set_live_sample_callback(self._on_live_overlay)
        connected = self.pipeline_manager.connect_source()
        self.pipeline_manager.start_replay_buffer(session_paths, feed_id=self.feed.feed_id)
        self.pipeline_manager.start_preview()
        self._started = True
        return connected

    def stop(self) -> None:
        """Stop the feed runtime."""
        self._started = False
        self.pipeline_manager.stop_all()

    def is_started(self) -> bool:
        """Return whether the runtime has been started."""
        return self._started

    def is_connected(self) -> bool:
        """Return whether the source is currently connected."""
        return self.pipeline_manager.is_source_connected()

    def get_source_name(self) -> str:
        """Return the current source display name."""
        return self.pipeline_manager.get_source_name()

    def get_status_message(self) -> str | None:
        """Return any current non-fatal source warning."""
        return self.pipeline_manager.get_source_status_message()

    def get_ingest_telemetry(self) -> IngestTelemetry | None:
        """Return negotiated capture vs target dimensions for the active source."""
        return self.pipeline_manager.get_ingest_telemetry()

    def get_latest_live_frame(self) -> MediaFrame | None:
        """Return the newest live frame, if any."""
        return self._latest_live_frame

    def get_latest_live_overlay(self) -> FrameOverlayInfo:
        """Return the newest live overlay metadata."""
        return self._latest_live_overlay

    def add_live_frame_listener(self, listener: Callable[[MediaFrame], None]) -> None:
        """Register a listener for live frames."""
        self._live_frame_listeners.append(listener)
        if self._latest_live_frame is not None:
            listener(self._latest_live_frame)

    def add_live_overlay_listener(self, listener: Callable[[FrameOverlayInfo], None]) -> None:
        """Register a listener for live overlay metadata."""
        self._live_overlay_listeners.append(listener)
        listener(self._latest_live_overlay)

    def _on_live_frame(self, frame: MediaFrame) -> None:
        self._latest_live_frame = frame
        for listener in list(self._live_frame_listeners):
            listener(frame)

    def _on_live_overlay(self, frame_overlay: FrameOverlayInfo) -> None:
        self._latest_live_overlay = frame_overlay
        for listener in list(self._live_overlay_listeners):
            listener(frame_overlay)
