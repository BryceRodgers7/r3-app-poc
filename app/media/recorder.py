"""Full-session recording service."""

from __future__ import annotations

from pathlib import Path

from app.core.models import SessionPaths


class Recorder:
    """Tracks full-session recording state independently of playback."""

    def __init__(self) -> None:
        self._session_paths: SessionPaths | None = None
        self._is_recording = False

    def start(self, session_paths: SessionPaths) -> None:
        """Prepare the recorder for a new session."""
        # TODO: Attach the recording branch of the GStreamer pipeline here.
        self._session_paths = session_paths
        self._is_recording = True

    def stop(self) -> None:
        """Stop recording while preserving session metadata."""
        self._is_recording = False

    def is_recording(self) -> bool:
        """Return whether the recorder is active."""
        return self._is_recording

    def get_recording_target(self) -> Path | None:
        """Return the directory where full-session media should be written."""
        if self._session_paths is None:
            return None
        return self._session_paths.recording_dir
