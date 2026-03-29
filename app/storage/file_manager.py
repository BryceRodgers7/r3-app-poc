"""Filesystem helpers for session directories and file locations."""

from __future__ import annotations

from pathlib import Path

from app.config.settings import AppSettings
from app.core.models import SessionPaths


class FileManager:
    """Creates and resolves directories used by replay sessions."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def ensure_base_directories(self) -> None:
        """Create the top-level application data directory tree."""
        self._settings.base_data_dir.mkdir(parents=True, exist_ok=True)
        self._settings.sessions_root.mkdir(parents=True, exist_ok=True)

    def get_next_session_id(self) -> str:
        """Return the next monotonically increasing session identifier."""
        self.ensure_base_directories()
        existing_ids: list[int] = []

        for directory in self._settings.sessions_root.iterdir():
            if directory.is_dir() and directory.name.startswith("session_"):
                suffix = directory.name.removeprefix("session_")
                if suffix.isdigit():
                    existing_ids.append(int(suffix))

        next_value = max(existing_ids, default=0) + 1
        return f"session_{next_value:03d}"

    def create_session_paths(self, session_id: str) -> SessionPaths:
        """Create and return the folder layout for a new session."""
        self.ensure_base_directories()
        root_dir = self._settings.sessions_root / session_id
        recording_dir = root_dir / "recording"
        rolling_dir = root_dir / "rolling"
        clips_dir = root_dir / "clips"

        for path in (root_dir, recording_dir, rolling_dir, clips_dir):
            path.mkdir(parents=True, exist_ok=True)

        return SessionPaths(
            session_id=session_id,
            root_dir=root_dir,
            recording_dir=recording_dir,
            rolling_dir=rolling_dir,
            clips_dir=clips_dir,
        )

    def get_recording_manifest_path(self, session_paths: SessionPaths) -> Path:
        """Return a placeholder location for future recording metadata."""
        return session_paths.recording_dir / "recording_manifest.json"
