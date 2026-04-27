"""Per-output UI state surfaced by `PlaybackController` and rendered in Qt.

This dataclass is *not* the top-level application state machine — that is
the `AppState` enum in §10.1, scheduled for slice 2.D. To keep the namespace
clean for that enum, the dataclass is named `UiState`. The legacy
`AppState` symbol remains as a deprecated alias so nothing external breaks
during the transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.models import FrameOverlayInfo, IngestTelemetry, PlaybackMode, PlaybackOverlayInfo


@dataclass(slots=True)
class UiState:
    """Mutable per-output state that the UI renders."""

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
    ingest_telemetry: IngestTelemetry | None = None
    frame_overlay: FrameOverlayInfo = field(default_factory=FrameOverlayInfo)
    playback_overlay: PlaybackOverlayInfo = field(default_factory=PlaybackOverlayInfo)


# Deprecated alias so ad-hoc importers do not break mid-transition.
# Slice 2.D re-binds `AppState` to the §10.1 enum; remove this alias then.
AppState = UiState
