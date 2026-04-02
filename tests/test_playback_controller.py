"""Focused replay controller tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest

from app.core.models import MediaFrame, PlaybackMode, SessionPaths
from app.core.playback_controller import PlaybackController
from app.media.preview_output import PreviewOutput
from app.media.replay_buffer import ReplayBuffer, ReplayFrameRef


class _FakePipelineManager:
    def __init__(self, replay_store: ReplayBuffer, source_status_message: str | None = None) -> None:
        self._replay_store = replay_store
        self._live_sample_callback = None
        self._source_status_message = source_status_message
        self.live_activation_count = 0
        self.replay_activation_calls: list[tuple[int, bool]] = []

    def connect_source(self) -> bool:
        return True

    def get_source_name(self) -> str:
        return "Fake Source"

    def get_source_status_message(self) -> str | None:
        return self._source_status_message

    def set_live_sample_callback(self, callback) -> None:
        self._live_sample_callback = callback

    def start_replay_buffer(self, session_paths: SessionPaths) -> None:
        self._replay_store.start(session_paths)

    def start_recording(self, session_paths: SessionPaths) -> None:
        del session_paths

    def start_preview(self) -> None:
        return

    def activate_live_output(self) -> None:
        self.live_activation_count += 1

    def activate_replay_output(self, frame_ref: ReplayFrameRef, paused: bool) -> None:
        self.replay_activation_calls.append((frame_ref.sequence_index, paused))

    def set_video_window_handle(self, window_handle: int) -> None:
        del window_handle

    def refresh_active_video_output(self) -> None:
        return

    def stop_all(self) -> None:
        return


class _FakeRecorder:
    def is_recording(self) -> bool:
        return True

    def stop(self) -> None:
        return


class _FakeSessionManager:
    def __init__(self, session_paths: SessionPaths) -> None:
        self._session_paths = session_paths

    def start_new_session(self, source_name: str) -> SessionPaths:
        del source_name
        return self._session_paths

    def close(self) -> None:
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
        self.replay_store = ReplayBuffer(buffer_duration_seconds=30, jpeg_quality=85)
        self.pipeline_manager = _FakePipelineManager(self.replay_store)
        self.controller = PlaybackController(
            session_manager=_FakeSessionManager(self.session_paths),
            pipeline_manager=self.pipeline_manager,
            preview_output=PreviewOutput(),
            recorder=_FakeRecorder(),
            replay_buffer=self.replay_store,
            default_source_name="Fake Source",
        )
        self.controller.initialize()

        for frame_id in range(240):
            timestamp = 100.0 + (frame_id * (1.0 / 15.0))
            image = np.full((24, 32, 3), frame_id % 255, dtype=np.uint8)
            frame = MediaFrame(
                frame_id=frame_id,
                timestamp=timestamp,
                image=image,
                source_name="Fake Source",
            )
            self.replay_store.append_frame(frame)
        latest_timestamp = self.replay_store.get_latest_timestamp()
        assert latest_timestamp is not None
        self.controller.on_live_sample(latest_timestamp, "Fake Source")

    def tearDown(self) -> None:
        self.controller.shutdown()
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
        self.assertTrue(self.pipeline_manager.replay_activation_calls)
        self.assertFalse(self.pipeline_manager.replay_activation_calls[-1][1])

        self.controller.pause_playback()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.PAUSED)
        self.assertFalse(self.controller._replay_timer.isActive())
        paused_t1 = self.controller._playback_timestamp
        QTest.qWait(400)
        paused_t2 = self.controller._playback_timestamp
        self.assertEqual(paused_t1, paused_t2)
        self.assertTrue(self.pipeline_manager.replay_activation_calls[-1][1])

    def test_repeated_rewind_and_jump_to_live(self) -> None:
        self.controller.rewind_10_seconds()
        first_rewind_timestamp = self.controller._playback_timestamp
        self.controller.pause_playback()
        paused_timestamp = self.controller._playback_timestamp
        self.controller.rewind_10_seconds()
        second_rewind_timestamp = self.controller._playback_timestamp

        self.assertIsNotNone(first_rewind_timestamp)
        self.assertIsNotNone(paused_timestamp)
        self.assertIsNotNone(second_rewind_timestamp)
        assert first_rewind_timestamp is not None
        assert paused_timestamp is not None
        assert second_rewind_timestamp is not None
        self.assertLess(second_rewind_timestamp, paused_timestamp)
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.REPLAY)

        self.controller.jump_to_live()
        self.assertEqual(self.controller.get_state().current_playback_mode, PlaybackMode.LIVE)
        self.assertFalse(self.controller._replay_timer.isActive())
        self.assertGreaterEqual(self.pipeline_manager.live_activation_count, 2)

    def test_source_warning_is_persisted_in_state(self) -> None:
        replay_store = ReplayBuffer(buffer_duration_seconds=30, jpeg_quality=85)
        warning_pipeline_manager = _FakePipelineManager(
            replay_store,
            source_status_message="Camera opened but returned black frames; using synthetic fallback.",
        )
        controller = PlaybackController(
            session_manager=_FakeSessionManager(self.session_paths),
            pipeline_manager=warning_pipeline_manager,
            preview_output=PreviewOutput(),
            recorder=_FakeRecorder(),
            replay_buffer=replay_store,
            default_source_name="Fake Source",
        )

        controller.initialize()
        self.addCleanup(controller.shutdown)

        self.assertEqual(
            controller.get_state().warning_message,
            "Camera opened but returned black frames; using synthetic fallback.",
        )


if __name__ == "__main__":
    unittest.main()
