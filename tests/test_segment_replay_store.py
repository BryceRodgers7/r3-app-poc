"""Tests for `RecordingSegmentReplayStore` (slice 4.C data layer)."""

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


class ReplayStoreEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.idx = SegmentIndex()
        self.store = RecordingSegmentReplayStore(self.idx)

    def test_replay_unavailable_when_not_recording(self) -> None:
        for state in (
            RecordingState.NOT_RECORDING,
            RecordingState.STARTING_RECORDING,
            RecordingState.STOPPING_RECORDING,
            RecordingState.FINALIZING,
            RecordingState.RECORDING_ERROR,
        ):
            with self.subTest(recording_state=state):
                self.assertFalse(
                    self.store.is_replay_available(recording_state=state)
                )

    def test_replay_available_when_recording(self) -> None:
        self.assertTrue(
            self.store.is_replay_available(recording_state=RecordingState.RECORDING)
        )

    def test_resolve_returns_none_when_not_recording_even_if_segment_exists(self) -> None:
        self.idx.add(_seg(start_pts_ns=0, end_pts_ns=4_000_000_000))
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=2_000_000_000,
            recording_state=RecordingState.NOT_RECORDING,
        )
        self.assertIsNone(result)


class ReplayStoreResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.idx = SegmentIndex()
        self.store = RecordingSegmentReplayStore(self.idx)
        # Three back-to-back 4-second segments: [0, 4) [4, 8) [8, 12).
        self.idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        self.idx.add(_seg(fragment_index=1, start_pts_ns=4_000_000_000, end_pts_ns=8_000_000_000))
        self.idx.add(_seg(fragment_index=2, start_pts_ns=8_000_000_000, end_pts_ns=12_000_000_000))

    def test_resolve_to_first_segment(self) -> None:
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=2_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.segment.fragment_index, 0)
        self.assertEqual(result.offset_in_segment_ns, 2_000_000_000)

    def test_resolve_to_middle_segment_offset(self) -> None:
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=5_500_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert result is not None
        self.assertEqual(result.segment.fragment_index, 1)
        # 5.5s - 4s segment-start = 1.5s offset.
        self.assertEqual(result.offset_in_segment_ns, 1_500_000_000)

    def test_resolve_at_segment_boundary_picks_segment_starting_there(self) -> None:
        # PTS == 4s falls on the boundary between segment 0 (end_pts=4s) and
        # segment 1 (start_pts=4s). Both overlap the point. The store
        # returns the earlier-starting one (segment 0) per
        # `segments_overlapping`'s sort order.
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=4_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        assert result is not None
        self.assertEqual(result.segment.fragment_index, 0)

    def test_resolve_returns_none_before_earliest(self) -> None:
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=-1_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_resolve_returns_none_after_latest(self) -> None:
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=20_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_resolve_excludes_in_progress_segment(self) -> None:
        # Add a 4th segment in `writing` state; its PTS span includes the
        # target, but it should be excluded.
        self.idx.add(
            _seg(
                fragment_index=3,
                start_pts_ns=12_000_000_000,
                end_pts_ns=16_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        result = self.store.resolve(
            feed_id="ndi_main",
            target_pts_ns=14_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_resolve_returns_none_for_unknown_feed(self) -> None:
        result = self.store.resolve(
            feed_id="not_a_feed",
            target_pts_ns=2_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)


class ReplayStoreRangeTests(unittest.TestCase):
    def test_available_range_with_only_complete_segments(self) -> None:
        idx = SegmentIndex()
        idx.add(_seg(fragment_index=0, start_pts_ns=0, end_pts_ns=4_000_000_000))
        idx.add(_seg(fragment_index=1, start_pts_ns=4_000_000_000, end_pts_ns=8_000_000_000))
        store = RecordingSegmentReplayStore(idx)
        earliest, latest = store.available_pts_range("ndi_main")
        self.assertEqual(earliest, 0)
        self.assertEqual(latest, 8_000_000_000)

    def test_available_range_excludes_in_progress_from_latest(self) -> None:
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
        store = RecordingSegmentReplayStore(idx)
        earliest, latest = store.available_pts_range("ndi_main")
        # Earliest counts the first segment regardless of state.
        self.assertEqual(earliest, 0)
        # Latest only counts complete segments.
        self.assertEqual(latest, 4_000_000_000)

    def test_available_range_for_unknown_feed_is_none_pair(self) -> None:
        store = RecordingSegmentReplayStore(SegmentIndex())
        earliest, latest = store.available_pts_range("missing")
        self.assertIsNone(earliest)
        self.assertIsNone(latest)


if __name__ == "__main__":
    unittest.main()
