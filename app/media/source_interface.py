"""Abstract source contracts for live video ingest."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.models import MediaFrame


class SourceInterface(ABC):
    """Abstract interface for a pluggable live video source.

    `read_frame()` is the temporary frame-delivery contract for the current
    vertical slice. Later NDI and GStreamer-backed sources should plug in here
    without changing how replay, recording, or UI playback consume frames.
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

    @abstractmethod
    def create_pipeline_fragment(self) -> str:
        """Describe how this source will later plug into a native GStreamer graph."""

    @abstractmethod
    def read_frame(self) -> MediaFrame | None:
        """Return the next delivered frame or `None` if no frame is available."""

    @abstractmethod
    def get_frame_size(self) -> tuple[int, int]:
        """Return the source frame size as width and height."""

    @abstractmethod
    def get_nominal_fps(self) -> float:
        """Return the target frame rate used by the temporary source."""

    def get_status_message(self) -> str | None:
        """Return a non-fatal operator-facing status message, if any."""
        return None
