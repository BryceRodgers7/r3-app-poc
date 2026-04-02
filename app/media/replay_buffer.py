"""Rolling replay storage service."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import threading

import cv2
import numpy as np

from app.core.models import MediaFrame, SessionPaths

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplayFrameRef:
    """Metadata for a single replay frame persisted in rolling storage."""

    frame_id: int
    timestamp: float
    sequence_index: int
    image_path: Path
    source_name: str


class ReplayStore(ABC):
    """Interface for timestamp-addressable rolling replay storage."""

    @abstractmethod
    def start(self, session_paths: SessionPaths) -> None:
        """Prepare rolling replay storage for the active session."""

    @abstractmethod
    def stop(self) -> None:
        """Stop replay storage updates."""

    @abstractmethod
    def is_running(self) -> bool:
        """Return whether rolling replay buffering is active."""

    @abstractmethod
    def append_frame(self, frame: MediaFrame) -> None:
        """Append a new frame to the rolling replay history."""

    @abstractmethod
    def get_latest_frame(self) -> MediaFrame | None:
        """Return the newest buffered frame."""

    @abstractmethod
    def get_frame_at_or_before(self, timestamp: float) -> MediaFrame | None:
        """Return the newest frame at or before the requested timestamp."""

    @abstractmethod
    def get_frame_ref_at_or_before(self, timestamp: float) -> ReplayFrameRef | None:
        """Return metadata for the newest frame at or before the requested timestamp."""

    @abstractmethod
    def get_latest_frame_ref(self) -> ReplayFrameRef | None:
        """Return metadata for the newest buffered frame."""

    @abstractmethod
    def get_multifile_location_pattern(self) -> str | None:
        """Return the file pattern used by a native replay source."""

    @abstractmethod
    def get_seconds_behind_live(self, timestamp: float) -> float:
        """Return how far a timestamp sits behind the current live edge."""

    @abstractmethod
    def get_buffer_range(self) -> tuple[float | None, float | None]:
        """Return the oldest and newest buffered timestamps."""

    @abstractmethod
    def get_earliest_timestamp(self) -> float | None:
        """Return the earliest buffered timestamp."""

    @abstractmethod
    def get_latest_timestamp(self) -> float | None:
        """Return the latest buffered timestamp."""

    @abstractmethod
    def get_available_duration(self) -> float:
        """Return the total buffered timeline in seconds."""


class ReplayBuffer(ReplayStore):
    """Stores a rolling, timestamp-addressable history of recent frames on disk.

    Frames are persisted into the session's rolling directory as sequential JPEG
    files plus a compact manifest of the active replay window. This preserves the
    current controller-facing replay semantics while removing repeated JPEG decode
    work from the normal replay playback path.
    """

    _MANIFEST_FILENAME = "rolling_manifest.json"
    _FRAME_FILENAME_TEMPLATE = "frame_%09d.jpg"

    def __init__(self, buffer_duration_seconds: int = 120, jpeg_quality: int = 80) -> None:
        self._buffer_duration_seconds = buffer_duration_seconds
        self._jpeg_quality = jpeg_quality
        self._session_paths: SessionPaths | None = None
        self._rolling_dir: Path | None = None
        self._manifest_path: Path | None = None
        self._is_running = False
        self._frames: deque[ReplayFrameRef] = deque()
        self._next_sequence_index = 0
        self._lock = threading.Lock()

    @property
    def buffer_duration_seconds(self) -> int:
        """Return the configured rolling buffer length."""
        return self._buffer_duration_seconds

    def start(self, session_paths: SessionPaths) -> None:
        """Prepare rolling buffer storage for the active session."""
        with self._lock:
            self._session_paths = session_paths
            self._rolling_dir = session_paths.rolling_dir
            self._manifest_path = session_paths.rolling_dir / self._MANIFEST_FILENAME
            self._is_running = True
            self._next_sequence_index = 0
            self._clear_rolling_directory_locked()
            self._frames.clear()
            self._write_manifest_locked()
        LOGGER.info("Replay store started in %s", session_paths.rolling_dir)

    def stop(self) -> None:
        """Stop rolling buffer updates."""
        with self._lock:
            self._is_running = False
            self._frames.clear()
            self._next_sequence_index = 0
            self._clear_rolling_directory_locked()
        LOGGER.info("Replay store stopped")

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
            if not self._is_running or self._rolling_dir is None:
                return

            frame_path = self._rolling_dir / (self._FRAME_FILENAME_TEMPLATE % self._next_sequence_index)
            frame_path.write_bytes(encoded_frame.tobytes())
            self._frames.append(
                ReplayFrameRef(
                    frame_id=frame.frame_id,
                    timestamp=frame.timestamp,
                    sequence_index=self._next_sequence_index,
                    image_path=frame_path,
                    source_name=frame.source_name,
                )
            )
            self._next_sequence_index += 1
            self._prune_frames_locked()
            self._write_manifest_locked()

    def get_latest_frame(self) -> MediaFrame | None:
        """Return the newest buffered frame."""
        with self._lock:
            if not self._frames:
                return None
            return self._decode_frame_ref(self._frames[-1])

    def get_frame_at_or_before(self, timestamp: float) -> MediaFrame | None:
        """Return the newest frame at or before the requested timestamp."""
        with self._lock:
            if not self._frames:
                return None
            frame_ref = self._frame_ref_at_or_before_locked(timestamp)
            if frame_ref is None:
                return None
            return self._decode_frame_ref(frame_ref)

    def get_frame_ref_at_or_before(self, timestamp: float) -> ReplayFrameRef | None:
        """Return metadata for the newest frame at or before the requested timestamp."""
        with self._lock:
            if not self._frames:
                return None
            return self._frame_ref_at_or_before_locked(timestamp)

    def get_latest_frame_ref(self) -> ReplayFrameRef | None:
        """Return metadata for the newest buffered frame."""
        with self._lock:
            if not self._frames:
                return None
            return self._frames[-1]

    def get_multifile_location_pattern(self) -> str | None:
        """Return the printf-style file pattern used by the native replay source."""
        with self._lock:
            if self._rolling_dir is None:
                return None
            return str(self._rolling_dir / self._FRAME_FILENAME_TEMPLATE)

    def get_seconds_behind_live(self, timestamp: float) -> float:
        """Return how far a timestamp sits behind the current live edge."""
        with self._lock:
            if not self._frames:
                return 0.0
            latest_timestamp = self._frames[-1].timestamp
            return max(0.0, latest_timestamp - timestamp)

    def get_buffer_range(self) -> tuple[float | None, float | None]:
        """Return the oldest and newest buffered timestamps."""
        with self._lock:
            if not self._frames:
                return None, None
            return self._frames[0].timestamp, self._frames[-1].timestamp

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
            return self._get_available_duration_locked()

    def _get_available_duration_locked(self) -> float:
        if len(self._frames) < 2:
            return 0.0
        return max(0.0, self._frames[-1].timestamp - self._frames[0].timestamp)

    def _prune_frames_locked(self) -> None:
        if not self._frames:
            return

        latest_timestamp = self._frames[-1].timestamp
        while self._frames and latest_timestamp - self._frames[0].timestamp > self._buffer_duration_seconds:
            expired_frame = self._frames.popleft()
            try:
                expired_frame.image_path.unlink(missing_ok=True)
            except Exception:
                LOGGER.warning("Failed to delete expired replay frame %s", expired_frame.image_path)

    def _frame_ref_at_or_before_locked(self, target_timestamp: float) -> ReplayFrameRef | None:
        if not self._frames:
            return None

        for frame_ref in reversed(self._frames):
            if frame_ref.timestamp <= target_timestamp:
                return frame_ref

        return self._frames[0]

    def _decode_frame_ref(self, frame_ref: ReplayFrameRef) -> MediaFrame | None:
        try:
            encoded_bytes = frame_ref.image_path.read_bytes()
        except FileNotFoundError:
            LOGGER.warning("Replay frame file disappeared before decode: %s", frame_ref.image_path)
            return None

        encoded_array = np.frombuffer(encoded_bytes, dtype=np.uint8)
        decoded_image = cv2.imdecode(encoded_array, cv2.IMREAD_COLOR)
        if decoded_image is None:
            return None
        return MediaFrame(
            frame_id=frame_ref.frame_id,
            timestamp=frame_ref.timestamp,
            image=decoded_image,
            source_name=frame_ref.source_name,
        )

    def _clear_rolling_directory_locked(self) -> None:
        if self._rolling_dir is None:
            return
        self._rolling_dir.mkdir(parents=True, exist_ok=True)
        for frame_file in self._rolling_dir.glob("frame_*.jpg"):
            frame_file.unlink(missing_ok=True)
        manifest_path = self._rolling_dir / self._MANIFEST_FILENAME
        manifest_path.unlink(missing_ok=True)

    def _write_manifest_locked(self) -> None:
        if self._manifest_path is None:
            return

        manifest = {
            "frame_count": len(self._frames),
            "buffer_duration_seconds": self._buffer_duration_seconds,
            "jpeg_quality": self._jpeg_quality,
            "frames": [
                {
                    "frame_id": frame_ref.frame_id,
                    "timestamp": frame_ref.timestamp,
                    "sequence_index": frame_ref.sequence_index,
                    "image_path": frame_ref.image_path.name,
                    "source_name": frame_ref.source_name,
                }
                for frame_ref in self._frames
            ],
        }
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
