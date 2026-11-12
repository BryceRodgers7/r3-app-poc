"""Tests for slice 5.A — `SessionClock` + per-feed PTS-to-session-time capture."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
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


class PipelineClockMappingTests(unittest.TestCase):
    """`PipelineManager._running_time_to_monotonic_ns`: running time to
    `time.monotonic_ns()`, one constant per pipeline (REPLAY_SEAM_GLITCH.md
    §7.4 steps 0-1). Segment rows built from it are covered in
    `test_segment_close_gating`."""

    class _Clock:
        def __init__(self, lag_ns: int) -> None:
            self.lag_ns = lag_ns

        def get_time(self) -> int:
            return time.monotonic_ns() - self.lag_ns

    def _pm(self, clock: object, base_time: int) -> object:
        from types import SimpleNamespace

        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = SimpleNamespace(CLOCK_TIME_NONE=2**64 - 1)
        pm._pipeline = SimpleNamespace(get_clock=lambda: clock, get_base_time=lambda: base_time)
        pm._pipeline_generation = 1
        pm._recording_feed_id = "ndi_main"
        return pm

    def test_maps_base_time_plus_clock_offset(self) -> None:
        pm = self._pm(self._Clock(lag_ns=3_000_000), base_time=9_000_000_000)
        with self.assertLogs("app.media.pipeline_manager", level="INFO") as logs:
            value = pm._running_time_to_monotonic_ns()
        self.assertAlmostEqual(value, 9_000_000_000 + 3_000_000, delta=200_000)
        self.assertTrue(any("pipeline clock" in line for line in logs.output))

    def test_constant_for_the_pipeline_and_warns_on_drift(self) -> None:
        clock = self._Clock(lag_ns=0)
        pm = self._pm(clock, base_time=5_000_000_000)
        first = pm._running_time_to_monotonic_ns()
        clock.lag_ns = 50_000_000  # the clock falls 50 ms behind
        with self.assertLogs("app.media.pipeline_manager", level="WARNING"):
            second = pm._running_time_to_monotonic_ns()
        self.assertEqual(first, second)

    def test_new_pipeline_generation_remeasures(self) -> None:
        clock = self._Clock(lag_ns=0)
        pm = self._pm(clock, base_time=5_000_000_000)
        first = pm._running_time_to_monotonic_ns()
        clock.lag_ns = 20_000_000
        pm._pipeline_generation = 2
        second = pm._running_time_to_monotonic_ns()
        self.assertAlmostEqual(second - first, 20_000_000, delta=200_000)

    def test_none_without_a_pipeline(self) -> None:
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = None
        self.assertIsNone(pm._running_time_to_monotonic_ns())


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
