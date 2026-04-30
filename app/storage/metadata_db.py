"""SQLite wrapper for lightweight session and segment metadata."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from app.core.models import EXPORT_STATUS_SUCCESS, ExportArtifact, Segment

LOGGER = logging.getLogger(__name__)


# Phase 7.F: column list shared between the migration's CREATE and INSERT.
# Listing columns explicitly (rather than `INSERT INTO ... SELECT *`) means
# adding a future column to the schema doesn't silently break the migration.
_SEGMENT_COLUMNS = (
    "segment_id",
    "session_id",
    "feed_id",
    "fragment_index",
    "file_path",
    "codec",
    "container",
    "start_pts_ns",
    "end_pts_ns",
    "duration_ns",
    "frame_count_estimate",
    "size_bytes",
    "state",
    "created_at",
    "finalized_at",
    "start_session_time_ns",
    "end_session_time_ns",
    "pts_to_session_offset_ns",
    "start_wall_clock_utc",
    "end_wall_clock_utc",
)

# Phase 7.F: per-game folder layout (Phase 4.E) plus per-game fragment_index
# reset (Phase 7.B-ext) means file_path is the natural unique key — splitmuxsink
# generates a fresh path on each rotation. The previous constraint forbade
# duplicate (session_id, feed_id, fragment_index) tuples, which broke the
# moment two games per session shared a fragment_index. The migration in
# `_migrate_segments_unique_constraint_locked` rewrites pre-Phase-7.F DBs.
_SEGMENTS_TABLE_BODY = """
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
    UNIQUE(file_path)
"""


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
        # Phase 7.F: migrate any pre-existing segments table from the
        # legacy UNIQUE(session_id, feed_id, fragment_index) constraint
        # to the new UNIQUE(file_path) constraint BEFORE the
        # CREATE TABLE IF NOT EXISTS below — otherwise IF NOT EXISTS
        # would no-op on the legacy schema and leave the bad
        # constraint in place.
        self._migrate_segments_unique_constraint_locked(connection)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS segments ({_SEGMENTS_TABLE_BODY})"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS segments_by_feed_pts "
            "ON segments(session_id, feed_id, start_pts_ns)"
        )
        # Phase 8.C: per-export bookkeeping. Existing DBs gain the
        # table on next connect (CREATE IF NOT EXISTS — no migration
        # of data needed since failure means no rows exist yet).
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS export_artifacts (
                artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                game_subdir TEXT,
                feed_id TEXT,
                output_path TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                size_bytes INTEGER,
                duration_ns INTEGER,
                started_at TEXT NOT NULL,
                finalized_at TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS export_artifacts_by_session "
            "ON export_artifacts(session_id, status)"
        )
        connection.commit()

    def _migrate_segments_unique_constraint_locked(
        self, connection: sqlite3.Connection
    ) -> None:
        """Phase 7.F: rewrite the segments table with `UNIQUE(file_path)`.

        Phase 7.B-ext made `fragment_index` reset to 0 each game (per-game
        folder layout makes filenames unique even with reset indices),
        but the schema still forbade duplicate
        `(session_id, feed_id, fragment_index)` tuples. The first
        multi-game session on the new code would silently drop every
        segment after the first game's last fragment_index.

        Detection looks for the literal substring of the legacy
        constraint inside the table's stored CREATE statement. This is
        good enough — the only way that substring shows up is the
        legacy schema. Migration uses the standard SQLite
        rename + recreate + copy + drop dance inside a transaction so
        a crash mid-migration doesn't leave the DB inconsistent.
        """
        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='segments'"
        ).fetchone()
        if row is None:
            return  # fresh DB; CREATE TABLE below will use the new schema
        sql_normalized = "".join((row["sql"] or "").lower().split())
        if "unique(session_id,feed_id,fragment_index)" not in sql_normalized:
            return  # already on the new schema
        LOGGER.info(
            "migrating segments table from legacy UNIQUE constraint "
            "(Phase 7.F schema fix)"
        )
        try:
            connection.execute("BEGIN")
            # Drop indexes that reference the old table — they'll be
            # recreated by `_initialize_schema` after migration completes.
            connection.execute("DROP INDEX IF EXISTS segments_by_feed_pts")
            connection.execute(
                "ALTER TABLE segments RENAME TO segments_legacy_pre_per_game"
            )
            connection.execute(
                f"CREATE TABLE segments ({_SEGMENTS_TABLE_BODY})"
            )
            columns_csv = ", ".join(_SEGMENT_COLUMNS)
            connection.execute(
                f"INSERT INTO segments ({columns_csv}) "
                f"SELECT {columns_csv} FROM segments_legacy_pre_per_game"
            )
            connection.execute("DROP TABLE segments_legacy_pre_per_game")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        LOGGER.info("segments table migration complete")

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

    def get_segment_by_path(self, file_path: str) -> Segment | None:
        """Return the segment row whose `file_path` matches, or `None`.

        Slice 4.E uses this so the startup recovery pass can correlate
        on-disk segment files with their persisted metadata.
        """
        connection = self.connect()
        with self._write_lock:
            row = connection.execute(
                "SELECT * FROM segments WHERE file_path = ? LIMIT 1",
                (file_path,),
            ).fetchone()
        return _row_to_segment(row) if row is not None else None

    def update_segment_state(self, segment_id: int, state: str) -> None:
        """Mutate the `state` column for a segment row (slice 4.E recovery)."""
        connection = self.connect()
        with self._write_lock:
            connection.execute(
                "UPDATE segments SET state = ? WHERE segment_id = ?",
                (state, segment_id),
            )
            connection.commit()

    def update_segment_file_path(self, segment_id: int, file_path: str) -> None:
        """Mutate the `file_path` column for a segment row.

        Slice 4.E: when a corrupt file is moved into the quarantine
        subtree, we update the row to point at the new location so
        replay queries don't return a stale path.
        """
        connection = self.connect()
        with self._write_lock:
            connection.execute(
                "UPDATE segments SET file_path = ? WHERE segment_id = ?",
                (file_path, segment_id),
            )
            connection.commit()

    def all_session_ids(self) -> list[str]:
        """Return every recorded `session_id` in the database, oldest first."""
        connection = self.connect()
        with self._write_lock:
            rows = connection.execute(
                "SELECT session_id FROM sessions ORDER BY started_at"
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    # ----------------------------------------------------------------
    # Phase 8.C: export_artifacts table CRUD
    # ----------------------------------------------------------------

    def insert_export_artifact(self, artifact: ExportArtifact) -> int:
        """Insert an `ExportArtifact` row and return its `artifact_id`."""
        connection = self.connect()
        with self._write_lock:
            cursor = connection.execute(
                """
                INSERT INTO export_artifacts (
                    session_id, kind, game_subdir, feed_id, output_path,
                    status, error_message, size_bytes, duration_ns,
                    started_at, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.session_id,
                    artifact.kind,
                    artifact.game_subdir,
                    artifact.feed_id,
                    artifact.output_path,
                    artifact.status,
                    artifact.error_message,
                    artifact.size_bytes,
                    artifact.duration_ns,
                    artifact.started_at,
                    artifact.finalized_at,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def export_artifacts_for_session(
        self, session_id: str
    ) -> list[ExportArtifact]:
        """Return every export-artifact row for `session_id`, oldest first."""
        connection = self.connect()
        with self._write_lock:
            rows = connection.execute(
                "SELECT * FROM export_artifacts WHERE session_id = ? "
                "ORDER BY artifact_id",
                (session_id,),
            ).fetchall()
        return [_row_to_export_artifact(row) for row in rows]

    def successful_artifact_keys(
        self, session_id: str
    ) -> set[tuple[str, str | None, str | None]]:
        """Return `(kind, game_subdir, feed_id)` triples that already have
        at least one successful export row for `session_id`.

        Phase 8.C idempotency: the post-session processor uses this to
        skip re-encoding artifacts that already succeeded on a prior
        run. `--force` overrides by ignoring this set.
        """
        connection = self.connect()
        with self._write_lock:
            rows = connection.execute(
                """
                SELECT DISTINCT kind, game_subdir, feed_id
                FROM export_artifacts
                WHERE session_id = ? AND status = ?
                """,
                (session_id, EXPORT_STATUS_SUCCESS),
            ).fetchall()
        return {
            (
                str(row["kind"]),
                row["game_subdir"],
                row["feed_id"],
            )
            for row in rows
        }

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


def _row_to_export_artifact(row: sqlite3.Row) -> ExportArtifact:
    return ExportArtifact(
        artifact_id=int(row["artifact_id"]),
        session_id=str(row["session_id"]),
        kind=str(row["kind"]),
        game_subdir=row["game_subdir"],
        feed_id=row["feed_id"],
        output_path=str(row["output_path"]),
        status=str(row["status"]),
        error_message=row["error_message"],
        size_bytes=row["size_bytes"],
        duration_ns=row["duration_ns"],
        started_at=str(row["started_at"]),
        finalized_at=row["finalized_at"],
    )
