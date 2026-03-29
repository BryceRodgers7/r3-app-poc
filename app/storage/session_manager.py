"""Session orchestration for filesystem and metadata initialization."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.models import SessionPaths
from app.storage.file_manager import FileManager
from app.storage.metadata_db import MetadataDb


class SessionManager:
    """Creates and tracks the active replay session."""

    def __init__(self, file_manager: FileManager, metadata_db: MetadataDb) -> None:
        self._file_manager = file_manager
        self._metadata_db = metadata_db
        self._active_session_paths: SessionPaths | None = None

    def start_new_session(self, source_name: str) -> SessionPaths:
        """Create a new session folder layout and database record."""
        session_id = self._file_manager.get_next_session_id()
        session_paths = self._file_manager.create_session_paths(session_id)
        self._metadata_db.create_session(
            session_id=session_id,
            source_name=source_name,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._active_session_paths = session_paths
        return session_paths

    def get_active_session_paths(self) -> SessionPaths | None:
        """Return the current active session, if one exists."""
        return self._active_session_paths

    def close(self) -> None:
        """Release persistence resources."""
        self._metadata_db.close()
