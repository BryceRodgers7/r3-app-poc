"""Stub NDI source implementation."""

from __future__ import annotations

from app.media.source_interface import SourceInterface


class NDIReceiver(SourceInterface):
    """Placeholder NDI receiver that satisfies the source contract."""

    def __init__(self, source_name: str) -> None:
        self._source_name = source_name
        self._connected = False

    def connect_source(self) -> bool:
        """Pretend to connect to an NDI source for scaffold purposes."""
        # TODO: Integrate the NDI SDK and source discovery workflow.
        self._connected = True
        return self._connected

    def disconnect_source(self) -> None:
        """Disconnect the current source."""
        self._connected = False

    def is_connected(self) -> bool:
        """Return the current connection flag."""
        return self._connected

    def get_display_name(self) -> str:
        """Return the configured source name."""
        return self._source_name

    def create_pipeline_fragment(self) -> str:
        """Describe the future media source fragment."""
        return "ndi-source-placeholder"
