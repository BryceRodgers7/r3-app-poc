"""Rolling replay storage service."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import threading
from collections.abc import Callable

import cv2
import numpy as np

from app.core.models import AudioChunk, FeedPaths, MediaFrame, SessionPaths
from app.media.muxed_writer import MuxedMediaWriter, MuxedWriterInfo

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplayFrameRef:
    """Metadata for a single replay frame persisted as a lightweight thumbnail."""

    frame_id: int
    timestamp: float
    sequence_index: int
    image_path: Path
    source_name: str
    feed_id: str = "default"
    media_segment_index: int | None = None


@dataclass(slots=True)
class ReplayMediaSegmentRef:
    """Metadata for one rolling muxed replay segment."""

    start_timestamp: float
    end_timestamp: float
    sequence_index: int
    media_path: Path
    source_name: str
    feed_id: str = "default"
    frame_count: int = 0
    audio_bytes: int = 0
    writer_info: MuxedWriterInfo | None = None


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
    def append_audio_chunk(self, chunk: AudioChunk) -> None:
        """Append source-embedded audio to the rolling replay history."""

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
    def get_media_segment_at_or_before(self, timestamp: float) -> ReplayMediaSegmentRef | None:
        """Return the muxed media segment covering or preceding a timestamp."""

    @abstractmethod
    def get_multifile_location_pattern(self) -> str | None:
        """Return legacy native replay source pattern, when available."""

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
    """Stores a rolling, timestamp-addressable history of recent A/V segments."""

    _MANIFEST_FILENAME = "rolling_manifest.json"
    _FRAME_FILENAME_TEMPLATE = "frame_%09d.jpg"
    _MEDIA_SEGMENT_FILENAME_TEMPLATE = "segment_%09d.mp4"

    def __init__(
        self,
        buffer_duration_seconds: int = 120,
        jpeg_quality: int = 80,
        audio_segment_seconds: float = 2.0,
        audio_bitrate: int = 128_000,
        writer_factory: Callable[..., MuxedMediaWriter] = MuxedMediaWriter,
    ) -> None:
        self._buffer_duration_seconds = buffer_duration_seconds
        self._jpeg_quality = jpeg_quality
        self._segment_seconds = max(audio_segment_seconds, 0.25)
        self._audio_bitrate = max(audio_bitrate, 16_000)
        self._writer_factory = writer_factory
        self._session_paths: SessionPaths | None = None
        self._feed_paths: FeedPaths | None = None
        self._feed_id = "default"
        self._rolling_dir: Path | None = None
        self._manifest_path: Path | None = None
        self._is_running = False
        self._frames: deque[ReplayFrameRef] = deque()
        self._media_segments: deque[ReplayMediaSegmentRef] = deque()
        self._active_segment: ReplayMediaSegmentRef | None = None
        self._active_writer: MuxedMediaWriter | None = None
        self._next_sequence_index = 0
        self._next_segment_index = 0
        self._lock = threading.Lock()

    @property
    def buffer_duration_seconds(self) -> int:
        """Return the configured rolling buffer length."""
        return self._buffer_duration_seconds

    def start(self, session_paths: SessionPaths, feed_id: str = "default") -> None:
        """Prepare rolling buffer storage for the active session."""
        with self._lock:
            self._session_paths = session_paths
            self._feed_id = feed_id
            self._feed_paths = session_paths.get_feed_paths(feed_id)
            self._rolling_dir = self._feed_paths.rolling_dir
            self._manifest_path = self._rolling_dir / self._MANIFEST_FILENAME
            self._is_running = True
            self._next_sequence_index = 0
            self._next_segment_index = 0
            self._clear_rolling_directory_locked()
            self._frames.clear()
            self._media_segments.clear()
            self._active_segment = None
            self._active_writer = None
            self._write_manifest_locked()
        LOGGER.info("Replay store started in %s", session_paths.rolling_dir)

    def stop(self) -> None:
        """Stop rolling buffer updates."""
        with self._lock:
            self._is_running = False
            self._close_active_segment_locked()
            self._frames.clear()
            self._media_segments.clear()
            self._next_sequence_index = 0
            self._next_segment_index = 0
            self._clear_rolling_directory_locked()
            self._feed_paths = None
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
            self._ensure_segment_for_timestamp_locked(frame.timestamp, frame.source_name, frame.feed_id)
            if self._active_writer is not None:
                self._active_writer.write_frame(frame)
                self._sync_active_segment_locked(frame.timestamp)

            frame_path = self._rolling_dir / (self._FRAME_FILENAME_TEMPLATE % self._next_sequence_index)
            frame_path.write_bytes(encoded_frame.tobytes())
            self._frames.append(
                ReplayFrameRef(
                    frame_id=frame.frame_id,
                    timestamp=frame.timestamp,
                    sequence_index=self._next_sequence_index,
                    image_path=frame_path,
                    source_name=frame.source_name,
                    feed_id=frame.feed_id,
                    media_segment_index=self._active_segment.sequence_index if self._active_segment else None,
                )
            )
            self._next_sequence_index += 1
            self._prune_locked()
            self._write_manifest_locked()

    def append_audio_chunk(self, chunk: AudioChunk) -> None:
        """Append source-embedded audio to the rolling replay history."""
        if not self._is_running or not chunk.data:
            return

        with self._lock:
            if not self._is_running or self._rolling_dir is None:
                return
            self._ensure_segment_for_timestamp_locked(chunk.timestamp, chunk.source_name, chunk.feed_id)
            if self._active_writer is not None:
                self._active_writer.write_audio_chunk(chunk)
                self._sync_active_segment_locked(chunk.timestamp + chunk.duration_seconds)
            self._prune_locked()
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

    def get_media_segment_at_or_before(self, timestamp: float) -> ReplayMediaSegmentRef | None:
        """Return the muxed media segment that covers or precedes a timestamp."""
        with self._lock:
            if not self._media_segments:
                return None
            for segment in reversed(self._media_segments):
                if segment.start_timestamp <= timestamp:
                    return segment
            return self._media_segments[0]

    def get_multifile_location_pattern(self) -> str | None:
        """Return legacy native replay source pattern, which muxed replay no longer uses."""
        return None

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

    def _ensure_segment_for_timestamp_locked(self, timestamp: float, source_name: str, feed_id: str) -> None:
        if self._active_segment is None:
            self._open_segment_locked(timestamp, source_name, feed_id)
            return
        if timestamp - self._active_segment.start_timestamp >= self._segment_seconds:
            self._close_active_segment_locked()
            self._open_segment_locked(timestamp, source_name, feed_id)

    def _open_segment_locked(self, timestamp: float, source_name: str, feed_id: str) -> None:
        assert self._rolling_dir is not None
        media_path = self._rolling_dir / (self._MEDIA_SEGMENT_FILENAME_TEMPLATE % self._next_segment_index)
        segment = ReplayMediaSegmentRef(
            start_timestamp=timestamp,
            end_timestamp=timestamp,
            sequence_index=self._next_segment_index,
            media_path=media_path,
            source_name=source_name,
            feed_id=feed_id,
        )
        self._next_segment_index += 1
        self._active_segment = segment
        self._active_writer = self._writer_factory(
            media_path,
            fps_hint=30.0,
            audio_bitrate=self._audio_bitrate,
        )
        self._media_segments.append(segment)

    def _close_active_segment_locked(self) -> None:
        if self._active_writer is not None:
            self._active_writer.close()
            self._sync_active_segment_locked()
            self._active_writer = None
        self._active_segment = None

    def _sync_active_segment_locked(self, end_timestamp: float | None = None) -> None:
        if self._active_segment is None or self._active_writer is None:
            return
        self._active_segment.media_path = self._active_writer.output_path
        self._active_segment.frame_count = self._active_writer.video_frame_count
        self._active_segment.audio_bytes = self._active_writer.audio_bytes
        self._active_segment.writer_info = self._active_writer.info
        if end_timestamp is not None:
            self._active_segment.end_timestamp = max(self._active_segment.end_timestamp, end_timestamp)

    def _prune_locked(self) -> None:
        if not self._frames:
            return

        latest_timestamp = self._frames[-1].timestamp
        while self._frames and latest_timestamp - self._frames[0].timestamp > self._buffer_duration_seconds:
            expired_frame = self._frames.popleft()
            try:
                expired_frame.image_path.unlink(missing_ok=True)
            except Exception:
                LOGGER.warning("Failed to delete expired replay frame %s", expired_frame.image_path)

        while (
            self._media_segments
            and latest_timestamp - self._media_segments[0].end_timestamp > self._buffer_duration_seconds
        ):
            expired_segment = self._media_segments.popleft()
            if expired_segment is self._active_segment:
                continue
            try:
                expired_segment.media_path.unlink(missing_ok=True)
            except Exception:
                LOGGER.warning("Failed to delete expired replay segment %s", expired_segment.media_path)

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
            LOGGER.warning("Replay frame thumbnail disappeared before decode: %s", frame_ref.image_path)
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
            feed_id=frame_ref.feed_id,
        )

    def _clear_rolling_directory_locked(self) -> None:
        if self._rolling_dir is None:
            return
        self._rolling_dir.mkdir(parents=True, exist_ok=True)
        for frame_file in self._rolling_dir.glob("frame_*.jpg"):
            frame_file.unlink(missing_ok=True)
        for segment_file in self._rolling_dir.glob("segment_*.mp4"):
            segment_file.unlink(missing_ok=True)
        for segment_file in self._rolling_dir.glob("segment_*.mkv"):
            segment_file.unlink(missing_ok=True)
        manifest_path = self._rolling_dir / self._MANIFEST_FILENAME
        manifest_path.unlink(missing_ok=True)

    def _write_manifest_locked(self) -> None:
        if self._manifest_path is None:
            return

        manifest = {
            "feed_id": self._feed_id,
            "frame_count": len(self._frames),
            "buffer_duration_seconds": self._buffer_duration_seconds,
            "jpeg_quality": self._jpeg_quality,
            "segment_seconds": self._segment_seconds,
            "frames": [
                {
                    "frame_id": frame_ref.frame_id,
                    "timestamp": frame_ref.timestamp,
                    "sequence_index": frame_ref.sequence_index,
                    "image_path": frame_ref.image_path.name,
                    "source_name": frame_ref.source_name,
                    "feed_id": frame_ref.feed_id,
                    "media_segment_index": frame_ref.media_segment_index,
                }
                for frame_ref in self._frames
            ],
            "media_segments": [
                {
                    "start_timestamp": segment.start_timestamp,
                    "end_timestamp": segment.end_timestamp,
                    "sequence_index": segment.sequence_index,
                    "media_path": segment.media_path.name,
                    "source_name": segment.source_name,
                    "feed_id": segment.feed_id,
                    "frame_count": segment.frame_count,
                    "has_audio": segment.audio_bytes > 0,
                    "audio_bytes": segment.audio_bytes,
                    "container": segment.writer_info.container if segment.writer_info else None,
                    "video_codec": segment.writer_info.video_encoder if segment.writer_info else None,
                    "audio_codec": segment.writer_info.audio_encoder if segment.writer_info else None,
                }
                for segment in self._media_segments
            ],
        }
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
