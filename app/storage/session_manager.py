"""Session orchestration for filesystem and metadata initialization."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.models import SessionPaths
from app.core.session_state import (
    SESSION_MANIFEST_FILENAME,
    SessionManifest,
    SessionState,
    make_session_state_machine,
)
from app.core.state_machine import StateMachine
from app.storage.file_manager import FileManager
from app.storage.metadata_db import MetadataDb


class SessionManager:
    """Creates and tracks the active replay session and its persisted state."""

    def __init__(self, file_manager: FileManager, metadata_db: MetadataDb) -> None:
        self._file_manager = file_manager
        self._metadata_db = metadata_db
        self._active_session_paths: SessionPaths | None = None
        self._active_session_state: StateMachine[SessionState] | None = None

    def start_new_session(self, source_name: str) -> SessionPaths:
        """Create a new session folder layout, manifest, and database record."""
        session_id = self._file_manager.get_next_session_id()
        session_paths = self._file_manager.create_session_paths(session_id)
        self._metadata_db.create_session(
            session_id=session_id,
            source_name=source_name,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        manifest = SessionManifest(session_paths.root_dir / SESSION_MANIFEST_FILENAME)
        self._active_session_paths = session_paths
        self._active_session_state = make_session_state_machine(
            session_id=session_id,
            manifest=manifest,
        )
        return session_paths

    def adopt_session(self, session_id: str) -> SessionPaths:
        """Adopt an existing on-disk session as the active one (§11.4 Resume).

        Used when the operator picks **Resume** in the recovery prompt.
        The session must already exist on disk (its `session.json`,
        recording subtree, and SQLite `sessions` row are all expected to
        be present). Unlike `start_new_session`, this:

        - does not allocate a new `session_id`
        - does not create directories
        - does not insert a `sessions` row
        - starts the state machine at `DIRTY` (matching the on-disk state
          left by `mark_dirty_sessions`) and immediately transitions to
          `CREATED` so the operator can hit Start Game Recording and
          drive `CREATED → RECORDING` like a fresh session.

        Returns the same `SessionPaths` shape as `start_new_session`,
        pointing at the existing directory tree.
        """
        from app.core.session_state import SessionState  # local import — avoids circular at module import time

        session_paths = self._file_manager.session_paths_for_existing(session_id)
        manifest = SessionManifest(session_paths.root_dir / SESSION_MANIFEST_FILENAME)
        existing = manifest.read() or {}
        created_at = existing.get("created_at") or datetime.now(timezone.utc).isoformat()
        self._active_session_paths = session_paths
        self._active_session_state = make_session_state_machine(
            session_id=session_id,
            manifest=manifest,
            created_at=str(created_at),
            initial_state=SessionState.DIRTY,
        )
        # DIRTY → CREATED so the rest of the app (which expects to drive
        # CREATED → RECORDING via toggle_long_session_recording) sees a
        # familiar starting state.
        self._active_session_state.transition_to(SessionState.CREATED)
        return session_paths

    def get_active_session_paths(self) -> SessionPaths | None:
        """Return the current active session, if one exists."""
        return self._active_session_paths

    def get_active_session_state(self) -> StateMachine[SessionState] | None:
        """Return the active session's state machine, if any."""
        return self._active_session_state

    def get_metadata_db(self) -> MetadataDb:
        """Return the underlying SQLite metadata DB.

        Slice 4.B threads this into per-feed `PipelineManager` so segment
        rows can be persisted as their splitmuxsink rotates files.
        """
        return self._metadata_db

    def close(self) -> None:
        """Release persistence resources and finalize the active session."""
        if self._active_session_state is not None:
            current = self._active_session_state.state
            if current == SessionState.RECORDING:
                self._active_session_state.transition_to(SessionState.STOPPED)
            if self._active_session_state.state == SessionState.STOPPED:
                self._active_session_state.transition_to(SessionState.FINALIZED)
            elif self._active_session_state.state == SessionState.CREATED:
                self._active_session_state.transition_to(SessionState.FINALIZED)
            self._active_session_state = None
        self._active_session_paths = None
        self._metadata_db.close()
