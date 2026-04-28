"""Tests for the in-memory `SegmentIndex` (slice 4.B)."""

from __future__ import annotations

import unittest

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_WRITING,
    Segment,
)
from app.storage.segment_index import SegmentIndex


def _seg(
    *,
    feed_id: str = "ndi_main",
    fragment_index: int = 0,
    start_pts_ns: int,
    end_pts_ns: int,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    return Segment(
        session_id="s1",
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=f"/tmp/{feed_id}/seg_{fragment_index:05d}.mkv",
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start_pts_ns,
        end_pts_ns=end_pts_ns,
        duration_ns=end_pts_ns - start_pts_ns,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at="2026-04-28T01:00:04+00:00",
    )


class SegmentIndexTests(unittest.TestCase):
    def test_empty_index_queries(self) -> None:
        idx = SegmentIndex()
        self.assertEqual(idx.all_for_feed("missing"), [])
        self.assertIsNone(idx.earliest_pts("missing"))
        self.assertIsNone(idx.latest_replayable_pts("missing"))
        self.assertEqual(idx.feed_ids(), [])

    def test_add_and_sort_by_pts(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(fragment_index=2, start_pts_ns=8_000_000_000, end_pts_ns=12_000_000_000))
        idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        idx.add(_seg(fragment_index=1, start_pts_ns=4_000_000_000, end_pts_ns=8_000_000_000))
        segs = idx.all_for_feed("ndi_main")
        self.assertEqual([s.start_pts_ns for s in segs], [0, 4_000_000_000, 8_000_000_000])

    def test_earliest_pts_returns_first_segment_start(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(fragment_index=0, start_pts_ns=4_000_000_000, end_pts_ns=8_000_000_000))
        idx.add(_seg(fragment_index=1, start_pts_ns=8_000_000_000, end_pts_ns=12_000_000_000))
        self.assertEqual(idx.earliest_pts("ndi_main"), 4_000_000_000)

    def test_latest_replayable_pts_only_counts_complete(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        idx.add(
            _seg(
                fragment_index=1,
                start_pts_ns=4_000_000_000,
                end_pts_ns=8_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        # Writing segment ignored; complete one's end_pts is what counts.
        self.assertEqual(idx.latest_replayable_pts("ndi_main"), 4_000_000_000)

    def test_overlapping_query_returns_segments_intersecting_range(self) -> None:
        idx = SegmentIndex()
        # Three back-to-back 4-second segments: [0..4), [4..8), [8..12).
        idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        idx.add(_seg(fragment_index=1, start_pts_ns=4_000_000_000, end_pts_ns=8_000_000_000))
        idx.add(_seg(fragment_index=2, start_pts_ns=8_000_000_000, end_pts_ns=12_000_000_000))

        # Query: 5s..7s → only middle segment.
        result = idx.segments_overlapping(
            "ndi_main", 5_000_000_000, 7_000_000_000
        )
        self.assertEqual([s.fragment_index for s in result], [1])

        # Query: 3s..9s → spans first two segments and into the third.
        result = idx.segments_overlapping(
            "ndi_main", 3_000_000_000, 9_000_000_000
        )
        self.assertEqual([s.fragment_index for s in result], [0, 1, 2])

        # Query: 100s..200s → no overlap.
        result = idx.segments_overlapping(
            "ndi_main", 100_000_000_000, 200_000_000_000
        )
        self.assertEqual(result, [])

        # Query: -10s..-1s → before earliest segment.
        result = idx.segments_overlapping("ndi_main", -10, -1)
        self.assertEqual(result, [])

    def test_overlapping_with_inverted_range_returns_empty(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        result = idx.segments_overlapping("ndi_main", 4_000_000_000, 0)
        self.assertEqual(result, [])

    def test_remove_by_path(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        target_path = idx.all_for_feed("ndi_main")[0].file_path
        self.assertTrue(idx.remove_by_path(target_path))
        self.assertEqual(idx.all_for_feed("ndi_main"), [])
        self.assertFalse(idx.remove_by_path(target_path))  # idempotent

    def test_per_feed_isolation(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(feed_id="cam_a", fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        idx.add(
            _seg(
                feed_id="cam_b",
                fragment_index=0,
                start_pts_ns=4_000_000_000,
                end_pts_ns=8_000_000_000,
            )
        )
        self.assertEqual(idx.earliest_pts("cam_a"), 0)
        self.assertEqual(idx.earliest_pts("cam_b"), 4_000_000_000)
        self.assertEqual(set(idx.feed_ids()), {"cam_a", "cam_b"})


if __name__ == "__main__":
    unittest.main()
