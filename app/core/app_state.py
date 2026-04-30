"""Per-output UI state surfaced by `PlaybackController` and rendered in Qt.

This dataclass is *not* the top-level application state machine — that is
the `AppState` enum in `app.core.application_state` (§10.1). The dataclass
is named `UiState` to keep the `AppState` name available for the enum.
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
    # Phase 6: feed_ids whose tile is currently rendering a clamped
    # freeze frame (per §8.6.1) instead of an exact-coverage frame.
    # Empty in LIVE mode and during normal in-coverage replay. Drives
    # the per-tile "FROZEN" badge surfacing in the operator UI.
    feeds_in_freeze_frame: tuple[str, ...] = ()
    # Phase 7.B: replay-availability surfaces.
    #
    # `latest_replayable_session_time_ns` is the cross-feed latest
    # finalized segment end in session-time. None when no segments have
    # finalized yet. Already computed for the rewind anchor; exposed
    # here so the status bar / diagnostics can render it without
    # reaching back into the replay store.
    #
    # `live_lag_behind_replayable_seconds` is the distance between the
    # session clock's "now" and `latest_replayable_session_time_ns`.
    # Hovers near `recording_segment_duration_seconds` while recording
    # is healthy (the in-progress segment hasn't finalized yet);
    # growing unboundedly = a wedged splitmuxsink.
    #
    # `replay_available` is False until the first segment finalizes —
    # covers the "operator just started recording, no replay yet" UX
    # gap.
    latest_replayable_session_time_ns: int | None = None
    live_lag_behind_replayable_seconds: float = 0.0
    replay_available: bool = False
    # Phase 7.H.3: currently-open play number, scoped per game.
    # None when no game is being recorded. Populated by
    # `PlaybackController._update_state_timestamps_locked` from
    # `PlayManager.current_play_number()`.
    current_play_number: int | None = None


