"""Shared core models used across the application."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

import numpy as np
import numpy.typing as npt


class PlaybackMode(str, Enum):
    """Modes that describe what the operator is currently viewing."""

    LIVE = "LIVE"
    PAUSED = "PAUSED"
    REPLAY = "REPLAY"
    SOURCE_LOST = "SOURCE_LOST"


FrameArray: TypeAlias = npt.NDArray[np.uint8]


@dataclass(slots=True, frozen=True)
class IngestTelemetry:
    """Negotiated capture characteristics vs the pipeline target (resize / rate caps)."""

    target_width: int
    target_height: int
    target_fps: float
    raw_width: int | None = None
    raw_height: int | None = None
    raw_fps: float | None = None

    def summary_line(self) -> str:
        """Single-line description for logs and the operator status panel."""
        target_part = f"{self.target_width}x{self.target_height} @ {self.target_fps:.4g} fps"
        if self.raw_width is None or self.raw_height is None:
            raw_part = "unknown"
        else:
            fps_text = f"{self.raw_fps:.4g}" if self.raw_fps is not None else "?"
            raw_part = f"{self.raw_width}x{self.raw_height} @ {fps_text} fps"
        return f"Raw {raw_part} → target {target_part}"


@dataclass(slots=True)
class MediaFrame:
    """A single timestamped frame delivered through the temporary media layer.

    The OpenCV-backed `image` payload is temporary for this milestone. Later
    GStreamer tee/fan-out and NDI integration can plug in behind the same
    lightweight frame contract.
    """

    frame_id: int
    timestamp: float
    image: FrameArray
    source_name: str
    feed_id: str = "default"

    @property
    def image_bgr(self) -> FrameArray:
        """Return the current OpenCV BGR payload."""
        return self.image


@dataclass(slots=True, frozen=True)
class AudioFormat:
    """PCM audio characteristics used by the transitional audio tee."""

    sample_rate: int = 48_000
    channels: int = 2
    sample_format: str = "S16LE"

    @property
    def bytes_per_sample(self) -> int:
        """Return bytes per sample for the configured PCM format."""
        if self.sample_format.upper() != "S16LE":
            raise ValueError(f"Unsupported audio sample format: {self.sample_format}")
        return 2

    @property
    def bytes_per_frame(self) -> int:
        """Return bytes for one interleaved sample frame."""
        return self.channels * self.bytes_per_sample


@dataclass(slots=True, frozen=True)
class AudioChunk:
    """Timestamped source-embedded PCM audio delivered through the audio tee."""

    timestamp: float
    data: bytes
    format: AudioFormat
    source_name: str
    feed_id: str = "default"

    @property
    def duration_seconds(self) -> float:
        """Return the chunk duration based on byte count and PCM format."""
        bytes_per_frame = self.format.bytes_per_frame
        if bytes_per_frame <= 0 or self.format.sample_rate <= 0:
            return 0.0
        return len(self.data) / float(bytes_per_frame * self.format.sample_rate)


@dataclass(slots=True, frozen=True)
class FrameOverlayInfo:
    """Immutable metadata that describes a captured frame.

    The optional `feed_id` keeps the contract open for future multi-feed support
    without coupling the current implementation to a specific routing scheme.
    """

    feed_id: str | None = None
    source_name: str | None = None
    frame_id: int | None = None
    capture_timestamp: float | None = None

    @classmethod
    def from_media_frame(cls, frame: MediaFrame, feed_id: str | None = None) -> "FrameOverlayInfo":
        """Build overlay metadata directly from a delivered media frame."""
        return cls(
            feed_id=feed_id,
            source_name=frame.source_name,
            frame_id=frame.frame_id,
            capture_timestamp=frame.timestamp,
        )


@dataclass(slots=True, frozen=True)
class PlaybackOverlayInfo:
    """Dynamic metadata about the operator's current playback context."""

    mode: PlaybackMode = PlaybackMode.SOURCE_LOST
    playback_timestamp: float | None = None
    wall_clock_timestamp: float | None = None
    seconds_behind_live: float = 0.0
    playback_rate: float = 1.0
    status_text: str | None = None


@dataclass(slots=True)
class FeedDefinition:
    """Configuration for a single ingest feed."""

    feed_id: str
    display_name: str
    source_kind: str = "auto"
    camera_index: int = 0
    ndi_name: str | None = None
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class FeedPaths:
    """Filesystem locations associated with one feed inside a session."""

    feed_id: str
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path


@dataclass(slots=True)
class SessionPaths:
    """Filesystem locations associated with a recording session."""

    session_id: str
    root_dir: Path
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path

    def get_feed_paths(self, feed_id: str) -> FeedPaths:
        """Return the per-feed directories used inside the session tree."""
        safe_feed_id = feed_id.strip() or "default"
        return FeedPaths(
            feed_id=safe_feed_id,
            recording_dir=self.recording_dir / safe_feed_id,
            rolling_dir=self.rolling_dir / safe_feed_id,
            clips_dir=self.clips_dir / safe_feed_id,
        )
