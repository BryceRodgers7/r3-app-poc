"""Abstract source contracts for live video ingest."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from app.core.models import AudioChunk, AudioFormat, IngestTelemetry, MediaFrame


class PipelineMode(str, Enum):
    """How a source delivers frames into the per-feed graph.

    - `python_push`: the source pulls frames into Python (numpy BGR via
      `read_frame()`) and `PipelineManager` pushes them into an `appsrc`
      head. The synthetic dev source is the only intended user after
      Phase 3.A.2.
    - `native`: the source exposes a configured `Gst.Bin` whose src pad
      links directly into the per-feed `tee`. Frames never enter Python on
      the hot path. Production NDI ingest will use this mode after 3.A.2.
    """

    PYTHON_PUSH = "python_push"
    NATIVE = "native"


class SourceInterface(ABC):
    """Abstract interface for a pluggable live video source.

    `read_frame()` is the contract for `python_push` mode. Native-mode
    sources expose a `Gst.Bin` instead and `read_frame()` may return `None`
    or raise — `PipelineManager` does not call it for native sources.
    """

    @abstractmethod
    def connect_source(self) -> bool:
        """Connect to the source and return whether it succeeded."""

    @abstractmethod
    def disconnect_source(self) -> None:
        """Disconnect from the source if connected."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return whether the source is currently available."""

    @abstractmethod
    def get_display_name(self) -> str:
        """Return a user-facing name for the source."""

    def get_feed_id(self) -> str:
        """Return the stable feed identifier for the source."""
        return self.get_display_name()

    @abstractmethod
    def create_pipeline_fragment(self) -> str:
        """Describe how this source will later plug into a native GStreamer graph."""

    @property
    def pipeline_mode(self) -> PipelineMode:
        """Return how the source delivers frames into the per-feed graph.

        Defaults to `python_push` so existing sources keep working. Native
        sources should override and provide `build_native_bin()`.
        """
        return PipelineMode.PYTHON_PUSH

    def build_native_bin(self, gst_module: object) -> object | None:
        """Build a configured `Gst.Bin` for this source.

        Only meaningful for `pipeline_mode == NATIVE` sources. The returned
        bin must expose a single video src ghost pad emitting `BGR` raw
        video at the source's negotiated frame size and rate. Default
        implementation returns `None` — `PipelineManager` falls back to the
        Python-push appsrc head.
        """
        return None

    @abstractmethod
    def read_frame(self) -> MediaFrame | None:
        """Return the next delivered frame or `None` if no frame is available."""

    def supports_embedded_audio(self) -> bool:
        """Return whether this source can deliver source-embedded audio chunks."""
        return False

    def get_audio_format(self) -> AudioFormat | None:
        """Return the negotiated embedded audio format when available."""
        return None

    def read_audio_chunk(self) -> AudioChunk | None:
        """Return the next source-embedded audio chunk, if available."""
        return None

    @abstractmethod
    def get_frame_size(self) -> tuple[int, int]:
        """Return the source frame size as width and height."""

    @abstractmethod
    def get_nominal_fps(self) -> float:
        """Return the target frame rate used by the temporary source."""

    def get_status_message(self) -> str | None:
        """Return a non-fatal operator-facing status message, if any."""
        return None

    def get_ingest_telemetry(self) -> IngestTelemetry | None:
        """Return raw vs target resolution/FPS when the source can determine them."""
        return None
