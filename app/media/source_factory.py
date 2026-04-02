"""Helpers for constructing the preferred live video source chain."""

from __future__ import annotations

import logging

from app.config.settings import AppSettings
from app.core.models import MediaFrame
from app.media.gstreamer_camera_source import GStreamerCameraSource
from app.media.source_interface import SourceInterface
from app.media.test_source import TestSource

LOGGER = logging.getLogger(__name__)


class PreferredSourceChain(SourceInterface):
    """Try sources in order and keep the first one that connects."""

    def __init__(self, sources: list[SourceInterface]) -> None:
        if not sources:
            raise ValueError("PreferredSourceChain requires at least one source.")
        self._sources = sources
        self._active_source: SourceInterface | None = None

    def connect_source(self) -> bool:
        """Connect the first source that succeeds."""
        if self._active_source is not None and self._active_source.is_connected():
            return True

        self._active_source = None
        for source in self._sources:
            if not source.connect_source():
                LOGGER.info("Source candidate %s did not connect.", source.__class__.__name__)
                continue
            self._active_source = source
            LOGGER.info("Selected source candidate %s.", source.__class__.__name__)
            return True
        return False

    def disconnect_source(self) -> None:
        """Disconnect the active source if one is selected."""
        if self._active_source is not None:
            self._active_source.disconnect_source()
        self._active_source = None

    def is_connected(self) -> bool:
        """Return whether the active source is connected."""
        return self._active_source is not None and self._active_source.is_connected()

    def get_display_name(self) -> str:
        """Return the selected source name."""
        return self._active().get_display_name()

    def create_pipeline_fragment(self) -> str:
        """Return the selected source fragment description."""
        return self._active().create_pipeline_fragment()

    def read_frame(self) -> MediaFrame | None:
        """Read the next frame from the selected source."""
        return self._active().read_frame()

    def get_frame_size(self) -> tuple[int, int]:
        """Return the selected source frame size."""
        return self._active().get_frame_size()

    def get_nominal_fps(self) -> float:
        """Return the selected source frame rate."""
        return self._active().get_nominal_fps()

    def get_status_message(self) -> str | None:
        """Return the selected source status message."""
        return self._active().get_status_message()

    def _active(self) -> SourceInterface:
        if self._active_source is None:
            raise RuntimeError("No active source is selected.")
        return self._active_source


def build_default_source(settings: AppSettings) -> SourceInterface:
    """Build the preferred live source chain for the current platform."""
    return PreferredSourceChain(
        [
            GStreamerCameraSource(
                source_name=settings.default_source_name,
                camera_index=settings.test_camera_index,
                frame_width=settings.target_frame_width,
                frame_height=settings.target_frame_height,
                target_fps=settings.target_fps,
            ),
            TestSource(
                source_name=settings.default_source_name,
                camera_index=settings.test_camera_index,
                frame_width=settings.target_frame_width,
                frame_height=settings.target_frame_height,
                target_fps=settings.target_fps,
            ),
        ]
    )
