"""Application state models for UI and controller coordination."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import PlaybackMode


@dataclass(slots=True)
class AppState:
    """Mutable high-level state that the UI renders."""

    current_playback_mode: PlaybackMode = PlaybackMode.SOURCE_LOST
    is_recording: bool = False
    source_connected: bool = False
    seconds_behind_live: float = 0.0
    current_session_id: str | None = None
    current_source_name: str | None = None
    error_message: str | None = None
