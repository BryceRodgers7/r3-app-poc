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
    """A timestamped video frame passed through the temporary media pipeline."""

    frame_id: int
    timestamp: float
    image_bgr: FrameArray
    source_name: str


@dataclass(slots=True)
class SessionPaths:
    """Filesystem locations associated with a recording session."""

    session_id: str
    root_dir: Path
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path
