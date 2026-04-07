"""Application state models for UI and controller coordination."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.models import FrameOverlayInfo, PlaybackMode, PlaybackOverlayInfo


@dataclass(slots=True)
class AppState:
    """Mutable high-level state that the UI renders."""

    current_playback_mode: PlaybackMode = PlaybackMode.SOURCE_LOST
    is_recording: bool = False
    source_connected: bool = False
    seconds_behind_live: float = 0.0
    current_session_id: str | None = None
    current_source_name: str | None = None
    last_frame_timestamp: float | None = None
    replay_buffer_span_seconds: float = 0.0
    error_message: str | None = None
    warning_message: str | None = None
    frame_overlay: FrameOverlayInfo = field(default_factory=FrameOverlayInfo)
    playback_overlay: PlaybackOverlayInfo = field(default_factory=PlaybackOverlayInfo)
