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

    def test_unique_constraint_on_fragment_index(self) -> None:
        with TemporaryDirectory() as tmp:
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="s1",
                source_name="NDI",
                started_at="2026-04-28T00:00:00+00:00",
            )
            db.insert_segment(_make_segment(fragment_index=5))
            with self.assertRaises(Exception):
                db.insert_segment(_make_segment(fragment_index=5))
            db.close()


if __name__ == "__main__":
    unittest.main()
