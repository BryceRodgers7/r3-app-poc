"""Tests for the per-game scope filter on `RecordingSegmentReplayStore`.

Phase 7.B-ext: when the operator stops one game and starts another in
the same session, replay queries must only see the current game's
segments. The store's `set_current_game_start_session_time` filter
enforces this.
"""

from __future__ import annotations

import unittest

from app.core.models import SEGMENT_STATE_COMPLETE, Segment
from app.core.recording_state import RecordingState
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import RecordingSegmentReplayStore


def _seg(
    *,
    feed_id: str = "feed_main",
    fragment_index: int = 0,
    start_session_time_ns: int,
    duration_ns: int = 4_000_000_000,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    """Build a Segment with session-time populated.

    PTS-time fields default to mirroring session-time (offset 0). The
    per-game filter only inspects `start_session_time_ns`, so the PTS
    side doesn't need elaborate setup.
    """
    end_session_time_ns = start_session_time_ns + duration_ns
    return Segment(
        session_id="s1",
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=f"/tmp/{feed_id}/seg_{fragment_index:05d}.mkv",
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start_session_time_ns,
        end_pts_ns=end_session_time_ns,
        duration_ns=duration_ns,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at="2026-04-28T01:00:04+00:00",
        start_session_time_ns=start_session_time_ns,
        end_session_time_ns=end_session_time_ns,
        pts_to_session_offset_ns=0,
    )


class PerGameScopeFilterTests(unittest.TestCase):
    """Two-game fixture: game 1 spans session_time 0..20s, game 2 starts
    at session_time 60s. The operator sets the filter to 60s once game 2
    begins; replay queries must then exclude game 1 entirely.
    """

    def setUp(self) -> None:
        self.idx = SegmentIndex()
        # Game 1: five 4s segments at session_time 0..20.
        for i in range(5):
            self.idx.add(
                _seg(fragment_index=i, start_session_time_ns=i * 4_000_000_000)
            )
        # Game 2: three 4s segments at session_time 60..72.
        for i in range(3):
            self.idx.add(
                _seg(
                    fragment_index=10 + i,
                    start_session_time_ns=60_000_000_000 + i * 4_000_000_000,
                )
            )
        self.store = RecordingSegmentReplayStore(self.idx)
        self.GAME_2_START_NS = 60_000_000_000

    def test_no_filter_sees_all_segments(self) -> None:
        # Sanity: without a filter, the store sees all 8 segments
        # (0..20s plus 60..72s).
        earliest, latest = self.store.available_session_time_range()
        self.assertEqual(earliest, 0)
        self.assertEqual(latest, 72_000_000_000)

    def test_filter_clamps_range_to_current_game(self) -> None:
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        earliest, latest = self.store.available_session_time_range()
        self.assertEqual(earliest, 60_000_000_000)
        self.assertEqual(latest, 72_000_000_000)

    def test_filter_clamps_earliest_session_time(self) -> None:
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        self.assertEqual(
            self.store.earliest_session_time("feed_main"),
            60_000_000_000,
        )

    def test_filter_clamps_latest_replayable_session_time(self) -> None:
        # Setting filter past game 2 end → no eligible segments → None.
        self.store.set_current_game_start_session_time(80_000_000_000)
        self.assertIsNone(self.store.latest_replayable_session_time("feed_main"))
        # Setting to game 2 start → latest is game 2's last finalized end.
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        self.assertEqual(
            self.store.latest_replayable_session_time("feed_main"),
            72_000_000_000,
        )

    def test_resolve_session_time_skips_prior_game(self) -> None:
        # Target falls inside game 1's coverage (10s). With filter set,
        # the resolver must NOT return a game-1 segment.
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        result = self.store.resolve_session_time(
            feed_id="feed_main",
            target_session_time_ns=10_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_resolve_session_time_returns_current_game_segment(self) -> None:
        # Target inside the second game-2 segment (64..68s), away from
        # segment boundaries to avoid the first-of-overlap tie.
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        result = self.store.resolve_session_time(
            feed_id="feed_main",
            target_session_time_ns=66_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.segment.start_session_time_ns, 64_000_000_000)

    def test_nearest_frame_location_clamps_to_current_game_earliest(self) -> None:
        # Target before game 2 (i.e., inside game 1's range, 10s). With
        # filter set, the §8.6.1 rule should clamp to game 2's earliest
        # segment as a freeze frame, NOT pick a game-1 segment.
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        result = self.store.nearest_frame_location(
            feed_id="feed_main",
            session_time_ns=10_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.is_freeze)
        self.assertEqual(result.segment.start_session_time_ns, 60_000_000_000)

    def test_feeds_with_coverage_at_skips_prior_game(self) -> None:
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        # 10s falls in game 1; should return no feeds when filtered.
        self.assertEqual(
            self.store.feeds_with_coverage_at(10_000_000_000), []
        )
        # 64s falls in game 2; should still return the feed.
        self.assertEqual(
            self.store.feeds_with_coverage_at(64_000_000_000), ["feed_main"]
        )

    def test_clearing_filter_restores_full_visibility(self) -> None:
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        self.store.set_current_game_start_session_time(None)
        earliest, latest = self.store.available_session_time_range()
        self.assertEqual(earliest, 0)
        self.assertEqual(latest, 72_000_000_000)

    def test_filter_handles_segments_without_session_time(self) -> None:
        # Add a segment with start_session_time_ns=None (legacy / pre-5.A)
        # — it should be excluded by the filter (defensive: we can't
        # know whether it belongs to the current game).
        self.idx.add(
            Segment(
                session_id="s1",
                feed_id="feed_main",
                fragment_index=99,
                file_path="/tmp/legacy.mkv",
                codec="mjpeg",
                container="mkv",
                start_pts_ns=0,
                end_pts_ns=4_000_000_000,
                duration_ns=4_000_000_000,
                frame_count_estimate=120,
                size_bytes=5_000_000,
                state=SEGMENT_STATE_COMPLETE,
                created_at="2026-04-28T01:00:00+00:00",
                finalized_at="2026-04-28T01:00:04+00:00",
                start_session_time_ns=None,
                end_session_time_ns=None,
                pts_to_session_offset_ns=None,
            )
        )
        self.store.set_current_game_start_session_time(self.GAME_2_START_NS)
        earliest, latest = self.store.available_session_time_range()
        # Range should still be game 2 only — the legacy segment is
        # excluded.
        self.assertEqual(earliest, 60_000_000_000)
        self.assertEqual(latest, 72_000_000_000)


if __name__ == "__main__":
    unittest.main()
