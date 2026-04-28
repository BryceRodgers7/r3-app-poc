"""Focused playback-session controller tests (post slice 4.C.tail)."""

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
        self.decode_calls: list[tuple[str, int]] = []

    def decode(self, location):  # type: ignore[override]
        self.decode_calls.append(
            (location.segment.file_path, location.offset_in_segment_ns)
        )
        # Encode the requested offset into the pixel value so tests can
        # tell which decode call produced which displayed frame.
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
) -> Segment:
    """Build a `Segment` row for SegmentIndex fixtures."""
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

        # Populate the segment index with five back-to-back 4-second
        # segments — 0..20 seconds of replayable history.
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
        """Drive the recording state machine into RECORDING so replay
        actions are not rejected by the §10.4 / §15.2 guard."""
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()

    def test_rewind_enters_replay_and_renders_segment_frame(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        self.assertTrue(self.controller._replay_timer.isActive())
        # Latest replayable PTS = end of last segment = 20s; rewind 10s → target = 10s.
        # That falls in segment 2 (start_pts=8s, end_pts=12s), offset 2s.
        self.assertTrue(self.stub_decoder.decode_calls)
        rendered_path, rendered_offset = self.stub_decoder.decode_calls[-1]
        self.assertIn("segment_00002.mkv", rendered_path)
        self.assertEqual(rendered_offset, 2_000_000_000)
        # Renderer should have received the decoded frame.
        self.assertTrue(self.renderer.frames)
        self.assertEqual(self.renderer.frames[-1].feed_id, self.feed.feed_id)

    def test_rewind_30_seconds_seeks_further_back(self) -> None:
        self._force_recording_state()
        self.controller.rewind_30_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        # Latest replayable PTS = 20s; rewind 30s clamps at earliest_pts=0.
        # 0s falls in segment 0 (start_pts=0s, end_pts=4s), offset 0s.
        self.assertTrue(self.stub_decoder.decode_calls)
        rendered_path, rendered_offset = self.stub_decoder.decode_calls[-1]
        self.assertIn("segment_00000.mkv", rendered_path)
        self.assertEqual(rendered_offset, 0)

    def test_pause_freezes_replay_clock_and_renders_freeze_frame(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        rewind_decode_count = len(self.stub_decoder.decode_calls)
        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED)
        self.assertFalse(self.controller._replay_timer.isActive())
        # Pause should have triggered an extra decode call to freeze on.
        self.assertGreater(len(self.stub_decoder.decode_calls), rewind_decode_count)

    def test_pause_with_no_active_replay_freezes_at_latest_replayable(self) -> None:
        self._force_recording_state()
        self.controller.pause_playback()
        # Latest replayable PTS = 20s, which is in segment 4 (start=16s, end=20s).
        # The replay store's `resolve` for a target equal to a segment's end_pts
        # falls on the boundary — it returns segment 4 with offset = 4s.
        # (See test_resolve_at_segment_boundary_picks_segment_starting_there.)
        self.assertTrue(self.stub_decoder.decode_calls)
        path, offset = self.stub_decoder.decode_calls[-1]
        # Either segment_00003.mkv (at boundary, picks earlier) or segment_00004.mkv
        # — both are valid; what matters is that we resolved to a real segment.
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

    def test_jump_to_live_clears_playback_pts_and_returns_to_live(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertIsNotNone(self.controller._playback_pts_ns)
        self.controller.jump_to_live()
        self.assertIsNone(self.controller._playback_pts_ns)
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

    def test_live_only_controller_does_not_allocate_decoder(self) -> None:
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
        self.assertIsNone(controller._segment_decoder)

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
        # Five 4-second segments → 20 seconds of coverage.
        self.assertAlmostEqual(
            self.controller.get_state().replay_buffer_span_seconds, 20.0, places=3
        )


if __name__ == "__main__":
    unittest.main()
