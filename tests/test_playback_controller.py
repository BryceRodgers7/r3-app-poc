"""Focused playback-session controller tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest

from app.core.models import FeedDefinition, FrameOverlayInfo, MediaFrame, PlaybackMode, SessionPaths
from app.core.playback_controller import PlaybackController
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import OutputRenderer
from app.media.recording_manager import RecordingManager
from app.media.replay_buffer import ReplayBuffer
from app.media.replay_store_manager import ReplayStoreManager
from app.media.source_interface import SourceInterface


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
    def __init__(self, feed_id: str, replay_store: ReplayBuffer) -> None:
        self._feed_id = feed_id
        self._replay_store = replay_store

    def set_frame_callback(self, callback) -> None:
        self._frame_callback = callback

    def set_live_sample_callback(self, callback) -> None:
        self._overlay_callback = callback

    def connect_source(self) -> bool:
        return True

    def start_replay_buffer(self, session_paths: SessionPaths, feed_id: str | None = None) -> None:
        self._replay_store.start(session_paths, feed_id=feed_id or self._feed_id)

    def start_recording(self, session_paths: SessionPaths, feed_id: str | None = None) -> None:
        del session_paths, feed_id

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


class _FakeRecorder:
    def is_recording(self) -> bool:
        return True

    def stop(self) -> None:
        return


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
        self.replay_store = ReplayBuffer(buffer_duration_seconds=30, jpeg_quality=85)
        fake_source = _FakeSource(self.feed.feed_id)
        fake_pipeline = _FakePipelineManager(self.feed.feed_id, self.replay_store)
        fake_recorder = _FakeRecorder()
        self.recording_manager = RecordingManager()
        self.recording_manager.register(self.feed.feed_id, fake_recorder)
        self.replay_store_manager = ReplayStoreManager()
        self.replay_store_manager.register(self.feed.feed_id, self.replay_store)
        self.runtime = FeedRuntime(
            feed=self.feed,
            source=fake_source,
            pipeline_manager=fake_pipeline,
            recorder=fake_recorder,
            replay_store=self.replay_store,
        )
        self.runtime.start(self.session_paths)

        for frame_id in range(240):
            timestamp = 100.0 + (frame_id * (1.0 / 15.0))
            image = np.full((24, 32, 3), frame_id % 255, dtype=np.uint8)
            frame = MediaFrame(
                frame_id=frame_id,
                timestamp=timestamp,
                image=image,
                source_name="Fake Source",
                feed_id=self.feed.feed_id,
            )
            self.replay_store.append_frame(frame)
            self.runtime._on_live_frame(frame)
            self.runtime._on_live_overlay(FrameOverlayInfo.from_media_frame(frame, feed_id=self.feed.feed_id))

        self.renderer = _FakeRenderer()
        self.controller = PlaybackController(
            feed_runtime=self.runtime,
            output_renderer=self.renderer,
            recording_manager=self.recording_manager,
            replay_store_manager=self.replay_store_manager,
            default_source_name="Fake Source",
            session_role="operator",
            live_only=False,
        )
        self.controller.initialize(self.session_paths.session_id)

    def tearDown(self) -> None:
        self.controller.shutdown()
        self.replay_store.stop()
        self._temp_dir.cleanup()

    def test_replay_timer_advances_and_pause_freezes(self) -> None:
        self.controller.rewind_10_seconds()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        self.assertTrue(self.controller._replay_timer.isActive())
        replay_t1 = self.controller._playback_timestamp
        QTest.qWait(600)
        replay_t2 = self.controller._playback_timestamp
        self.assertIsNotNone(replay_t1)
        self.assertIsNotNone(replay_t2)
        assert replay_t1 is not None and replay_t2 is not None
        self.assertGreater(replay_t2, replay_t1)
        self.assertTrue(self.renderer.frames)

        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED)
        self.assertFalse(self.controller._replay_timer.isActive())
        paused_t1 = self.controller._playback_timestamp
        QTest.qWait(400)
        paused_t2 = self.controller._playback_timestamp
        self.assertEqual(paused_t1, paused_t2)

    def test_replay_rate_changes_progression_speed(self) -> None:
        self.controller.rewind_10_seconds()
        self.controller.set_playback_rate(0.5)
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)
        t1 = self.controller._playback_timestamp
        QTest.qWait(600)
        t2 = self.controller._playback_timestamp
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        assert t1 is not None and t2 is not None
        self.assertGreater(t2 - t1, 0.15)
        self.assertLess(t2 - t1, 0.6)

    def test_live_only_controller_rejects_transport_changes(self) -> None:
        renderer = _FakeRenderer()
        controller = PlaybackController(
            feed_runtime=self.runtime,
            output_renderer=renderer,
            recording_manager=self.recording_manager,
            replay_store_manager=self.replay_store_manager,
            default_source_name="Fake Source",
            session_role="program",
            live_only=True,
        )
        controller.initialize(self.session_paths.session_id)
        self.addCleanup(controller.shutdown)

        controller.pause_playback()
        self.assertEqual(controller.get_state().current_playback_mode, PlaybackMode.LIVE)


if __name__ == "__main__":
    unittest.main()
