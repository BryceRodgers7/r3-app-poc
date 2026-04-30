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
    """Configuration for a single ingest feed.

    `source_kind` accepts only `"ndi"` (production) or `"synthetic"` (dev
    fallback). USB / OpenCV camera ingest was removed in Phase 2.5.
    """

    feed_id: str
    display_name: str
    source_kind: str = "synthetic"
    ndi_name: str | None = None
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class FeedPaths:
    """Filesystem locations associated with one feed inside a session.

    Slice 4.E added `quarantine_dir`: corrupt segment files are moved here
    by the startup recovery pass (§6.5). The directory is created lazily
    by the recovery code only when something actually needs quarantining,
    so happy-path sessions don't accumulate empty directories.
    """

    feed_id: str
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path
    quarantine_dir: Path


@dataclass(slots=True)
class SessionPaths:
    """Filesystem locations associated with a recording session."""

    session_id: str
    root_dir: Path
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path
    quarantine_dir: Path | None = None

    def __post_init__(self) -> None:
        # Ergonomic default for test fixtures that don't pass an explicit
        # quarantine_dir: derive `<root>/quarantine`. Production code goes
        # through `FileManager.create_session_paths` which sets it
        # explicitly.
        if self.quarantine_dir is None:
            self.quarantine_dir = self.root_dir / "quarantine"

    def get_feed_paths(self, feed_id: str) -> FeedPaths:
        """Return the per-feed directories used inside the session tree."""
        safe_feed_id = feed_id.strip() or "default"
        assert self.quarantine_dir is not None  # set by __post_init__
        return FeedPaths(
            feed_id=safe_feed_id,
            recording_dir=self.recording_dir / safe_feed_id,
            rolling_dir=self.rolling_dir / safe_feed_id,
            clips_dir=self.clips_dir / safe_feed_id,
            quarantine_dir=self.quarantine_dir / safe_feed_id,
        )


# Segment metadata states (§6.5). The 4.B implementation only writes
# `complete` rows today; `dirty`/`corrupt`/`quarantined` are reserved for
# 4.E's startup scan.
SEGMENT_STATE_WRITING = "writing"
SEGMENT_STATE_COMPLETE = "complete"
SEGMENT_STATE_DIRTY = "dirty"
SEGMENT_STATE_CORRUPT = "corrupt"
SEGMENT_STATE_QUARANTINED = "quarantined"


# Phase 8.C: post-session export artifact bookkeeping. Rows in the
# `export_artifacts` SQLite table track every long-form encoding
# attempt so re-runs are idempotent (a `success` row for a given
# `(session_id, kind, game_subdir, feed_id)` makes the processor
# skip that artifact unless `--force` is passed). The only `kind`
# today is `long_form`; the post-session processor also writes a
# `plays.json` sidecar per game (Phase 8.D), but that's a single
# JSON file per game rather than a per-artifact row.
EXPORT_KIND_LONG_FORM = "long_form"
EXPORT_STATUS_SUCCESS = "success"
EXPORT_STATUS_FAILED = "failed"


@dataclass(slots=True, frozen=True)
class ExportArtifact:
    """One persisted export attempt.

    `game_subdir` and `feed_id` are nullable for future kinds that
    might span multiple feeds. Long-form artifacts always populate
    both.
    """

    session_id: str
    kind: str  # EXPORT_KIND_*
    output_path: str
    status: str  # EXPORT_STATUS_*
    started_at: str
    artifact_id: int | None = None  # set on insert by SQLite
    game_subdir: str | None = None
    feed_id: str | None = None
    error_message: str | None = None
    size_bytes: int | None = None
    duration_ns: int | None = None
    finalized_at: str | None = None


@dataclass(slots=True, frozen=True)
class Segment:
    """One closed recording segment, matching the §6.3 schema.

    Audio fields, `pts_to_session_offset_ns`, and the wall-clock pair are
    populated as they become available — slice 4.B captures what
    `splitmuxsink`'s `format-location` cycle gives us cleanly. Phase 5's
    `SessionClock` will fill in `start_session_time_ns` / `end_session_time_ns`.
    """

    session_id: str
    feed_id: str
    fragment_index: int
    file_path: str
    codec: str
    container: str
    start_pts_ns: int
    end_pts_ns: int
    duration_ns: int
    frame_count_estimate: int
    size_bytes: int
    state: str
    created_at: str
    finalized_at: str | None
    segment_id: int | None = None  # set on insert by SQLite
    start_session_time_ns: int | None = None
    end_session_time_ns: int | None = None
    pts_to_session_offset_ns: int | None = None
    start_wall_clock_utc: str | None = None
    end_wall_clock_utc: str | None = None
