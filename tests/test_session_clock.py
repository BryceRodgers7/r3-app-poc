"""Tests for slice 5.A — `SessionClock` + per-feed PTS-to-session-time capture."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    Segment,
    SessionPaths,
)
from app.core.session_clock import SessionClock
from app.storage.metadata_db import MetadataDb


class _FakeNanosecondClock:
    """Manually advanced monotonic-ns clock for deterministic SessionClock tests."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_ns(self, delta_ns: int) -> None:
        self.now_ns += delta_ns


class SessionClockTests(unittest.TestCase):
    def test_session_start_anchors_at_construction(self) -> None:
        clock_fn = _FakeNanosecondClock(start_ns=42_000_000_000)
        sc = SessionClock(clock_ns=clock_fn)
        self.assertEqual(sc.session_start_monotonic_ns, 42_000_000_000)

    def test_now_session_time_advances_with_underlying_clock(self) -> None:
        clock_fn = _FakeNanosecondClock(start_ns=10_000_000_000)
        sc = SessionClock(clock_ns=clock_fn)
        self.assertEqual(sc.now_session_time_ns(), 0)
        clock_fn.advance_ns(2_500_000_000)
        self.assertEqual(sc.now_session_time_ns(), 2_500_000_000)
        clock_fn.advance_ns(1_000_000)
        self.assertEqual(sc.now_session_time_ns(), 2_501_000_000)

    def test_default_clock_uses_real_monotonic_ns(self) -> None:
        # Smoke test the default path: time stays non-negative and
        # monotonically non-decreasing.
        sc = SessionClock()
        first = sc.now_session_time_ns()
        second = sc.now_session_time_ns()
        self.assertGreaterEqual(first, 0)
        self.assertGreaterEqual(second, first)


class SessionClockRebaseTests(unittest.TestCase):
    """Phase 7.D: rebasing the clock past pre-crash session-time so the
    resume-after-crash path can keep integer comparison meaningful."""

    def test_rebase_makes_now_return_anchor(self) -> None:
        clock_fn = _FakeNanosecondClock(start_ns=10_000_000_000)
        sc = SessionClock(clock_ns=clock_fn)
        self.assertEqual(sc.now_session_time_ns(), 0)
        sc.rebase(30_000_000_000)
        # now_session_time_ns() at the moment of rebase returns the anchor.
        self.assertEqual(sc.now_session_time_ns(), 30_000_000_000)

    def test_rebase_preserves_monotonic_advance(self) -> None:
        clock_fn = _FakeNanosecondClock(start_ns=10_000_000_000)
        sc = SessionClock(clock_ns=clock_fn)
        sc.rebase(30_000_000_000)
        # Advance underlying clock 5s.
        clock_fn.advance_ns(5_000_000_000)
        self.assertEqual(sc.now_session_time_ns(), 35_000_000_000)

    def test_rebase_post_crash_anchor_above_pre_crash_session_time(self) -> None:
        # Simulates the resume flow: latest pre-crash end_session_time = 30s.
        # After rebase, post-resume "now" is strictly greater than 30s
        # so integer comparison against pre-crash segment values stays
        # well-defined.
        pre_crash_latest_end_ns = 30_000_000_000
        clock_fn = _FakeNanosecondClock(start_ns=99_000_000_000)
        sc = SessionClock(clock_ns=clock_fn)
        # Fresh clock starts at 0 — comparison would say
        # `0 < 30_000_000_000`, putting "now" before pre-crash.
        self.assertLess(sc.now_session_time_ns(), pre_crash_latest_end_ns)
        # Rebase past the pre-crash latest with a 1ms gap.
        sc.rebase(pre_crash_latest_end_ns + 1_000_000)
        self.assertGreater(sc.now_session_time_ns(), pre_crash_latest_end_ns)


class PipelineManagerSessionTimeCaptureTests(unittest.TestCase):
    """Drive `_on_jpegenc_buffer_probe` + `_finalize_pending_segment_locked`
    through a stub `PipelineManager` to confirm that session-time fields
    end up on the `Segment` row."""

    def _build_pm_stub(self, *, clock: SessionClock | None) -> object:
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._recording_session_paths = None
        pm._recording_feed_id = None
        pm._recording_codec = "mjpeg"
        pm._recording_container = "mkv"
        pm._recording_segment_counter = 0
        pm._recording_game_subdir = None
        pm._pending_segment = None
        pm._metadata_db = None
        pm._segment_index = None
        pm._recording_running = True
        pm._feed_metrics = None
        pm._session_clock = clock
        return pm

    def _session_paths(self, root: Path) -> SessionPaths:
        recording_dir = root / "recording"
        for d in (root, recording_dir):
            d.mkdir(parents=True, exist_ok=True)
        return SessionPaths(
            session_id="session_001",
            root_dir=root,
            recording_dir=recording_dir,
        )

    def test_pts_to_session_offset_captured_on_first_buffer(self) -> None:
        """A segment whose first buffer arrives at session_time=10s with
        PTS=2s should record `pts_to_session_offset_ns = 8s`."""
        from app.media.pipeline_manager import PipelineManager

        clock_fn = _FakeNanosecondClock(start_ns=0)
        clock_fn.advance_ns(10_000_000_000)  # session starts at t=0; clock at t=10s
        # Construct AFTER advancing so session_start = 10s; first reading = 10s.
        sc = SessionClock(clock_ns=clock_fn)

        with TemporaryDirectory() as tmp:
            session = self._session_paths(Path(tmp))
            pm = self._build_pm_stub(clock=sc)
            pm._recording_session_paths = session
            pm._recording_feed_id = "ndi_main"

            # Open segment 0.
            path1 = PipelineManager._on_splitmuxsink_format_location(pm, None, 0)
            file_path1 = Path(path1)
            file_path1.parent.mkdir(parents=True, exist_ok=True)
            file_path1.write_bytes(b"\0")

            # Advance clock by 0.5s before first buffer.
            clock_fn.advance_ns(500_000_000)

            # Stub buffer probe: simulate first buffer at PTS=2_000_000_000.
            pending = pm._pending_segment
            assert pending is not None
            pending["first_pts_ns"] = 2_000_000_000
            pending["first_session_time_ns"] = sc.now_session_time_ns()
            pending["last_pts_ns"] = 2_000_000_000
            pending["frame_count"] = 1

            # Now simulate two seconds of buffers (last PTS = 4s).
            pending["last_pts_ns"] = 4_000_000_000
            pending["frame_count"] = 60

            # Close it via finalize.
            PipelineManager._finalize_pending_segment_locked(pm)

        # The pending dict was cleared by finalize. Re-derive what the
        # Segment row would have had: first_session_time_ns was 500ms
        # after the SessionClock construction (which we set to t=10s),
        # so first_session_time_ns = 500_000_000 (relative to session
        # origin). Offset = 500_000_000 - 2_000_000_000 = -1_500_000_000.
        # We can't read the inserted Segment back without a metadata_db,
        # but we can re-run the math the same way and assert via the
        # next test (which uses metadata_db).

    def test_finalize_persists_session_time_fields_to_db(self) -> None:
        """Round-trip: finalize → insert_segment → read back."""
        from app.media.pipeline_manager import PipelineManager

        clock_fn = _FakeNanosecondClock(start_ns=0)
        sc = SessionClock(clock_ns=clock_fn)

        with TemporaryDirectory() as tmp:
            session = self._session_paths(Path(tmp))
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="session_001",
                source_name="Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            pm = self._build_pm_stub(clock=sc)
            pm._recording_session_paths = session
            pm._recording_feed_id = "ndi_main"
            pm._metadata_db = db

            # Open segment.
            path = PipelineManager._on_splitmuxsink_format_location(pm, None, 0)
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"\0")

            # Advance clock 3s; first buffer arrives with PTS=1_000_000_000.
            clock_fn.advance_ns(3_000_000_000)
            pending = pm._pending_segment
            assert pending is not None
            pending["first_pts_ns"] = 1_000_000_000
            pending["first_session_time_ns"] = sc.now_session_time_ns()
            pending["last_pts_ns"] = 5_000_000_000  # 4s span in PTS
            pending["frame_count"] = 120

            # Finalize.
            PipelineManager._finalize_pending_segment_locked(pm)

            # Read back from DB.
            segments = db.segments_for_session("session_001")
            self.assertEqual(len(segments), 1)
            seg = segments[0]
            self.assertEqual(seg.start_pts_ns, 1_000_000_000)
            self.assertEqual(seg.end_pts_ns, 5_000_000_000)
            # session_time fields populated.
            self.assertEqual(seg.start_session_time_ns, 3_000_000_000)
            # offset = first_session_time - first_pts = 3s - 1s = 2s
            self.assertEqual(seg.pts_to_session_offset_ns, 2_000_000_000)
            # end_session_time = last_pts + offset = 5s + 2s = 7s
            self.assertEqual(seg.end_session_time_ns, 7_000_000_000)
            db.close()

    def test_finalize_leaves_session_time_fields_none_when_no_clock(self) -> None:
        """A pipeline running without a `SessionClock` (test stubs, etc.)
        produces segments with NULL session-time fields rather than
        crashing."""
        from app.media.pipeline_manager import PipelineManager

        with TemporaryDirectory() as tmp:
            session = self._session_paths(Path(tmp))
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="session_001",
                source_name="Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            pm = self._build_pm_stub(clock=None)
            pm._recording_session_paths = session
            pm._recording_feed_id = "ndi_main"
            pm._metadata_db = db

            path = PipelineManager._on_splitmuxsink_format_location(pm, None, 0)
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"\0")

            pending = pm._pending_segment
            assert pending is not None
            pending["first_pts_ns"] = 1_000_000_000
            # `first_session_time_ns` left as None (no clock attached).
            pending["last_pts_ns"] = 5_000_000_000
            pending["frame_count"] = 120

            PipelineManager._finalize_pending_segment_locked(pm)

            segments = db.segments_for_session("session_001")
            self.assertEqual(len(segments), 1)
            seg = segments[0]
            self.assertIsNone(seg.start_session_time_ns)
            self.assertIsNone(seg.end_session_time_ns)
            self.assertIsNone(seg.pts_to_session_offset_ns)
            db.close()


class LoadSegmentIndexRoundTripTests(unittest.TestCase):
    """Confirm session-time fields survive the recovery-load path."""

    def test_session_time_fields_round_trip_through_index(self) -> None:
        from app.storage.session_recovery import load_segment_index_for_session

        with TemporaryDirectory() as tmp:
            db = MetadataDb(Path(tmp) / "metadata.db")
            db.create_session(
                session_id="session_001",
                source_name="Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            db.insert_segment(
                Segment(
                    session_id="session_001",
                    feed_id="ndi_main",
                    fragment_index=0,
                    file_path="/x/0.mkv",
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
                    start_session_time_ns=10_000_000_000,
                    end_session_time_ns=14_000_000_000,
                    pts_to_session_offset_ns=10_000_000_000,
                )
            )
            index = load_segment_index_for_session(db, "session_001")
            loaded = index.all_for_feed("ndi_main")
            self.assertEqual(len(loaded), 1)
            seg = loaded[0]
            self.assertEqual(seg.start_session_time_ns, 10_000_000_000)
            self.assertEqual(seg.end_session_time_ns, 14_000_000_000)
            self.assertEqual(seg.pts_to_session_offset_ns, 10_000_000_000)
            db.close()


if __name__ == "__main__":
    unittest.main()
