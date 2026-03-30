"""Rolling replay buffer service."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading

import cv2
import numpy as np

from app.core.models import MediaFrame, SessionPaths


@dataclass(slots=True)
class BufferedFrame:
    """Compressed frame payload stored in the temporary in-memory replay buffer."""

    frame_id: int
    timestamp: float
    encoded_image: bytes
    source_name: str


class ReplayBuffer:
    """Tracks replay buffer state independently of recording and preview."""

    def __init__(self, buffer_duration_seconds: int = 120, jpeg_quality: int = 80) -> None:
        self._buffer_duration_seconds = buffer_duration_seconds
        self._jpeg_quality = jpeg_quality
        self._session_paths: SessionPaths | None = None
        self._is_running = False
        self._seconds_behind_live = 0.0
        self._frames: deque[BufferedFrame] = deque()
        self._lock = threading.Lock()

    @property
    def buffer_duration_seconds(self) -> int:
        """Return the configured rolling buffer length."""
        return self._buffer_duration_seconds

    def start(self, session_paths: SessionPaths) -> None:
        """Prepare rolling buffer storage for the active session."""
        # TODO: Replace the temporary in-memory ring buffer with disk-backed rolling segments.
        with self._lock:
            self._session_paths = session_paths
            self._is_running = True
            self._seconds_behind_live = 0.0
            self._frames.clear()

    def stop(self) -> None:
        """Stop rolling buffer updates."""
        with self._lock:
            self._is_running = False
            self._frames.clear()

    def is_running(self) -> bool:
        """Return whether rolling replay buffering is active."""
        return self._is_running

    def append_frame(self, frame: MediaFrame) -> None:
        """Append a new frame to the rolling replay history."""
        if not self._is_running:
            return

        encoded_ok, encoded_frame = cv2.imencode(
            ".jpg",
            frame.image_bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality],
        )
        if not encoded_ok:
            return

        with self._lock:
            self._frames.append(
                BufferedFrame(
                    frame_id=frame.frame_id,
                    timestamp=frame.timestamp,
                    encoded_image=encoded_frame.tobytes(),
                    source_name=frame.source_name,
                )
            )
            self._prune_frames_locked()

    def seek_seconds_behind_live(self, seconds: float) -> float:
        """Clamp and store the current replay offset from live."""
        with self._lock:
            available_seconds = min(self._buffer_duration_seconds, self.get_available_duration_locked())
            clamped_seconds = max(0.0, min(seconds, float(available_seconds)))
            self._seconds_behind_live = clamped_seconds
            return self._seconds_behind_live

    def jump_to_live(self) -> None:
        """Reset replay playback back to the live edge."""
        with self._lock:
            self._seconds_behind_live = 0.0

    def get_seconds_behind_live(self) -> float:
        """Return the currently selected replay offset."""
        with self._lock:
            return self._seconds_behind_live

    def get_latest_frame(self) -> MediaFrame | None:
        """Return the newest buffered frame."""
        with self._lock:
            if not self._frames:
                return None
            return self._decode_buffered_frame(self._frames[-1])

    def get_frame_seconds_behind_live(self, seconds: float) -> MediaFrame | None:
        """Return the frame nearest to the requested live offset."""
        with self._lock:
            if not self._frames:
                return None
            latest_timestamp = self._frames[-1].timestamp
            target_timestamp = latest_timestamp - max(seconds, 0.0)
            return self._frame_nearest_to_timestamp_locked(target_timestamp)

    def get_earliest_timestamp(self) -> float | None:
        """Return the earliest buffered timestamp."""
        with self._lock:
            if not self._frames:
                return None
            return self._frames[0].timestamp

    def get_latest_timestamp(self) -> float | None:
        """Return the latest buffered timestamp."""
        with self._lock:
            if not self._frames:
                return None
            return self._frames[-1].timestamp

    def get_available_duration(self) -> float:
        """Return the total buffered timeline in seconds."""
        with self._lock:
            return self.get_available_duration_locked()

    def get_available_duration_locked(self) -> float:
        if len(self._frames) < 2:
            return 0.0
        return max(0.0, self._frames[-1].timestamp - self._frames[0].timestamp)

    def _prune_frames_locked(self) -> None:
        if not self._frames:
            return

        latest_timestamp = self._frames[-1].timestamp
        while self._frames and latest_timestamp - self._frames[0].timestamp > self._buffer_duration_seconds:
            self._frames.popleft()

    def _frame_nearest_to_timestamp_locked(self, target_timestamp: float) -> MediaFrame | None:
        if not self._frames:
            return None

        nearest = min(self._frames, key=lambda item: abs(item.timestamp - target_timestamp))
        return self._decode_buffered_frame(nearest)

    def _decode_buffered_frame(self, buffered_frame: BufferedFrame) -> MediaFrame | None:
        encoded_array = np.frombuffer(buffered_frame.encoded_image, dtype=np.uint8)
        decoded_image = cv2.imdecode(encoded_array, cv2.IMREAD_COLOR)
        if decoded_image is None:
            return None
        return MediaFrame(
            frame_id=buffered_frame.frame_id,
            timestamp=buffered_frame.timestamp,
            image_bgr=decoded_image,
            source_name=buffered_frame.source_name,
        )
