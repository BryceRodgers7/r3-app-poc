"""SQLite wrapper for lightweight session and segment metadata."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from app.core.models import Segment


class MetadataDb:
    """Owns the SQLite database used for session and segment metadata."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._connection: sqlite3.Connection | None = None
        # Inserts can come from the GStreamer streaming thread (segment
        # rotation via splitmuxsink's format-location callback) or from
        # the Qt main thread (recording stop). Serialize with a lock so
        # we can use a single shared connection.
        self._write_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        """Open the database connection and initialize the schema."""
        if self._connection is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            # `check_same_thread=False` lets the streaming thread reuse
            # the same connection; `_write_lock` keeps writes safe.
            self._connection = sqlite3.connect(
                self._db_path, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS segments (
                segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                feed_id TEXT NOT NULL,
                fragment_index INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                codec TEXT NOT NULL,
                container TEXT NOT NULL,
                start_pts_ns INTEGER NOT NULL,
                end_pts_ns INTEGER NOT NULL,
                duration_ns INTEGER NOT NULL,
                frame_count_estimate INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finalized_at TEXT,
                start_session_time_ns INTEGER,
                end_session_time_ns INTEGER,
                pts_to_session_offset_ns INTEGER,
                start_wall_clock_utc TEXT,
                end_wall_clock_utc TEXT,
                UNIQUE(session_id, feed_id, fragment_index)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS segments_by_feed_pts "
            "ON segments(session_id, feed_id, start_pts_ns)"
        )
        connection.commit()

    def create_session(self, session_id: str, source_name: str, started_at: str) -> None:
        """Insert a session row into the metadata database."""
        connection = self.connect()
        with self._write_lock:
            connection.execute(
                """
                INSERT INTO sessions (session_id, source_name, started_at)
                VALUES (?, ?, ?)
                """,
                (session_id, source_name, started_at),
            )
            connection.commit()

    def insert_segment(self, segment: Segment) -> int:
        """Insert a `Segment` row and return its assigned `segment_id`."""
        connection = self.connect()
        with self._write_lock:
            cursor = connection.execute(
                """
                INSERT INTO segments (
                    session_id, feed_id, fragment_index, file_path,
                    codec, container,
                    start_pts_ns, end_pts_ns, duration_ns,
                    frame_count_estimate, size_bytes, state,
                    created_at, finalized_at,
                    start_session_time_ns, end_session_time_ns,
                    pts_to_session_offset_ns,
                    start_wall_clock_utc, end_wall_clock_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment.session_id,
                    segment.feed_id,
                    segment.fragment_index,
                    segment.file_path,
                    segment.codec,
                    segment.container,
                    segment.start_pts_ns,
                    segment.end_pts_ns,
                    segment.duration_ns,
                    segment.frame_count_estimate,
                    segment.size_bytes,
                    segment.state,
                    segment.created_at,
                    segment.finalized_at,
                    segment.start_session_time_ns,
                    segment.end_session_time_ns,
                    segment.pts_to_session_offset_ns,
                    segment.start_wall_clock_utc,
                    segment.end_wall_clock_utc,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def segments_for_session(self, session_id: str) -> list[Segment]:
        """Return every segment belonging to `session_id`, ordered by feed + start_pts."""
        connection = self.connect()
        with self._write_lock:
            rows = connection.execute(
                "SELECT * FROM segments WHERE session_id = ? "
                "ORDER BY feed_id, start_pts_ns",
                (session_id,),
            ).fetchall()
        return [_row_to_segment(row) for row in rows]

    def segments_for_feed(self, session_id: str, feed_id: str) -> list[Segment]:
        """Return every segment for `(session_id, feed_id)`, ordered by start_pts."""
        connection = self.connect()
        with self._write_lock:
            rows = connection.execute(
                "SELECT * FROM segments WHERE session_id = ? AND feed_id = ? "
                "ORDER BY start_pts_ns",
                (session_id, feed_id),
            ).fetchall()
        return [_row_to_segment(row) for row in rows]

    def close(self) -> None:
        """Close the active database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _row_to_segment(row: sqlite3.Row) -> Segment:
    return Segment(
        segment_id=int(row["segment_id"]),
        session_id=str(row["session_id"]),
        feed_id=str(row["feed_id"]),
        fragment_index=int(row["fragment_index"]),
        file_path=str(row["file_path"]),
        codec=str(row["codec"]),
        container=str(row["container"]),
        start_pts_ns=int(row["start_pts_ns"]),
        end_pts_ns=int(row["end_pts_ns"]),
        duration_ns=int(row["duration_ns"]),
        frame_count_estimate=int(row["frame_count_estimate"]),
        size_bytes=int(row["size_bytes"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        finalized_at=row["finalized_at"],
        start_session_time_ns=row["start_session_time_ns"],
        end_session_time_ns=row["end_session_time_ns"],
        pts_to_session_offset_ns=row["pts_to_session_offset_ns"],
        start_wall_clock_utc=row["start_wall_clock_utc"],
        end_wall_clock_utc=row["end_wall_clock_utc"],
    )
