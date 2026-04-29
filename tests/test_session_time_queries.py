"""Tests for slice 5.B — session-time queries on `SegmentIndex` and
`RecordingSegmentReplayStore`, including the §8.6.1 catch-up rule."""

from __future__ import annotations

import unittest

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_WRITING,
    Segment,
)
from app.core.recording_state import RecordingState
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import (
    RecordingSegmentReplayStore,
    SegmentReplayLocation,
)


def _seg(
    *,
    feed_id: str,
    fragment_index: int,
    start_pts_ns: int,
    end_pts_ns: int,
    pts_to_session_offset_ns: int | None = 0,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    """Build a `Segment` row.

    `pts_to_session_offset_ns=None` simulates a pre-5.A segment
    (session-time fields all NULL) — used to confirm those rows are
    silently skipped from session-time queries while remaining
    queryable by PTS time.
    """
    if pts_to_session_offset_ns is None:
        start_session = None
        end_session = None
        offset = None
    else:
        offset = pts_to_session_offset_ns
        start_session = start_pts_ns + offset
        end_session = end_pts_ns + offset
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
        start_session_time_ns=start_session,
        end_session_time_ns=end_session,
        pts_to_session_offset_ns=offset,
    )


class SegmentIndexSessionTimeTests(unittest.TestCase):
    """Direct queries against the index — no replay store wrapper."""

    def setUp(self) -> None:
        self.idx = SegmentIndex()
        # Feed A: session_time 0..20 (5 segments × 4s, offset = 0).
        for i in range(5):
            self.idx.add(
                _seg(
                    feed_id="ndi_a",
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    end_pts_ns=(i + 1) * 4_000_000_000,
                    pts_to_session_offset_ns=0,
                )
            )
        # Feed B: joined at session_time = 5s. PTS starts at 0 for that
        # feed, but offset is 5_000_000_000 so session-time spans 5..15
        # (3 segments × 4s = 12 — but to keep the example clean, use 2.5).
        for i in range(3):
            self.idx.add(
                _seg(
                    feed_id="ndi_b",
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    end_pts_ns=(i + 1) * 4_000_000_000,
                    pts_to_session_offset_ns=5_000_000_000,
                )
            )

    def test_segments_overlapping_session_time_finds_match(self) -> None:
        # session_time = 6s should hit segment 1 of feed A (4..8) and
        # segment 0 of feed B (5..9, in session time).
        a_hits = self.idx.segments_overlapping_session_time(
            "ndi_a", 6_000_000_000, 6_000_000_000
        )
        self.assertEqual([s.fragment_index for s in a_hits], [1])
        b_hits = self.idx.segments_overlapping_session_time(
            "ndi_b", 6_000_000_000, 6_000_000_000
        )
        self.assertEqual([s.fragment_index for s in b_hits], [0])

    def test_segments_overlapping_session_time_skips_pre_5a_rows(self) -> None:
        idx = SegmentIndex()
        # A segment from before 5.A — no session-time fields.
        idx.add(
            _seg(
                feed_id="legacy",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                pts_to_session_offset_ns=None,
            )
        )
        result = idx.segments_overlapping_session_time(
            "legacy", 0, 4_000_000_000
        )
        self.assertEqual(result, [])

    def test_feeds_with_coverage_at(self) -> None:
        # session_time = 3s → only feed A.
        self.assertEqual(
            self.idx.feeds_with_coverage_at(3_000_000_000), ["ndi_a"]
        )
        # session_time = 6s → both feeds.
        self.assertEqual(
            sorted(self.idx.feeds_with_coverage_at(6_000_000_000)),
            ["ndi_a", "ndi_b"],
        )
        # session_time = 18s → only feed A (B ended at 17s).
        self.assertEqual(
            self.idx.feeds_with_coverage_at(18_000_000_000), ["ndi_a"]
        )
        # session_time = 25s → neither.
        self.assertEqual(self.idx.feeds_with_coverage_at(25_000_000_000), [])

    def test_feeds_with_coverage_at_excludes_writing(self) -> None:
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        self.assertEqual(idx.feeds_with_coverage_at(2_000_000_000), [])

    def test_earliest_and_latest_session_time(self) -> None:
        self.assertEqual(self.idx.earliest_session_time("ndi_a"), 0)
        self.assertEqual(self.idx.latest_replayable_session_time("ndi_a"), 20_000_000_000)
        self.assertEqual(self.idx.earliest_session_time("ndi_b"), 5_000_000_000)
        self.assertEqual(
            self.idx.latest_replayable_session_time("ndi_b"), 17_000_000_000
        )

    def test_cross_feed_session_time_range(self) -> None:
        earliest, latest = self.idx.cross_feed_session_time_range()
        # A starts at 0; B starts at 5. Earliest = 0.
        self.assertEqual(earliest, 0)
        # A ends at 20; B ends at 17. Latest = 20.
        self.assertEqual(latest, 20_000_000_000)

    def test_cross_feed_range_with_only_writing_segments(self) -> None:
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        earliest, latest = idx.cross_feed_session_time_range()
        # Earliest counts any segment regardless of state.
        self.assertEqual(earliest, 0)
        # Latest only counts complete segments.
        self.assertIsNone(latest)

    def test_cross_feed_range_returns_none_when_empty(self) -> None:
        idx = SegmentIndex()
        self.assertEqual(idx.cross_feed_session_time_range(), (None, None))


class ResolveSessionTimeTests(unittest.TestCase):
    """Strict session-time resolver."""

    def setUp(self) -> None:
        self.idx = SegmentIndex()
        # Single feed, 3 contiguous 4s segments at session-time 0..12.
        for i in range(3):
            self.idx.add(
                _seg(
                    feed_id="ndi_a",
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    end_pts_ns=(i + 1) * 4_000_000_000,
                    pts_to_session_offset_ns=0,
                )
            )
        self.store = RecordingSegmentReplayStore(self.idx)

    def test_resolves_to_correct_segment_and_offset(self) -> None:
        result = self.store.resolve_session_time(
            feed_id="ndi_a",
            target_session_time_ns=5_500_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert result is not None
        self.assertEqual(result.segment.fragment_index, 1)
        self.assertEqual(result.offset_in_segment_ns, 1_500_000_000)

    def test_returns_none_before_earliest(self) -> None:
        result = self.store.resolve_session_time(
            feed_id="ndi_a",
            target_session_time_ns=-1_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_returns_none_after_latest(self) -> None:
        result = self.store.resolve_session_time(
            feed_id="ndi_a",
            target_session_time_ns=20_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_returns_none_when_not_recording(self) -> None:
        result = self.store.resolve_session_time(
            feed_id="ndi_a",
            target_session_time_ns=5_000_000_000,
            recording_state=RecordingState.NOT_RECORDING,
        )
        self.assertIsNone(result)


class NearestFrameLocationTests(unittest.TestCase):
    """The §8.6.1 catch-up clamping rule."""

    def setUp(self) -> None:
        self.idx = SegmentIndex()
        self.store = RecordingSegmentReplayStore(self.idx)

    def _populate_two_feeds(self) -> None:
        # Feed A: session_time 0..20 contiguous.
        for i in range(5):
            self.idx.add(
                _seg(
                    feed_id="ndi_a",
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    end_pts_ns=(i + 1) * 4_000_000_000,
                    pts_to_session_offset_ns=0,
                )
            )
        # Feed B: session_time 5..17 (joined late).
        for i in range(3):
            self.idx.add(
                _seg(
                    feed_id="ndi_b",
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    end_pts_ns=(i + 1) * 4_000_000_000,
                    pts_to_session_offset_ns=5_000_000_000,
                )
            )

    def test_in_coverage_returns_exact_match(self) -> None:
        self._populate_two_feeds()
        loc = self.store.nearest_frame_location(
            feed_id="ndi_a",
            session_time_ns=10_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert loc is not None
        # 10s falls in fragment 2 (8..12), offset = 2s.
        self.assertEqual(loc.segment.fragment_index, 2)
        self.assertEqual(loc.offset_in_segment_ns, 2_000_000_000)

    def test_before_earliest_freezes_on_first_frame(self) -> None:
        """The §8.6.1 worked example: feed B joined at session_time=5,
        operator rewinds to session_time=2 → tile shows feed B's first
        frame frozen."""
        self._populate_two_feeds()
        loc = self.store.nearest_frame_location(
            feed_id="ndi_b",
            session_time_ns=2_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert loc is not None
        self.assertEqual(loc.segment.fragment_index, 0)
        self.assertEqual(loc.offset_in_segment_ns, 0)

    def test_advancing_clock_into_coverage_starts_playback(self) -> None:
        """The other half of the worked example: as the playback clock
        advances from 2→5, feed B transitions from freeze (offset 0)
        into in-coverage (offset 0, but now real)."""
        self._populate_two_feeds()
        # At session_time = 5 exactly, we're inside segment 0 (5..9 in
        # session-time space) at offset 0. Same segment, same offset
        # as the freeze state — but now naturally moving.
        loc = self.store.nearest_frame_location(
            feed_id="ndi_b",
            session_time_ns=5_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert loc is not None
        self.assertEqual(loc.segment.fragment_index, 0)
        self.assertEqual(loc.offset_in_segment_ns, 0)

    def test_after_latest_freezes_on_last_frame(self) -> None:
        self._populate_two_feeds()
        # session_time = 19 — past feed B's last segment (ended at 17).
        loc = self.store.nearest_frame_location(
            feed_id="ndi_b",
            session_time_ns=19_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert loc is not None
        # Should freeze on segment 2's last frame (segment 2 ended at 17).
        self.assertEqual(loc.segment.fragment_index, 2)
        # Offset = segment.duration_ns (= 4s).
        self.assertEqual(loc.offset_in_segment_ns, 4_000_000_000)

    def test_in_gap_freezes_on_last_segment_before_gap(self) -> None:
        # Build a feed with a gap: segments at session-time 0..4 and 10..14.
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                pts_to_session_offset_ns=0,
            )
        )
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=1,
                start_pts_ns=10_000_000_000,
                end_pts_ns=14_000_000_000,
                pts_to_session_offset_ns=0,
            )
        )
        store = RecordingSegmentReplayStore(idx)
        # session_time = 7 — in the gap between segment 0 (ended at 4)
        # and segment 1 (starts at 10).
        loc = store.nearest_frame_location(
            feed_id="ndi_a",
            session_time_ns=7_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert loc is not None
        # Should freeze on segment 0's last frame.
        self.assertEqual(loc.segment.fragment_index, 0)
        self.assertEqual(loc.offset_in_segment_ns, 4_000_000_000)

    def test_returns_none_when_feed_has_no_segments(self) -> None:
        self._populate_two_feeds()
        loc = self.store.nearest_frame_location(
            feed_id="ndi_unknown",
            session_time_ns=10_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(loc)

    def test_returns_none_when_not_recording(self) -> None:
        self._populate_two_feeds()
        loc = self.store.nearest_frame_location(
            feed_id="ndi_a",
            session_time_ns=10_000_000_000,
            recording_state=RecordingState.NOT_RECORDING,
        )
        self.assertIsNone(loc)

    def test_returns_none_when_only_writing_segments(self) -> None:
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        store = RecordingSegmentReplayStore(idx)
        loc = store.nearest_frame_location(
            feed_id="ndi_a",
            session_time_ns=2_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        # No completed segments yet — nothing to render even as a freeze.
        self.assertIsNone(loc)

    def test_returns_none_when_only_pre_5a_segments(self) -> None:
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                pts_to_session_offset_ns=None,
            )
        )
        store = RecordingSegmentReplayStore(idx)
        # Pre-5.A segments lack session-time fields; nearest_frame_location
        # has nothing to clamp against and returns None. PTS-time
        # queries (`resolve`) still work.
        loc = store.nearest_frame_location(
            feed_id="ndi_a",
            session_time_ns=2_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(loc)


class AvailableSessionTimeRangeTests(unittest.TestCase):
    def test_returns_cross_feed_bounds(self) -> None:
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                pts_to_session_offset_ns=0,
            )
        )
        idx.add(
            _seg(
                feed_id="ndi_b",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                pts_to_session_offset_ns=10_000_000_000,
            )
        )
        store = RecordingSegmentReplayStore(idx)
        earliest, latest = store.available_session_time_range()
        self.assertEqual(earliest, 0)
        self.assertEqual(latest, 14_000_000_000)


class FeedsWithCoverageAtTests(unittest.TestCase):
    def test_passes_through_to_index(self) -> None:
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_a",
                fragment_index=0,
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                pts_to_session_offset_ns=0,
            )
        )
        store = RecordingSegmentReplayStore(idx)
        self.assertEqual(store.feeds_with_coverage_at(2_000_000_000), ["ndi_a"])
        self.assertEqual(store.feeds_with_coverage_at(10_000_000_000), [])


if __name__ == "__main__":
    unittest.main()
