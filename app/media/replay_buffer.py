"""Rolling replay buffer service."""

from __future__ import annotations

from app.core.models import SessionPaths


class ReplayBuffer:
    """Tracks replay buffer state independently of recording and preview."""

    def __init__(self, buffer_duration_seconds: int = 120) -> None:
        self._buffer_duration_seconds = buffer_duration_seconds
        self._session_paths: SessionPaths | None = None
        self._is_running = False
        self._seconds_behind_live = 0.0

    @property
    def buffer_duration_seconds(self) -> int:
        """Return the configured rolling buffer length."""
        return self._buffer_duration_seconds

    def start(self, session_paths: SessionPaths) -> None:
        """Prepare rolling buffer storage for the active session."""
        # TODO: Segment media to rolling storage and prune stale content.
        self._session_paths = session_paths
        self._is_running = True
        self._seconds_behind_live = 0.0

    def stop(self) -> None:
        """Stop rolling buffer updates."""
        self._is_running = False

    def is_running(self) -> bool:
        """Return whether rolling replay buffering is active."""
        return self._is_running

    def seek_seconds_behind_live(self, seconds: float) -> float:
        """Clamp and store the current replay offset from live."""
        clamped_seconds = max(0.0, min(seconds, float(self._buffer_duration_seconds)))
        self._seconds_behind_live = clamped_seconds
        return self._seconds_behind_live

    def jump_to_live(self) -> None:
        """Reset replay playback back to the live edge."""
        self._seconds_behind_live = 0.0

    def get_seconds_behind_live(self) -> float:
        """Return the currently selected replay offset."""
        return self._seconds_behind_live
