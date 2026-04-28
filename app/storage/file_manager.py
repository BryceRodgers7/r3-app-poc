"""Filesystem helpers for session directories and file locations."""

from __future__ import annotations

from pathlib import Path

from app.config.settings import AppSettings
from app.core.models import FeedPaths, SessionPaths


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
        """Create and return the folder layout for a new session.

        Slice 4.E: also computes the per-session `quarantine` directory
        under `<root>/quarantine`. The directory is *not* created here —
        the recovery code creates it on demand only when a corrupt
        segment is moved into it.
        """
        self.ensure_base_directories()
        root_dir = self._settings.sessions_root / session_id
        recording_dir = root_dir / "recording"
        rolling_dir = root_dir / "rolling"
        clips_dir = root_dir / "clips"
        quarantine_dir = root_dir / "quarantine"

        for path in (root_dir, recording_dir, rolling_dir, clips_dir):
            path.mkdir(parents=True, exist_ok=True)

        return SessionPaths(
            session_id=session_id,
            root_dir=root_dir,
            recording_dir=recording_dir,
            rolling_dir=rolling_dir,
            clips_dir=clips_dir,
            quarantine_dir=quarantine_dir,
        )

    def session_root(self, session_id: str) -> Path:
        """Return the on-disk root directory for an existing session."""
        return self._settings.sessions_root / session_id

    def session_paths_for_existing(self, session_id: str) -> SessionPaths:
        """Build a `SessionPaths` for a previously-created session.

        Used by recovery code that needs to address a session's
        directories without recreating them.
        """
        root_dir = self.session_root(session_id)
        return SessionPaths(
            session_id=session_id,
            root_dir=root_dir,
            recording_dir=root_dir / "recording",
            rolling_dir=root_dir / "rolling",
            clips_dir=root_dir / "clips",
            quarantine_dir=root_dir / "quarantine",
        )

    def ensure_feed_paths(self, session_paths: SessionPaths, feed_id: str) -> FeedPaths:
        """Create and return the per-feed folder layout inside a session."""
        feed_paths = session_paths.get_feed_paths(feed_id)
        for path in (feed_paths.recording_dir, feed_paths.rolling_dir, feed_paths.clips_dir):
            path.mkdir(parents=True, exist_ok=True)
        return feed_paths

    def get_recording_manifest_path(self, session_paths: SessionPaths) -> Path:
        """Return a placeholder location for future recording metadata."""
        return session_paths.recording_dir / "recording_manifest.json"
