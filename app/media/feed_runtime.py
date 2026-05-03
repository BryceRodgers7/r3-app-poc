"""Per-feed ingest runtime and listener fan-out."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading

from app.core.feed_state import FeedState, make_feed_state_machine
from app.core.models import FeedDefinition, FrameOverlayInfo, IngestTelemetry, MediaFrame, SessionPaths
from app.core.reconnect_supervisor import ReconnectSupervisor, Scheduler
from app.core.state_machine import StateMachine
from app.media.pipeline_manager import PipelineManager
from app.media.source_interface import SourceInterface

LOGGER = logging.getLogger(__name__)


class FeedRuntime:
    """Own the media services for one configured feed."""

    def __init__(
        self,
        feed: FeedDefinition,
        source: SourceInterface,
        pipeline_manager: PipelineManager,
        feed_state: StateMachine[FeedState] | None = None,
        *,
        reconnect_scheduler: Scheduler | None = None,
    ) -> None:
        self.feed = feed
        self.source = source
        self.pipeline_manager = pipeline_manager
        self.feed_state = feed_state if feed_state is not None else make_feed_state_machine(
            feed.feed_id, feed.display_name
        )
        self._live_frame_listeners: list[Callable[[MediaFrame], None]] = []
        self._live_overlay_listeners: list[Callable[[FrameOverlayInfo], None]] = []
        self._latest_live_frame: MediaFrame | None = None
        self._latest_live_overlay = FrameOverlayInfo(feed_id=feed.feed_id, source_name=feed.display_name)
        self._started = False
        self._reconnect_scheduler = reconnect_scheduler
        self._reconnect_supervisor: ReconnectSupervisor | None = None

    def start(self, session_paths: SessionPaths) -> bool:
        """Start the feed ingest pipeline and persistence services."""
        self.pipeline_manager.set_frame_callback(self._on_live_frame)
        self.pipeline_manager.set_live_sample_callback(self._on_live_overlay)
        # Phase 10.B: install the reconnect supervisor before any state
        # transitions so that an immediate DISCONNECTED (e.g. source
        # factory failure on the first connect_source call) triggers
        # the backoff schedule.
        self._reconnect_supervisor = ReconnectSupervisor(
            feed_id=self.feed.feed_id,
            display_name=self.feed.display_name,
            feed_state=self.feed_state,
            rebuild_callable=self.rebuild,
            scheduler=self._reconnect_scheduler,
        )
        self._reconnect_supervisor.attach()
        self.feed_state.transition_to(FeedState.CONNECTING)
        connected = self.pipeline_manager.connect_source()
        self.pipeline_manager.start_preview()
        self._started = True
        if not connected:
            # Source factory already failed. Mark disconnected; LIVE will be
            # entered later when first frame arrives via on_live_frame.
            self.feed_state.transition_to(FeedState.DISCONNECTED)
        return connected

    def stop(self) -> None:
        """Stop the feed runtime."""
        self._started = False
        if self._reconnect_supervisor is not None:
            # Detach before driving DISABLED so the supervisor doesn't
            # observe a transient transition during teardown.
            self._reconnect_supervisor.shutdown()
            self._reconnect_supervisor = None
        self.feed_state.transition_to(FeedState.DISABLED)
        self.pipeline_manager.stop_all()

    def rebuild(self) -> bool:
        """Tear down and rebuild the source-side ingest pipeline (Phase 10.B).

        Called by `ReconnectSupervisor` on each scheduled attempt.
        Returns whether `connect_source` succeeded — the actual proof
        of recovery is a buffer arriving downstream, which drives
        `RECONNECTING → LIVE` via `_promote_feed_state_on_arrival` on
        the bus / appsrc thread.
        """
        LOGGER.info("rebuild feed=%s starting", self.feed.feed_id)
        try:
            self.pipeline_manager.stop_all()
        except Exception:
            LOGGER.exception(
                "rebuild feed=%s stop_all raised; continuing", self.feed.feed_id
            )
        connected = self.pipeline_manager.connect_source()
        if not connected:
            LOGGER.warning(
                "rebuild feed=%s connect_source returned False", self.feed.feed_id
            )
            return False
        self.pipeline_manager.start_preview()
        return True

    def request_encoder_software_fallback(self) -> None:
        """Phase 11.A: rebuild the pipeline pinned to software encoding.

        Invoked by `PipelineManager` from the bus-error path when the
        recording-branch encoder fails caps negotiation. The
        `force_software_encoder()` flag is already set by the caller;
        we just need to schedule the rebuild on a thread that *isn't*
        the bus thread (the rebuild tears the bus down).

        Spec (architecture §11.A): does NOT route through
        `FeedState.DISCONNECTED` because the source is healthy — the
        ReconnectSupervisor's source-side backoff would be the wrong
        recovery posture for an encoder-side fault.
        """
        LOGGER.info(
            "encoder fallback rebuild scheduled for feed=%s", self.feed.feed_id
        )
        threading.Thread(
            target=self.rebuild,
            name=f"encoder-fallback-{self.feed.feed_id}",
            daemon=True,
        ).start()

    def is_started(self) -> bool:
        """Return whether the runtime has been started."""
        return self._started

    def is_connected(self) -> bool:
        """Return whether the source is currently delivering frames."""
        return self.feed_state.state in {FeedState.LIVE, FeedState.DEGRADED}

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

    def _promote_feed_state_on_arrival(self) -> None:
        """Run the CONNECTING/RECONNECTING/etc → LIVE transition once data flows.

        Called from both `_on_live_frame` (python_push sources) and
        `_on_live_overlay` (native sources, which never deliver pixel data
        into Python). This keeps `FeedState` accurate regardless of which
        callback path the source uses.
        """
        if self.feed_state.state in {
            FeedState.CONNECTING,
            FeedState.RECONNECTING,
            FeedState.DEGRADED,
            FeedState.DISCONNECTED,
        }:
            if self.feed_state.state == FeedState.DISCONNECTED:
                # DISCONNECTED must hop through RECONNECTING per §10.2.
                self.feed_state.transition_to(FeedState.RECONNECTING)
            self.feed_state.transition_to(FeedState.LIVE)

    def _on_live_frame(self, frame: MediaFrame) -> None:
        self._promote_feed_state_on_arrival()
        self._latest_live_frame = frame
        for listener in list(self._live_frame_listeners):
            listener(frame)

    def _on_live_overlay(self, frame_overlay: FrameOverlayInfo) -> None:
        self._promote_feed_state_on_arrival()
        self._latest_live_overlay = frame_overlay
        for listener in list(self._live_overlay_listeners):
            listener(frame_overlay)
