"""Abstract source contracts for live video ingest."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.models import MediaFrame


class SourceInterface(ABC):
    """Abstract interface for a pluggable live video source."""

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
        """Return a placeholder pipeline fragment for future GStreamer wiring."""

    @abstractmethod
    def read_frame(self) -> MediaFrame | None:
        """Read the next frame from the source."""

    @abstractmethod
    def get_frame_size(self) -> tuple[int, int]:
        """Return the source frame size as width and height."""

    @abstractmethod
    def get_nominal_fps(self) -> float:
        """Return the target frame rate used by the temporary source."""
