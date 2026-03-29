"""Abstract source contracts for live video ingest."""

from __future__ import annotations

from abc import ABC, abstractmethod


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
