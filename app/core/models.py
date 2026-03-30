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

    @property
    def image_bgr(self) -> FrameArray:
        """Return the current OpenCV BGR payload."""
        return self.image


@dataclass(slots=True)
class SessionPaths:
    """Filesystem locations associated with a recording session."""

    session_id: str
    root_dir: Path
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path
