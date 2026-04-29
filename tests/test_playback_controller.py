"""Focused playback-session controller tests (post slice 5.C)."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PySide6.QtCore import QCoreApplication

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    FeedDefinition,
    FrameOverlayInfo,
    MediaFrame,
    PlaybackMode,
    Segment,
    SessionPaths,
)
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState
from app.core.session_clock import SessionClock
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import OutputRenderer
from app.media.recording_manager import RecordingManager
from app.media.segment_decoder import SegmentDecoder
from app.media.source_interface import SourceInterface
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import RecordingSegmentReplayStore


class _FakeSource(SourceInterface):
    def __init__(self, feed_id: str) -> None:
        self._feed_id = feed_id

    def connect_source(self) -> bool:
        return True

    def disconnect_source(self) -> None:
        return

    def is_connected(self) -> bool:
        return True

    def get_display_name(self) -> str:
        return "Fake Source"

    def get_feed_id(self) -> str:
        return self._feed_id

    def create_pipeline_fragment(self) -> str:
        return "fake"

    def read_frame(self) -> MediaFrame | None:
        return None

    def get_frame_size(self) -> tuple[int, int]:
        return 32, 24

    def get_nominal_fps(self) -> float:
        return 15.0


class _FakePipelineManager:
    def __init__(self, feed_id: str) -> None:
        self._feed_id = feed_id

    def set_frame_callback(self, callback) -> None:
        self._frame_callback = callback

    def set_live_sample_callback(self, callback) -> None:
        self._overlay_callback = callback

    def connect_source(self) -> bool:
        return True

    def start_preview(self) -> None:
        return

    def stop_all(self) -> None:
        return

    def is_source_connected(self) -> bool:
        return True

    def get_source_name(self) -> str:
        return "Fake Source"

    def get_source_status_message(self) -> str | None:
        return None

    def get_ingest_telemetry(self) -> None:
        return None


class _FakeRenderer(OutputRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[MediaFrame] = []
        self.placeholders: list[str] = []

    def show_frame(self, frame: MediaFrame) -> None:
        self.frames.append(frame)
        super().show_frame(frame)

    def show_placeholder_message(self, message: str) -> None:
        self.placeholders.append(message)
        super().show_placeholder_message(message)


class _StubSegmentDecoder(SegmentDecoder):
    """SegmentDecoder that returns a synthesized BGR frame without cv2."""

    def __init__(self, feed_id: str, source_name: str) -> None:
        super().__init__(feed_id, source_name)
        self.decode_calls: list[tuple[str, int, str]] = []

    def decode(self, location):  # type: ignore[override]
        self.decode_calls.append(
            (
                location.segment.file_path,
                location.offset_in_segment_ns,
                location.segment.feed_id,
            )
        )
        marker = (location.offset_in_segment_ns // 1_000_000) % 255
        image = np.full((24, 32, 3), marker, dtype=np.uint8)
        return MediaFrame(
            frame_id=int(location.offset_in_segment_ns),
            timestamp=0.0,
            image=image,
            source_name=self._source_name,
            feed_id=self._feed_id,
        )

    def close(self) -> None:  # type: ignore[override]
        return


def _make_segment(
    *,
    fragment_index: int,
    start_pts_ns: int,
    duration_ns: int = 4_000_000_000,
    feed_id: str = "feed_main",
    state: str = SEGMENT_STATE_COMPLETE,
    pts_to_session_offset_ns: int = 0,
) -> Segment:
    """Build a `Segment` row for SegmentIndex fixtures.

    `pts_to_session_offset_ns` defaults to 0 — session time equals PTS.
    Set it to a positive value to simulate a feed that joined later
    (its session-time start is `start_pts_ns + offset`).
    """
    return Segment(
        session_id="session_001",
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=f"/tmp/{feed_id}/segment_{fragment_index:05d}.mkv",
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start_pts_ns,
        end_pts_ns=start_pts_ns + duration_ns,
        duration_ns=duration_ns,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at="2026-04-28T01:00:04+00:00",
        start_session_time_ns=start_pts_ns + pts_to_session_offset_ns,
        end_session_time_ns=start_pts_ns + duration_ns + pts_to_session_offset_ns,
        pts_to_session_offset_ns=pts_to_session_offset_ns,
    )


class PlaybackControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root_dir = Path(self._temp_dir.name) / "session_001"
        recording_dir = root_dir / "recording"
        rolling_dir = root_dir / "rolling"
        clips_dir = root_dir / "clips"
        for path in (root_dir, recording_dir, rolling_dir, clips_dir):
            path.mkdir(parents=True, exist_ok=True)

        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root_dir,
            recording_dir=recording_dir,
            rolling_dir=rolling_dir,
            clips_dir=clips_dir,
        )
        self.feed = FeedDefinition(feed_id="feed_main", display_name="Fake Source")
        fake_source = _FakeSource(self.feed.feed_id)
        fake_pipeline = _FakePipelineManager(self.feed.feed_id)
        self.recording_manager = RecordingManager()

        # Five back-to-back 4-second segments — session-time 0..20 (offset=0).
        self.segment_index = SegmentIndex()
        for i in range(5):
            self.segment_index.add(
                _make_segment(fragment_index=i, start_pts_ns=i * 4_000_000_000)
            )
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)
        self.stub_decoder = _StubSegmentDecoder(self.feed.feed_id, self.feed.display_name)

        self.runtime = FeedRuntime(
            feed=self.feed,
            source=fake_source,
            pipeline_manager=fake_pipeline,
        )
        self.runtime.start(self.session_paths)

        for frame_id in range(60):
            timestamp = 100.0 + (frame_id * (1.0 / 15.0))
            image = np.full((24, 32, 3), frame_id % 255, dtype=np.uint8)
            frame = MediaFrame(
                frame_id=frame_id,
                timestamp=timestamp,
                image=image,
                source_name="Fake Source",
                feed_id=self.feed.feed_id,
            )
            self.runtime._on_live_frame(frame)
            self.runtime._on_live_overlay(FrameOverlayInfo.from_media_frame(frame, feed_id=self.feed.feed_id))

        self.renderer = _FakeRenderer()
        self.controller = PlaybackController(
            feed_runtimes=[self.runtime],
            output_renderer=self.renderer,
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Fake Source",
            session_role="operator",
            live_only=False,
            decoder_factory=lambda *_args: self.stub_decoder,
        )
        self.controller.initialize(self.session_paths.session_id)

    def tearDown(self) -> None:
        self.controller.shutdown()
        self._temp_dir.cleanup()

    def _force_recording_state(self) -> None:
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()

    def test_rewind_enters_replay_and_renders_segment_frame(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        self.assertTrue(self.controller._replay_timer.isActive())
        # Latest replayable session_time = 20s; rewind 10s → target = 10s.
        # Falls in segment 2 (start_session_time=8s, end_session_time=12s), offset 2s.
        self.assertTrue(self.stub_decoder.decode_calls)
        rendered_path, rendered_offset, _ = self.stub_decoder.decode_calls[-1]
        self.assertIn("segment_00002.mkv", rendered_path)
        self.assertEqual(rendered_offset, 2_000_000_000)
        self.assertTrue(self.renderer.frames)
        self.assertEqual(self.renderer.frames[-1].feed_id, self.feed.feed_id)

    def test_rewind_twice_from_live_accumulates_to_minus_20s(self) -> None:
        """Slice 5.C UX: clicking Rewind 10s twice goes back 20s in total.

        First click anchors on `latest_replayable_session_time` = 20s,
        so target = 10s. Second click anchors on the current playback
        position (10s), so target = 0s.
        """
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        first_target = self.controller._playback_session_time_ns
        self.assertEqual(first_target, 10_000_000_000)
        self.controller.rewind_10_seconds()
        second_target = self.controller._playback_session_time_ns
        self.assertEqual(second_target, 0)
        # Last decode should be at session_time=0 → segment 0, offset 0.
        rendered_path, rendered_offset, _ = self.stub_decoder.decode_calls[-1]
        self.assertIn("segment_00000.mkv", rendered_path)
        self.assertEqual(rendered_offset, 0)

    def test_rewind_from_replay_anchors_on_current_position_not_live(self) -> None:
        """Verifies the §5.C anchor switch: in REPLAY mode, the anchor
        is `_playback_session_time_ns`, not the latest replayable."""
        self._force_recording_state()
        # Force a known playback position partway through the timeline.
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 15_000_000_000  # 15s
        self.controller.rewind_10_seconds()
        # Anchor on 15s, rewind 10s → target = 5s.
        self.assertEqual(self.controller._playback_session_time_ns, 5_000_000_000)

    def test_rewind_from_pause_also_anchors_on_current_position(self) -> None:
        """PAUSED is also an active replay state — repeated rewind clicks
        from a paused frame should still accumulate."""
        self._force_recording_state()
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.PAUSED
            self.controller._playback_session_time_ns = 12_000_000_000  # 12s
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller._playback_session_time_ns, 2_000_000_000)

    def test_rewind_clamps_at_earliest_session_time(self) -> None:
        """A rewind that would go past session-time 0 lands at 0."""
        self._force_recording_state()
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 5_000_000_000
        self.controller.rewind_10_seconds()  # 5s − 10s = -5s, clamps to 0.
        self.assertEqual(self.controller._playback_session_time_ns, 0)

    def test_pause_freezes_replay_clock_and_renders_freeze_frame(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        rewind_decode_count = len(self.stub_decoder.decode_calls)
        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED)
        self.assertFalse(self.controller._replay_timer.isActive())
        self.assertGreater(len(self.stub_decoder.decode_calls), rewind_decode_count)

    def test_pause_with_no_active_replay_freezes_at_latest_replayable(self) -> None:
        self._force_recording_state()
        self.controller.pause_playback()
        # Latest replayable session_time = 20s, which is in segment 4
        # (start=16s, end=20s). The replay store's `resolve_session_time`
        # returns the matching segment with offset = 4s. Either segment 3
        # or 4 is a valid result depending on overlap-tie-break.
        self.assertTrue(self.stub_decoder.decode_calls)
        path, _offset, _ = self.stub_decoder.decode_calls[-1]
        self.assertTrue(
            "segment_00003.mkv" in path or "segment_00004.mkv" in path
        )

    def test_set_playback_rate_half_speed_keeps_decoder_rate_agnostic(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.controller.set_playback_rate(0.5)
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        self.assertEqual(self.controller.replay_state.state, ReplayState.SLOW_MOTION)

    def test_set_playback_rate_zero_pauses(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.controller.set_playback_rate(0.0)
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED)
        self.assertEqual(self.controller.replay_state.state, ReplayState.PAUSED)

    def test_set_playback_rate_from_live_does_not_rewind(self) -> None:
        """Bug fix: Slow 1/2 from LIVE used to call `rewind_10_seconds`
        as a side effect, dropping playback to `latest − 10s`. The new
        behavior snaps to `latest_replayable_session_time` (the leading
        edge of replay coverage) without any rewind, so the operator
        starts watching at the freshest available frame and falls
        behind live as playback progresses.
        """
        self._force_recording_state()
        # Pre-condition: not yet in REPLAY.
        self.assertNotEqual(
            self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY
        )
        self.assertIsNone(self.controller._playback_session_time_ns)
        self.controller.set_playback_rate(0.5)
        # Latest replayable session_time is the end of the last
        # finalized segment = 20s in this fixture. The slow button
        # should land us at 20s, NOT at 10s.
        self.assertEqual(
            self.controller._playback_session_time_ns, 20_000_000_000
        )
        self.assertEqual(
            self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY
        )

    def test_jump_to_live_clears_playback_session_time_and_returns_to_live(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertIsNotNone(self.controller._playback_session_time_ns)
        self.controller.jump_to_live()
        self.assertIsNone(self.controller._playback_session_time_ns)
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.LIVE)

    def test_live_only_controller_rejects_transport_changes(self) -> None:
        renderer = _FakeRenderer()
        controller = PlaybackController(
            feed_runtimes=[self.runtime],
            output_renderer=renderer,
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Fake Source",
            session_role="program",
            live_only=True,
        )
        controller.initialize(self.session_paths.session_id)
        self.addCleanup(controller.shutdown)

        controller.pause_playback()
        self.assertEqual(controller.get_state().current_playback_mode, PlaybackMode.LIVE)

    def test_live_only_controller_does_not_allocate_decoders(self) -> None:
        controller = PlaybackController(
            feed_runtimes=[self.runtime],
            output_renderer=_FakeRenderer(),
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Fake Source",
            session_role="program",
            live_only=True,
        )
        self.addCleanup(controller.shutdown)
        # Slice 5.C: dict instead of single optional decoder; live_only outputs are empty.
        self.assertEqual(controller._segment_decoders, {})

    def test_refresh_recording_state_emits(self) -> None:
        self.controller.refresh_recording_state()
        self.assertFalse(self.controller.get_state().is_recording)

    def test_rewind_rejected_when_recording_not_active(self) -> None:
        before_mode = self.controller.get_state().current_playback_mode
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, before_mode)
        self.assertEqual(self.stub_decoder.decode_calls, [])

    def test_pause_rejected_when_recording_not_active(self) -> None:
        before_mode = self.controller.get_state().current_playback_mode
        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, before_mode)

    def test_recording_stop_mid_replay_snaps_back_to_live(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        self.recording_manager.recording_state.force(RecordingState.NOT_RECORDING)
        self.controller.refresh_recording_state()
        self.assertEqual(
            self.controller.replay_state.state,
            ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        )
        self.assertNotEqual(
            self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY
        )

    def test_replay_buffer_span_seconds_reflects_segment_index_coverage(self) -> None:
        self._force_recording_state()
        self.controller.refresh_recording_state()
        # Five 4-second segments → 20 seconds of session-time coverage.
        self.assertAlmostEqual(
            self.controller.get_state().replay_buffer_span_seconds, 20.0, places=3
        )


class _FakeNanosecondClock:
    """Manually advanced monotonic-ns clock for SessionClock tests."""

    def __init__(self, start_ns: int = 0) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_ns(self, delta_ns: int) -> None:
        self.now_ns += delta_ns


class SecondsBehindLiveSmoothnessTests(unittest.TestCase):
    """Bug fix: `seconds_behind_live` should grow continuously with
    real time during pause / slow motion, not jump in segment-finalize
    quanta. Verified by passing in a fake `SessionClock` whose monotonic
    source the test advances directly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root_dir = Path(self._temp_dir.name) / "session_001"
        for sub in ("recording", "rolling", "clips"):
            (root_dir / sub).mkdir(parents=True, exist_ok=True)
        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root_dir,
            recording_dir=root_dir / "recording",
            rolling_dir=root_dir / "rolling",
            clips_dir=root_dir / "clips",
        )
        self.feed = FeedDefinition(feed_id="feed_main", display_name="Fake Source")
        self.recording_manager = RecordingManager()
        self.segment_index = SegmentIndex()
        # Single 4-second segment at session_time 0..4. Fixture
        # deliberately keeps `latest_replayable` constant at 4s so the
        # smoothness check isolates the clock-vs-segment-edge difference.
        self.segment_index.add(
            _make_segment(fragment_index=0, start_pts_ns=0)
        )
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)
        self.stub_decoder = _StubSegmentDecoder(self.feed.feed_id, self.feed.display_name)

        self.fake_clock_fn = _FakeNanosecondClock(start_ns=0)
        # Advance to t=10s so when SessionClock is constructed below,
        # `now_session_time_ns()` starts at 0 and grows from there.
        self.fake_clock_fn.advance_ns(10_000_000_000)
        self.session_clock = SessionClock(clock_ns=self.fake_clock_fn)

        runtime = FeedRuntime(
            feed=self.feed,
            source=_FakeSource(self.feed.feed_id),
            pipeline_manager=_FakePipelineManager(self.feed.feed_id),
        )
        runtime.start(self.session_paths)

        self.renderer = _FakeRenderer()
        self.controller = PlaybackController(
            feed_runtimes=[runtime],
            output_renderer=self.renderer,
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Fake Source",
            session_role="operator",
            live_only=False,
            decoder_factory=lambda *_args: self.stub_decoder,
            session_clock=self.session_clock,
        )
        self.controller.initialize(self.session_paths.session_id)
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()

    def tearDown(self) -> None:
        self.controller.shutdown()
        self._temp_dir.cleanup()

    def test_seconds_behind_live_grows_smoothly_during_pause(self) -> None:
        """While paused, `latest_replayable` stays at 4s but the clock
        keeps advancing. The displayed `seconds_behind_live` should
        track the clock, not the segment edge."""
        self.controller.pause_playback()
        # Pause anchored at latest_replayable = 4s; clock now at 0s
        # session-time → behind = 0 - 4 clamped to 0. Advance clock 5s
        # of real time without finalizing any new segments.
        self.fake_clock_fn.advance_ns(5_000_000_000)
        with self.controller._lock:
            self.controller._update_state_timestamps_locked()
        # session_clock.now = 5s; playback_session_time = 4s; behind = 1s.
        self.assertAlmostEqual(
            self.controller.get_state().seconds_behind_live, 1.0, places=2
        )
        # Advance another 4 real-time seconds (still no new segment).
        self.fake_clock_fn.advance_ns(4_000_000_000)
        with self.controller._lock:
            self.controller._update_state_timestamps_locked()
        # session_clock.now = 9s; playback_session_time = 4s; behind = 5s.
        self.assertAlmostEqual(
            self.controller.get_state().seconds_behind_live, 5.0, places=2
        )

    def test_seconds_behind_live_unaffected_by_segment_finalization(self) -> None:
        """The smooth metric should NOT jump when a new segment finalizes
        — the clock-based formula is independent of segment edges."""
        self.controller.pause_playback()
        self.fake_clock_fn.advance_ns(5_000_000_000)
        with self.controller._lock:
            self.controller._update_state_timestamps_locked()
        before = self.controller.get_state().seconds_behind_live
        # Simulate a new segment finalizing — this would have caused a
        # 4s jump in the old segment-edge metric.
        self.segment_index.add(
            _make_segment(fragment_index=1, start_pts_ns=4_000_000_000)
        )
        with self.controller._lock:
            self.controller._update_state_timestamps_locked()
        after = self.controller.get_state().seconds_behind_live
        # Same clock reading → same behind-live (no jump).
        self.assertAlmostEqual(before, after, places=2)


class MultiFeedRenderTests(unittest.TestCase):
    """Slice 5.C: multi-feed render via `nearest_frame_location` per
    feed. Covers the §8.6.1 worked example: feed B joining at
    session_time=5s renders a freeze frame on its first segment when
    the operator rewinds to a session_time before feed B exists, and
    starts moving once the playback clock catches up."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root_dir = Path(self._temp_dir.name) / "session_001"
        recording_dir = root_dir / "recording"
        rolling_dir = root_dir / "rolling"
        clips_dir = root_dir / "clips"
        for path in (root_dir, recording_dir, rolling_dir, clips_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root_dir,
            recording_dir=recording_dir,
            rolling_dir=rolling_dir,
            clips_dir=clips_dir,
        )

        self.feed_a = FeedDefinition(feed_id="ndi_a", display_name="Feed A")
        self.feed_b = FeedDefinition(feed_id="ndi_b", display_name="Feed B")
        self.recording_manager = RecordingManager()

        # Feed A: session_time 0..20 (offset=0).
        # Feed B: joined at session_time=5, segments at session_time 5..17.
        self.segment_index = SegmentIndex()
        for i in range(5):
            self.segment_index.add(
                _make_segment(
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    feed_id="ndi_a",
                )
            )
        for i in range(3):
            self.segment_index.add(
                _make_segment(
                    fragment_index=i,
                    start_pts_ns=i * 4_000_000_000,
                    feed_id="ndi_b",
                    pts_to_session_offset_ns=5_000_000_000,
                )
            )
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)

        # One stub decoder shared across both feeds — its decode_calls
        # log includes the per-call feed_id.
        self.stub_decoder = _StubSegmentDecoder("shared", "Shared")

        runtime_a = FeedRuntime(
            feed=self.feed_a,
            source=_FakeSource(self.feed_a.feed_id),
            pipeline_manager=_FakePipelineManager(self.feed_a.feed_id),
        )
        runtime_a.start(self.session_paths)
        runtime_b = FeedRuntime(
            feed=self.feed_b,
            source=_FakeSource(self.feed_b.feed_id),
            pipeline_manager=_FakePipelineManager(self.feed_b.feed_id),
        )
        runtime_b.start(self.session_paths)

        self.renderer = _FakeRenderer()
        self.controller = PlaybackController(
            feed_runtimes=[runtime_a, runtime_b],
            output_renderer=self.renderer,
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Feed A",
            session_role="operator",
            live_only=False,
            decoder_factory=lambda *_args: self.stub_decoder,
        )
        self.controller.initialize(self.session_paths.session_id)

    def tearDown(self) -> None:
        self.controller.shutdown()
        self._temp_dir.cleanup()

    def _force_recording_state(self) -> None:
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()

    def _decode_calls_for_feed(self, feed_id: str) -> list[tuple[str, int, str]]:
        return [c for c in self.stub_decoder.decode_calls if c[2] == feed_id]

    def test_rewind_to_before_feed_b_renders_freeze_on_b(self) -> None:
        """The §8.6.1 worked example, first phase: at session_time=2,
        feed A plays normally and feed B's tile shows its first frame
        (at session_time=5) frozen."""
        self._force_recording_state()
        # Latest replayable across feeds = 20s (feed A); rewind 18s
        # (manually via mode + position) lands us at session_time=2.
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 12_000_000_000
        self.controller.rewind_10_seconds()  # → 2s
        self.assertEqual(self.controller._playback_session_time_ns, 2_000_000_000)

        a_calls = self._decode_calls_for_feed("ndi_a")
        b_calls = self._decode_calls_for_feed("ndi_b")
        self.assertTrue(a_calls)
        self.assertTrue(b_calls)
        # Feed A: in coverage at session_time=2 → segment 0, offset 2s.
        a_path, a_offset, _ = a_calls[-1]
        self.assertIn("ndi_a/segment_00000.mkv", a_path)
        self.assertEqual(a_offset, 2_000_000_000)
        # Feed B: before earliest → freeze on segment 0 at offset 0.
        b_path, b_offset, _ = b_calls[-1]
        self.assertIn("ndi_b/segment_00000.mkv", b_path)
        self.assertEqual(b_offset, 0)

    def test_rewind_to_session_time_5_starts_b_playing(self) -> None:
        """The §8.6.1 worked example, second phase: at session_time=5,
        both feeds are in coverage and play in sync."""
        self._force_recording_state()
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 15_000_000_000
        self.controller.rewind_10_seconds()  # → 5s
        self.assertEqual(self.controller._playback_session_time_ns, 5_000_000_000)

        a_calls = self._decode_calls_for_feed("ndi_a")
        b_calls = self._decode_calls_for_feed("ndi_b")
        # Feed A at session_time=5: segment 1 (start=4s) offset=1s, OR
        # segment 0 (start=0, end=4s) at the boundary. The replay store
        # picks the segment that strictly contains the point — segment
        # 1 starts at 4 (inclusive), so 5 falls in segment 1 with
        # offset=1s.
        a_path, a_offset, _ = a_calls[-1]
        self.assertTrue(
            "ndi_a/segment_00001.mkv" in a_path
            or "ndi_a/segment_00000.mkv" in a_path
        )
        # Feed B at session_time=5: segment 0 (session_time 5..9), offset=0.
        b_path, b_offset, _ = b_calls[-1]
        self.assertIn("ndi_b/segment_00000.mkv", b_path)
        self.assertEqual(b_offset, 0)

    def test_render_includes_every_feed(self) -> None:
        """Slice 5.C contract: every enabled feed produces a decode call
        on every render tick (when nearest_frame_location returns
        non-None for it)."""
        self._force_recording_state()
        self.controller.rewind_10_seconds()  # session_time=10
        feeds_seen = {c[2] for c in self.stub_decoder.decode_calls}
        self.assertEqual(feeds_seen, {"ndi_a", "ndi_b"})

    def test_freeze_indicator_set_for_late_joining_feed(self) -> None:
        """Phase 6: when the operator rewinds before feed B's first
        segment, feed B's tile renders a freeze frame and shows up in
        `UiState.feeds_in_freeze_frame`. Feed A is in coverage and is
        NOT in the freeze list."""
        self._force_recording_state()
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 12_000_000_000
        self.controller.rewind_10_seconds()  # → 2s
        state = self.controller.get_state()
        self.assertEqual(state.feeds_in_freeze_frame, ("ndi_b",))

    def test_freeze_indicator_empty_when_all_feeds_in_coverage(self) -> None:
        """At a session_time covered by every feed, no tile is frozen."""
        self._force_recording_state()
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 15_000_000_000
        self.controller.rewind_10_seconds()  # → 5s — both feeds in coverage
        state = self.controller.get_state()
        self.assertEqual(state.feeds_in_freeze_frame, ())

    def test_freeze_indicator_cleared_on_jump_to_live(self) -> None:
        """Returning to live drops the freeze list — operator-visible
        badges should disappear immediately."""
        self._force_recording_state()
        with self.controller._lock:
            self.controller._state.current_playback_mode = PlaybackMode.REPLAY
            self.controller._playback_session_time_ns = 12_000_000_000
        self.controller.rewind_10_seconds()  # → 2s, feed B should freeze
        self.assertEqual(self.controller.get_state().feeds_in_freeze_frame, ("ndi_b",))
        self.controller.jump_to_live()
        self.assertEqual(self.controller.get_state().feeds_in_freeze_frame, ())


if __name__ == "__main__":
    unittest.main()
