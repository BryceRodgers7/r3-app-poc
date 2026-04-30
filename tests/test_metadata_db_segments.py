"""Tests for the `segments` table added to `MetadataDb` in slice 4.B."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core.models import SEGMENT_STATE_COMPLETE, Segment
from app.storage.metadata_db import MetadataDb


def _make_segment(
    *,
    session_id: str = "session_001",
    feed_id: str = "ndi_main",
    fragment_index: int = 0,
    file_path: str = "/tmp/seg.mkv",
    start_pts_ns: int = 0,
    end_pts_ns: int = 4_000_000_000,
    frame_count_estimate: int = 120,
    size_bytes: int = 5_000_000,
    state: str = SEGMENT_STATE_COMPLETE,
    created_at: str = "2026-04-28T01:00:00+00:00",
    finalized_at: str | None = "2026-04-28T01:00:04+00:00",
) -> Segment:
    return Segment(
        session_id=session_id,
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=file_path,
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start_pts_ns,
        end_pts_ns=end_pts_ns,
        duration_ns=end_pts_ns - start_pts_ns,
        frame_count_estimate=frame_count_estimate,
        size_bytes=size_bytes,
        state=state,
        created_at=created_at,
        finalized_at=finalized_at,
    )


class MetadataDbSegmentTests(unittest.TestCase):
    def test_insert_and_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="session_001",
                source_name="NDI",
                started_at="2026-04-28T00:00:00+00:00",
            )
            seg = _make_segment(fragment_index=0, file_path="/tmp/seg_0.mkv")
            seg_id = db.insert_segment(seg)
            self.assertGreater(seg_id, 0)

            rows = db.segments_for_session("session_001")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].fragment_index, 0)
            self.assertEqual(rows[0].file_path, "/tmp/seg_0.mkv")
            self.assertEqual(rows[0].start_pts_ns, 0)
            self.assertEqual(rows[0].end_pts_ns, 4_000_000_000)
            self.assertEqual(rows[0].state, SEGMENT_STATE_COMPLETE)
            db.close()

    def test_segments_for_feed_orders_by_pts(self) -> None:
        with TemporaryDirectory() as tmp:
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="s1",
                source_name="NDI",
                started_at="2026-04-28T00:00:00+00:00",
            )
            for idx, start in enumerate([8_000_000_000, 0, 4_000_000_000]):
                db.insert_segment(
                    _make_segment(
                        session_id="s1",
                        fragment_index=idx,
                        file_path=f"/tmp/seg_{idx}.mkv",
                        start_pts_ns=start,
                        end_pts_ns=start + 4_000_000_000,
                    )
                )
            rows = db.segments_for_feed("s1", "ndi_main")
            self.assertEqual([r.start_pts_ns for r in rows], [0, 4_000_000_000, 8_000_000_000])
            db.close()

    def test_duplicate_fragment_index_allowed_with_distinct_paths(self) -> None:
        # Phase 7.F: the legacy UNIQUE(session_id, feed_id, fragment_index)
        # constraint was replaced with UNIQUE(file_path). Two games in
        # the same session each starting at fragment_index=0 must now
        # both insert successfully.
        with TemporaryDirectory() as tmp:
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="session_001",
                source_name="NDI",
                started_at="2026-04-28T00:00:00+00:00",
            )
            db.insert_segment(
                _make_segment(
                    fragment_index=0,
                    file_path="/tmp/game_001/ndi_main/segment_00000.mkv",
                )
            )
            db.insert_segment(
                _make_segment(
                    fragment_index=0,
                    file_path="/tmp/game_002/ndi_main/segment_00000.mkv",
                )
            )
            rows = db.segments_for_feed("session_001", "ndi_main")
            self.assertEqual(len(rows), 2)
            db.close()

    def test_duplicate_file_path_rejected(self) -> None:
        # Phase 7.F: file_path is now the unique key. Re-inserting the
        # same path is an error.
        with TemporaryDirectory() as tmp:
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="session_001",
                source_name="NDI",
                started_at="2026-04-28T00:00:00+00:00",
            )
            db.insert_segment(
                _make_segment(fragment_index=0, file_path="/tmp/seg.mkv")
            )
            with self.assertRaises(Exception):
                db.insert_segment(
                    _make_segment(fragment_index=1, file_path="/tmp/seg.mkv")
                )
            db.close()


class SegmentsSchemaMigrationTests(unittest.TestCase):
    """Phase 7.F: pre-existing DBs created with the legacy
    UNIQUE(session_id, feed_id, fragment_index) constraint must be
    migrated in place to UNIQUE(file_path) without losing any rows.
    """

    def _create_legacy_segments_db(self, db_path: Path) -> None:
        """Build a SQLite DB with the pre-Phase-7.F schema."""
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    started_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS segments_by_feed_pts "
                "ON segments(session_id, feed_id, start_pts_ns)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                ("s1", "NDI", "2026-04-28T00:00:00+00:00"),
            )
            # Two pre-existing segments using monotonic fragment_index
            # (matches what pre-Phase-7.B-ext recordings wrote).
            for i in (0, 1):
                conn.execute(
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
                        "s1", "ndi_main", i, f"/tmp/old/seg_{i}.mkv",
                        "mjpeg", "mkv",
                        i * 4_000_000_000, (i + 1) * 4_000_000_000, 4_000_000_000,
                        120, 5_000_000, "complete",
                        "2026-04-28T01:00:00+00:00",
                        "2026-04-28T01:00:04+00:00",
                        None, None, None, None, None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _read_segments_create_sql(self, db_path: Path) -> str:
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='segments'"
            ).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

    def test_migration_replaces_legacy_constraint(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.db"
            self._create_legacy_segments_db(db_path)
            # Confirm the legacy constraint is present before migration.
            legacy_sql = "".join(self._read_segments_create_sql(db_path).lower().split())
            self.assertIn("unique(session_id,feed_id,fragment_index)", legacy_sql)

            # Open via MetadataDb — migration runs as a side effect of
            # `_initialize_schema` on first connect.
            db = MetadataDb(db_path)
            db.connect()
            try:
                migrated_sql = "".join(
                    self._read_segments_create_sql(db_path).lower().split()
                )
                # New constraint is in place.
                self.assertIn("unique(file_path)", migrated_sql)
                # Old constraint is gone.
                self.assertNotIn(
                    "unique(session_id,feed_id,fragment_index)", migrated_sql
                )
            finally:
                db.close()

    def test_migration_preserves_existing_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.db"
            self._create_legacy_segments_db(db_path)
            db = MetadataDb(db_path)
            try:
                rows = db.segments_for_session("s1")
                self.assertEqual(len(rows), 2)
                self.assertEqual([r.fragment_index for r in rows], [0, 1])
                self.assertEqual(
                    [r.file_path for r in rows],
                    ["/tmp/old/seg_0.mkv", "/tmp/old/seg_1.mkv"],
                )
            finally:
                db.close()

    def test_migration_then_per_game_inserts_succeed(self) -> None:
        # End-to-end: migrate a legacy DB, then insert two new segments
        # with the same fragment_index but different file_paths. Both
        # must succeed (the regression Phase 7.F fixes).
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.db"
            self._create_legacy_segments_db(db_path)
            db = MetadataDb(db_path)
            try:
                db.insert_segment(
                    _make_segment(
                        session_id="s1",
                        feed_id="ndi_2",
                        fragment_index=0,
                        file_path="/tmp/game_001/ndi_2/segment_00000.mkv",
                    )
                )
                db.insert_segment(
                    _make_segment(
                        session_id="s1",
                        feed_id="ndi_2",
                        fragment_index=0,
                        file_path="/tmp/game_002/ndi_2/segment_00000.mkv",
                    )
                )
                rows = db.segments_for_feed("s1", "ndi_2")
                self.assertEqual(len(rows), 2)
                # Both rows share fragment_index=0 — the new constraint
                # only forbids duplicate file_paths.
                self.assertEqual({r.fragment_index for r in rows}, {0})
            finally:
                db.close()

    def test_migration_is_idempotent(self) -> None:
        # Opening an already-migrated DB does NOT touch the segments
        # table again.
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.db"
            self._create_legacy_segments_db(db_path)
            db1 = MetadataDb(db_path)
            db1.connect()
            db1.close()
            sql_after_first = self._read_segments_create_sql(db_path)
            db2 = MetadataDb(db_path)
            db2.connect()
            db2.close()
            sql_after_second = self._read_segments_create_sql(db_path)
            self.assertEqual(sql_after_first, sql_after_second)


if __name__ == "__main__":
    unittest.main()
