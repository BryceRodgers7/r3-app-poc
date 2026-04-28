"""Synthetic test source for development without NDI hardware.

The OpenCV / DSHOW / MSMF webcam paths and the `GStreamerCameraSource`
fallback that this module used to support were removed in Phase 2.5.
Production deployments use NDI exclusively (`kind = "ndi"`); this synthetic
source is the dev-time fallback (`kind = "synthetic"`) and is the only
non-NDI source the application will build.
"""

from __future__ import annotations

import logging
import time
from math import sin

import cv2
import numpy as np

from app.core.models import IngestTelemetry, MediaFrame
from app.media.source_interface import SourceInterface

LOGGER = logging.getLogger(__name__)


class TestSource(SourceInterface):
    """Deterministic synthetic frame generator for camera-less development."""

    def __init__(
        self,
        source_name: str,
        feed_id: str,
        frame_width: int,
        frame_height: int,
        target_fps: float,
    ) -> None:
        self._base_source_name = source_name
        self._feed_id = feed_id
        self._display_name = f"{source_name} (Synthetic)"
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._target_fps = max(target_fps, 1.0)
        self._frame_counter = 0
        self._connected = False
        self._last_frame_monotonic = 0.0
        self._ingest_telemetry: IngestTelemetry | None = None

    def connect_source(self) -> bool:
        if self._connected:
            return True
        self._connected = True
        self._last_frame_monotonic = time.perf_counter()
        self._ingest_telemetry = IngestTelemetry(
            target_width=self._frame_width,
            target_height=self._frame_height,
            target_fps=self._target_fps,
            raw_width=None,
            raw_height=None,
            raw_fps=None,
        )
        LOGGER.info("Synthetic source connected: %s", self._ingest_telemetry.summary_line())
        return True

    def disconnect_source(self) -> None:
        self._connected = False
        self._ingest_telemetry = None

    def is_connected(self) -> bool:
        return self._connected

    def get_display_name(self) -> str:
        return self._display_name

    def get_feed_id(self) -> str:
        return self._feed_id

    def create_pipeline_fragment(self) -> str:
        return "synthetic-source-placeholder"

    def get_status_message(self) -> str | None:
        return None

    def read_frame(self) -> MediaFrame | None:
        if not self._connected:
            return None
        return self._generate_synthetic_frame()

    def get_frame_size(self) -> tuple[int, int]:
        return self._frame_width, self._frame_height

    def get_nominal_fps(self) -> float:
        return self._target_fps

    def get_ingest_telemetry(self) -> IngestTelemetry | None:
        return self._ingest_telemetry

    def _generate_synthetic_frame(self) -> MediaFrame:
        target_interval = 1.0 / self._target_fps
        elapsed = time.perf_counter() - self._last_frame_monotonic
        if elapsed < target_interval:
            time.sleep(target_interval - elapsed)

        canvas = np.zeros((self._frame_height, self._frame_width, 3), dtype=np.uint8)
        timestamp = time.time()
        self._frame_counter += 1
        phase = self._frame_counter / self._target_fps

        canvas[:, :, 0] = np.linspace(30, 190, self._frame_width, dtype=np.uint8)
        canvas[:, :, 1] = 40
        canvas[:, :, 2] = np.linspace(180, 40, self._frame_height, dtype=np.uint8)[:, None]

        center_x = int((self._frame_width * 0.5) + (self._frame_width * 0.28 * sin(phase)))
        center_y = int((self._frame_height * 0.5) + (self._frame_height * 0.2 * sin(phase * 0.6)))
        cv2.circle(canvas, (center_x, center_y), 36, (0, 255, 255), -1)
        cv2.rectangle(
            canvas, (30, 30), (self._frame_width - 30, self._frame_height - 30), (255, 255, 255), 2
        )

        self._last_frame_monotonic = time.perf_counter()
        return MediaFrame(
            frame_id=self._frame_counter,
            timestamp=timestamp,
            image=canvas,
            source_name=self._display_name,
            feed_id=self._feed_id,
        )
