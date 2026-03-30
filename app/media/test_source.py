"""Temporary webcam-backed test source with a synthetic fallback."""

from __future__ import annotations

import time
from math import sin

import cv2
import numpy as np

from app.core.models import MediaFrame
from app.media.source_interface import SourceInterface


class TestSource(SourceInterface):
    """Provides a continuous frame stream for the first working vertical slice."""

    def __init__(
        self,
        source_name: str,
        camera_index: int,
        frame_width: int,
        frame_height: int,
        target_fps: float,
    ) -> None:
        self._base_source_name = source_name
        self._display_name = source_name
        self._camera_index = camera_index
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._target_fps = max(target_fps, 1.0)
        self._frame_counter = 0
        self._connected = False
        self._capture: cv2.VideoCapture | None = None
        self._use_synthetic_frames = False
        self._last_frame_monotonic = 0.0

    def connect_source(self) -> bool:
        """Open the preferred camera or fall back to a synthetic test feed."""
        if self._connected:
            return True

        capture = cv2.VideoCapture(self._camera_index, cv2.CAP_DSHOW)
        if capture.isOpened():
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._frame_width))
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._frame_height))
            capture.set(cv2.CAP_PROP_FPS, float(self._target_fps))
            self._capture = capture
            self._use_synthetic_frames = False
            self._display_name = f"{self._base_source_name} (Camera {self._camera_index})"
        else:
            capture.release()
            self._capture = None
            self._use_synthetic_frames = True
            self._display_name = f"{self._base_source_name} (Synthetic Fallback)"

        self._connected = True
        self._last_frame_monotonic = time.perf_counter()
        return True

    def disconnect_source(self) -> None:
        """Release the active source backend."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._connected = False

    def is_connected(self) -> bool:
        """Return whether the source is available for frame reads."""
        return self._connected

    def get_display_name(self) -> str:
        """Return the current source label."""
        return self._display_name

    def create_pipeline_fragment(self) -> str:
        """Describe the future source fragment once GStreamer replaces this path."""
        return "opencv-test-source-placeholder"

    def read_frame(self) -> MediaFrame | None:
        """Read a frame from the camera or generate a synthetic fallback frame."""
        if not self._connected:
            return None

        if self._use_synthetic_frames:
            return self._generate_synthetic_frame()

        assert self._capture is not None
        ok, frame = self._capture.read()
        if not ok or frame is None:
            self._switch_to_synthetic_fallback()
            return self._generate_synthetic_frame()

        frame = cv2.resize(frame, (self._frame_width, self._frame_height))
        return self._build_media_frame(frame)

    def get_frame_size(self) -> tuple[int, int]:
        """Return the expected frame size."""
        return self._frame_width, self._frame_height

    def get_nominal_fps(self) -> float:
        """Return the target frame rate."""
        return self._target_fps

    def _switch_to_synthetic_fallback(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._use_synthetic_frames = True
        self._display_name = f"{self._base_source_name} (Synthetic Fallback)"

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
        cv2.rectangle(canvas, (30, 30), (self._frame_width - 30, self._frame_height - 30), (255, 255, 255), 2)

        label = time.strftime("%H:%M:%S", time.localtime(timestamp))
        cv2.putText(canvas, "Synthetic Test Feed", (26, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        cv2.putText(canvas, f"Frame {self._frame_counter:05d}", (26, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        cv2.putText(canvas, f"Time {label}", (26, 124), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        self._last_frame_monotonic = time.perf_counter()
        return MediaFrame(
            frame_id=self._frame_counter,
            timestamp=timestamp,
            image_bgr=canvas,
            source_name=self._display_name,
        )

    def _build_media_frame(self, frame: np.ndarray) -> MediaFrame:
        self._frame_counter += 1
        timestamp = time.time()
        label = time.strftime("%H:%M:%S", time.localtime(timestamp))
        cv2.putText(frame, self._display_name, (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame, f"Frame {self._frame_counter:05d}", (16, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame, f"Time {label}", (16, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        self._last_frame_monotonic = time.perf_counter()
        return MediaFrame(
            frame_id=self._frame_counter,
            timestamp=timestamp,
            image_bgr=frame,
            source_name=self._display_name,
        )
