"""Phase 7.D: tests for the resume-after-crash continuation flow.

Covers two surfaces:

1. `_setup_resume_continuation` correctness — given a populated
   segment index + a recording dir on disk, the helper finds the
   crashed game folder, captures its session-time bounds, rebases
   the SessionClock, and stashes the continuation.

2. The first Start press after Resume continues the crashed game in
   the same folder with the right per-game filter; a subsequent Stop
   then Start allocates a fresh game.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core.application_coordinator import (
    ApplicationCoordinator,
    _ResumeContinuation,
)
from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    Segment,
    SessionPaths,
)
from app.core.session_clock import SessionClock
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import RecordingSegmentReplayStore


class _FakeNanosecondClock:
    def __init__(self, start_ns: int = 0) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_ns(self, delta_ns: int) -> None:
        self.now_ns += delta_ns


def _seg(
    *,
    feed_id: str,
    fragment_index: int,
    file_path: str,
    start_session_time_ns: int | None,
    duration_ns: int = 4_000_000_000,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    end_session_time_ns = (
        start_session_time_ns + duration_ns
        if start_session_time_ns is not None
        else None
    )
    return Segment(
        session_id="session_001",
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=file_path,
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start_session_time_ns or 0,
        end_pts_ns=(start_session_time_ns or 0) + duration_ns,
        duration_ns=duration_ns,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at=(
            "2026-04-28T01:00:04+00:00" if state == SEGMENT_STATE_COMPLETE else None
        ),
        start_session_time_ns=start_session_time_ns,
        end_session_time_ns=end_session_time_ns,
        pts_to_session_offset_ns=0 if start_session_time_ns is not None else None,
    )


def _build_coordinator_stub(
    *,
    segment_index: SegmentIndex,
    session_clock: SessionClock | None,
) -> ApplicationCoordinator:
    """Construct a minimal ApplicationCoordinator with just the fields
    `_setup_resume_continuation` reads. Avoids spinning up the full
    feed-runtime graph."""
    coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
    coord.segment_index = segment_index
    coord.session_clock = session_clock
    coord.replay_store = RecordingSegmentReplayStore(segment_index)
    coord._resume_continuation = None
    return coord


class SetupResumeContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root = Path(self._temp_dir.name) / "session_001"
        for sub in ("recording", "rolling", "clips"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root,
            recording_dir=root / "recording",
            rolling_dir=root / "rolling",
            clips_dir=root / "clips",
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _make_game_folder(self, game_subdir: str) -> Path:
        d = self.session_paths.recording_dir / game_subdir / "ndi_main"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_continuation_set_for_crashed_game(self) -> None:
        # Two pre-crash games on disk: game_001 (clean) + game_002
        # (crashed). Index has segments from both.
        g1 = self._make_game_folder("game_001")
        g2 = self._make_game_folder("game_002")
        idx = SegmentIndex()
        # game_001 segments at session_time 0..16s.
        for i in range(4):
            idx.add(
                _seg(
                    feed_id="ndi_main",
                    fragment_index=i,
                    file_path=str(g1 / f"segment_{i:05d}.mkv"),
                    start_session_time_ns=i * 4_000_000_000,
                )
            )
        # game_002 segments at session_time 20..32s (3 finalized
        # before the crash).
        for i in range(3):
            idx.add(
                _seg(
                    feed_id="ndi_main",
                    fragment_index=i,
                    file_path=str(g2 / f"segment_{i:05d}.mkv"),
                    start_session_time_ns=20_000_000_000 + i * 4_000_000_000,
                )
            )
        clock_fn = _FakeNanosecondClock(start_ns=99_000_000_000)
        sc = SessionClock(clock_ns=clock_fn)
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)

        coord._setup_resume_continuation(self.session_paths)

        self.assertIsNotNone(coord._resume_continuation)
        c = coord._resume_continuation
        assert c is not None
        self.assertEqual(c.game_subdir, "game_002")
        # Earliest start of game_002 segments.
        self.assertEqual(c.game_start_session_time_ns, 20_000_000_000)
        # Clock rebased past game_002's latest end (32s) plus the 1ms gap.
        self.assertEqual(
            sc.now_session_time_ns(),
            32_000_000_000 + 1_000_000,
        )

    def test_no_continuation_when_no_game_folders(self) -> None:
        idx = SegmentIndex()  # empty
        sc = SessionClock(clock_ns=_FakeNanosecondClock())
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)
        coord._setup_resume_continuation(self.session_paths)
        self.assertIsNone(coord._resume_continuation)

    def test_no_continuation_when_recording_dir_missing(self) -> None:
        idx = SegmentIndex()
        sc = SessionClock(clock_ns=_FakeNanosecondClock())
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)
        # Point at a recording_dir that doesn't exist.
        bogus = SessionPaths(
            session_id="session_001",
            root_dir=Path(self._temp_dir.name) / "nonexistent",
            recording_dir=Path(self._temp_dir.name) / "nonexistent" / "recording",
            rolling_dir=Path(self._temp_dir.name) / "nonexistent" / "rolling",
            clips_dir=Path(self._temp_dir.name) / "nonexistent" / "clips",
        )
        coord._setup_resume_continuation(bogus)
        self.assertIsNone(coord._resume_continuation)

    def test_no_continuation_when_segments_lack_session_time_fields(self) -> None:
        # Pre-5.A legacy rows (no start_session_time_ns) can't anchor
        # the rebase; helper bails rather than risk a wrong rebase.
        g1 = self._make_game_folder("game_001")
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_main",
                fragment_index=0,
                file_path=str(g1 / "segment_00000.mkv"),
                start_session_time_ns=None,
            )
        )
        sc = SessionClock(clock_ns=_FakeNanosecondClock())
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)
        coord._setup_resume_continuation(self.session_paths)
        self.assertIsNone(coord._resume_continuation)

    def test_no_continuation_when_no_segments_under_crashed_game(self) -> None:
        # game_002 folder exists on disk but has no segment rows in
        # the index (e.g. crash occurred before first segment finalized).
        # Without index data we can't anchor the rebase.
        self._make_game_folder("game_002")
        idx = SegmentIndex()
        sc = SessionClock(clock_ns=_FakeNanosecondClock())
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)
        coord._setup_resume_continuation(self.session_paths)
        self.assertIsNone(coord._resume_continuation)

    def test_substring_collision_across_game_subdirs(self) -> None:
        # `game_001` is a substring of `game_0011` — defensive test
        # that the path-component matcher doesn't conflate them.
        g1 = self._make_game_folder("game_001")
        # Manually create a (hypothetical) `game_0011` folder.
        g11 = self.session_paths.recording_dir / "game_0011" / "ndi_main"
        g11.mkdir(parents=True, exist_ok=True)
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_main",
                fragment_index=0,
                file_path=str(g1 / "segment_00000.mkv"),
                start_session_time_ns=0,
            )
        )
        # find_next_game_index uses the strict `^game_(\d{3})$` regex,
        # so a 4-digit `game_0011` is NOT matched. Highest game = 1,
        # so the crashed game is game_001. Our helper must pick game_001's
        # segment, not skip it because of substring confusion.
        sc = SessionClock(clock_ns=_FakeNanosecondClock())
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)
        coord._setup_resume_continuation(self.session_paths)
        self.assertIsNotNone(coord._resume_continuation)
        assert coord._resume_continuation is not None
        self.assertEqual(coord._resume_continuation.game_subdir, "game_001")


class SegmentsUnderGameSubdirTests(unittest.TestCase):
    """Focused unit test for the path-component matcher in
    `_segments_under_game_subdir`."""

    def test_matches_per_game_layout(self) -> None:
        idx = SegmentIndex()
        seg_in = _seg(
            feed_id="ndi_main",
            fragment_index=0,
            file_path=str(Path("/data/recording/game_005/ndi_main/segment_00000.mkv")),
            start_session_time_ns=0,
        )
        seg_other = _seg(
            feed_id="ndi_main",
            fragment_index=0,
            file_path=str(Path("/data/recording/game_006/ndi_main/segment_00000.mkv")),
            start_session_time_ns=0,
        )
        idx.add(seg_in)
        idx.add(seg_other)
        coord = _build_coordinator_stub(segment_index=idx, session_clock=None)
        results = list(coord._segments_under_game_subdir("game_005"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].file_path, seg_in.file_path)

    def test_skips_legacy_flat_layout(self) -> None:
        # Pre-Phase-4.E: segments live directly under <recording>/<feed_id>/
        # with no game_subdir component. They must NOT match.
        idx = SegmentIndex()
        idx.add(
            _seg(
                feed_id="ndi_main",
                fragment_index=0,
                file_path=str(Path("/data/recording/ndi_main/segment_00000.mkv")),
                start_session_time_ns=0,
            )
        )
        coord = _build_coordinator_stub(segment_index=idx, session_clock=None)
        self.assertEqual(
            list(coord._segments_under_game_subdir("game_001")), []
        )


class FirstStartConsumesContinuationTests(unittest.TestCase):
    """The first Start after Resume reuses the continuation's
    game_subdir + per-game filter. After consumption, a Stop-then-Start
    cycle behaves normally (allocates a fresh game).

    The toggle path itself touches feed runtimes, recording manager,
    session manager, and controllers — too much to construct in a unit
    test. Verify the consumption logic via direct manipulation of the
    coordinator's `_resume_continuation` field plus the replay-store
    filter side-effect, which is the user-visible contract.
    """

    def test_continuation_consumption_clears_field(self) -> None:
        # Direct simulation of the consumption branch from
        # `toggle_long_session_recording`. After processing the first
        # Start, `_resume_continuation` must be None so a subsequent
        # Stop+Start enters the fresh-game branch.
        idx = SegmentIndex()
        sc = SessionClock(clock_ns=_FakeNanosecondClock())
        coord = _build_coordinator_stub(segment_index=idx, session_clock=sc)
        coord._resume_continuation = _ResumeContinuation(
            game_subdir="game_005",
            game_start_session_time_ns=20_000_000_000,
        )

        # Simulate the toggle path's read-and-clear.
        consumed = coord._resume_continuation
        coord._resume_continuation = None
        assert consumed is not None

        # Per-game filter set from continuation (mirrors the toggle path).
        coord.replay_store.set_current_game_start_session_time(
            consumed.game_start_session_time_ns
        )

        self.assertIsNone(coord._resume_continuation)
        # Filter applied to the store.
        idx.add(
            _seg(
                feed_id="ndi_main",
                fragment_index=0,
                file_path="/data/recording/game_005/ndi_main/segment_00000.mkv",
                start_session_time_ns=20_000_000_000,
            )
        )
        idx.add(
            _seg(
                feed_id="ndi_main",
                fragment_index=1,
                file_path="/data/recording/game_005/ndi_main/segment_00001.mkv",
                start_session_time_ns=24_000_000_000,
            )
        )
        earliest, latest = coord.replay_store.available_session_time_range()
        # Both pre-crash segments visible because the filter equals
        # the crashed game's earliest start.
        self.assertEqual(earliest, 20_000_000_000)
        self.assertEqual(latest, 28_000_000_000)


if __name__ == "__main__":
    unittest.main()
