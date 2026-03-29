"""SQLite wrapper for lightweight session metadata."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class MetadataDb:
    """Owns the SQLite database used for session metadata."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._connection: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Open the database connection and initialize the schema."""
        if self._connection is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self._db_path)
            self._initialize_schema()
        return self._connection

    def _initialize_schema(self) -> None:
        connection = self.connect()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                started_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    def create_session(self, session_id: str, source_name: str, started_at: str) -> None:
        """Insert a session row into the metadata database."""
        connection = self.connect()
        connection.execute(
            """
            INSERT INTO sessions (session_id, source_name, started_at)
            VALUES (?, ?, ?)
            """,
            (session_id, source_name, started_at),
        )
        connection.commit()

    def close(self) -> None:
        """Close the active database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
