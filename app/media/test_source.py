"""Temporary webcam-backed test source with a synthetic fallback."""

from __future__ import annotations

import logging
import sys
import time
from math import sin

import cv2
import numpy as np

from app.core.models import MediaFrame
from app.media.source_interface import SourceInterface

LOGGER = logging.getLogger(__name__)


class TestSource(SourceInterface):
    """Provides a continuous frame stream for the first working vertical slice.

    This temporary source keeps NDI out of scope while exercising the same
    frame-delivery path that later production sources will use.
    """

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
        self._capture_backend_name: str | None = None
        self._use_synthetic_frames = False
        self._last_frame_monotonic = 0.0
        self._pending_frame: np.ndarray | None = None
        self._status_message: str | None = None

    def connect_source(self) -> bool:
        """Open the preferred camera or fall back to a synthetic test feed."""
        if self._connected:
            return True

        self._capture = None
        self._capture_backend_name = None
        self._pending_frame = None
        self._status_message = None
        self._use_synthetic_frames = True
        self._display_name = f"{self._base_source_name} (Synthetic Fallback)"
        saw_black_startup_frames = False

        for backend_name, backend_id in self._get_backend_candidates():
            capture = cv2.VideoCapture(self._camera_index, backend_id)
            if not capture.isOpened():
                capture.release()
                continue

            self._configure_capture(capture)
            startup_frame = self._read_usable_startup_frame(capture)
            if startup_frame is None:
                saw_black_startup_frames = True
                LOGGER.warning(
                    "Rejected camera index %s via %s because startup frames were empty or black.",
                    self._camera_index,
                    backend_name,
                )
                capture.release()
                continue

            self._capture = capture
            self._capture_backend_name = backend_name
            self._pending_frame = startup_frame
            self._use_synthetic_frames = False
            self._display_name = (
                f"{self._base_source_name} (Camera {self._camera_index}, {backend_name})"
            )
            LOGGER.info(
                "Opened camera index %s using backend %s.",
                self._camera_index,
                backend_name,
            )
            break

        if self._use_synthetic_frames and saw_black_startup_frames:
            self._status_message = "Camera opened but returned black frames; using synthetic fallback."

        self._connected = True
        self._last_frame_monotonic = time.perf_counter()
        return True

    def disconnect_source(self) -> None:
        """Release the active source backend."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._capture_backend_name = None
        self._pending_frame = None
        self._connected = False

    def is_connected(self) -> bool:
        """Return whether the source is available for frame reads."""
        return self._connected

    def get_display_name(self) -> str:
        """Return the current source label."""
        return self._display_name

    def create_pipeline_fragment(self) -> str:
        """Describe the future source-side replacement for the current appsrc bridge."""
        return "opencv-test-source-placeholder"

    def get_status_message(self) -> str | None:
        """Return the current non-fatal status message."""
        return self._status_message

    def read_frame(self) -> MediaFrame | None:
        """Read the next delivered frame from the camera or synthetic fallback."""
        if not self._connected:
            return None

        if self._use_synthetic_frames:
            return self._generate_synthetic_frame()

        assert self._capture is not None
        if self._pending_frame is not None:
            frame = self._pending_frame
            self._pending_frame = None
            return self._build_media_frame(frame)

        ok, frame = self._capture.read()
        if not ok or frame is None:
            self._switch_to_synthetic_fallback("Camera capture stopped; using synthetic fallback.")
            return self._generate_synthetic_frame()

        if self._is_black_frame(frame):
            LOGGER.warning(
                "Camera index %s via %s returned an all-black frame; switching to synthetic fallback.",
                self._camera_index,
                self._capture_backend_name or "unknown backend",
            )
            self._switch_to_synthetic_fallback(
                "Camera opened but returned black frames; using synthetic fallback."
            )
            return self._generate_synthetic_frame()

        frame = cv2.resize(frame, (self._frame_width, self._frame_height))
        return self._build_media_frame(frame)

    def get_frame_size(self) -> tuple[int, int]:
        """Return the expected frame size."""
        return self._frame_width, self._frame_height

    def get_nominal_fps(self) -> float:
        """Return the target frame rate."""
        return self._target_fps

    def _switch_to_synthetic_fallback(self, status_message: str | None = None) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._capture_backend_name = None
        self._pending_frame = None
        self._use_synthetic_frames = True
        self._display_name = f"{self._base_source_name} (Synthetic Fallback)"
        self._status_message = status_message

    def _get_backend_candidates(self) -> list[tuple[str, int]]:
        if sys.platform.startswith("win"):
            candidates: list[tuple[str, int]] = [("CAP_DSHOW", cv2.CAP_DSHOW)]
            msmf_backend = getattr(cv2, "CAP_MSMF", None)
            if msmf_backend is not None:
                candidates.append(("CAP_MSMF", msmf_backend))
            if not candidates:
                candidates.append(("CAP_ANY", cv2.CAP_ANY))
        else:
            candidates = [("CAP_ANY", cv2.CAP_ANY)]

        deduplicated: list[tuple[str, int]] = []
        seen_backend_ids: set[int] = set()
        for backend_name, backend_id in candidates:
            if backend_id in seen_backend_ids:
                continue
            seen_backend_ids.add(backend_id)
            deduplicated.append((backend_name, backend_id))
        return deduplicated

    def _configure_capture(self, capture: cv2.VideoCapture) -> None:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._frame_width))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._frame_height))
        capture.set(cv2.CAP_PROP_FPS, float(self._target_fps))

    def _read_usable_startup_frame(self, capture: cv2.VideoCapture) -> np.ndarray | None:
        for _ in range(12):
            ok, frame = capture.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            if self._is_black_frame(frame):
                time.sleep(0.05)
                continue
            return cv2.resize(frame, (self._frame_width, self._frame_height))
        return None

    def _is_black_frame(self, frame: np.ndarray) -> bool:
        return bool(frame.size == 0 or not np.any(frame))

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
            image=canvas,
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
            image=frame,
            source_name=self._display_name,
        )
