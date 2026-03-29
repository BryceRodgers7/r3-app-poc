"""Shared core models used across the application."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PlaybackMode(str, Enum):
    """Modes that describe what the operator is currently viewing."""

    LIVE = "LIVE"
    PAUSED = "PAUSED"
    REPLAY = "REPLAY"
    SOURCE_LOST = "SOURCE_LOST"


@dataclass(slots=True)
class SessionPaths:
    """Filesystem locations associated with a recording session."""

    session_id: str
    root_dir: Path
    recording_dir: Path
    rolling_dir: Path
    clips_dir: Path
