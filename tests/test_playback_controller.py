"""Focused playback-session controller tests (post slice 4.D)."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PySide6.QtCore import QCoreApplication

from app.core.models import FeedDefinition, FrameOverlayInfo, MediaFrame, PlaybackMode, SessionPaths
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import OutputRenderer
from app.media.recording_manager import RecordingManager
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
        self.segment_index = SegmentIndex()
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)
        self.runtime = FeedRuntime(
            feed=self.feed,
            source=fake_source,
            pipeline_manager=fake_pipeline,
        )
        self.runtime.start(self.session_paths)

        # Drive the runtime through enough live frames that
        # `_latest_live_timestamp` is populated — that's what the
        # transport methods anchor their replay-clock target on.
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

    def test_rewind_enters_replay_when_recording(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        self.assertTrue(self.controller._replay_timer.isActive())

    def test_pause_freezes_replay_clock(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED)
        self.assertFalse(self.controller._replay_timer.isActive())

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

    def test_refresh_recording_state_emits(self) -> None:
        self.controller.refresh_recording_state()
        self.assertFalse(self.controller.get_state().is_recording)

    def test_rewind_rejected_when_recording_not_active(self) -> None:
        # No _force_recording_state() — recording state is NOT_RECORDING.
        # Rewind must be a no-op and not switch into REPLAY.
        before_mode = self.controller.get_state().current_playback_mode
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, before_mode)

    def test_pause_rejected_when_recording_not_active(self) -> None:
        before_mode = self.controller.get_state().current_playback_mode
        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, before_mode)

    def test_recording_stop_mid_replay_snaps_back_to_live(self) -> None:
        self._force_recording_state()
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        # Now simulate the operator stopping recording while replay is active.
        self.recording_manager.recording_state.force(RecordingState.NOT_RECORDING)
        self.controller.refresh_recording_state()
        self.assertEqual(self.controller.replay_state.state, ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING)
        self.assertNotEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)


if __name__ == "__main__":
    unittest.main()
