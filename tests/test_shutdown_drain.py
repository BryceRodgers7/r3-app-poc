"""Phase 10.F — graceful shutdown drain + transport gating.

Two layers covered:

1. `ApplicationCoordinator.shutdown` — drives the same clean-stop
   sequence the operator's Stop press uses when a game is in flight,
   then proceeds to teardown. Per-feed exceptions during the drain
   are logged but don't deadlock shutdown.

2. Transport methods (`PlaybackController.pause_playback`,
   `rewind_10_seconds`, `replay_current_play`, `jump_to_live`,
   `set_playback_rate`) and coordinator actions
   (`toggle_long_session_recording`, `mark_next_play`) become
   no-ops once `_shutting_down` is set.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.core.application_coordinator import ApplicationCoordinator
from app.core.recording_state import RecordingState, make_recording_state_machine
from app.core.session_state import SessionState


class _ShutdownDrainTests(unittest.TestCase):
    """Build a minimal coordinator with mocked dependencies and exercise
    the shutdown drain decision branches."""

    def _build_coord(self, *, recording: bool, with_play_manager: bool = True):
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._shutting_down = False
        coord.telemetry_hub = mock.Mock()
        coord.referee_controller = mock.Mock()
        coord.operator_controller = mock.Mock()
        coord._session_manager = mock.Mock()
        coord._session_manager.get_active_session_state.return_value = None
        coord.replay_store = mock.Mock()
        coord.session_clock = mock.Mock()
        coord.session_clock.now_session_time_ns.return_value = 12345
        coord.play_manager = mock.Mock() if with_play_manager else None
        recording_manager = mock.Mock()
        recording_manager.is_any_recording.return_value = recording
        recording_manager.recording_state = make_recording_state_machine()
        if recording:
            # Prime the FSM into RECORDING the same way Start would.
            recording_manager.recording_state.transition_to(
                RecordingState.STARTING_RECORDING
            )
            recording_manager.recording_state.transition_to(RecordingState.RECORDING)
        coord._recording_manager = recording_manager
        coord._feed_runtimes = {
            "cam_a": self._mk_runtime("cam_a"),
            "cam_b": self._mk_runtime("cam_b"),
        }
        return coord

    def _mk_runtime(self, feed_id: str) -> mock.Mock:
        rt = mock.Mock()
        rt.feed = mock.Mock(feed_id=feed_id, display_name=feed_id.upper())
        rt.pipeline_manager = mock.Mock()
        return rt

    def test_shutdown_with_recording_drains_via_disable_file_recording(self) -> None:
        coord = self._build_coord(recording=True)
        with mock.patch(
            "app.core.application_coordinator.default_health_log"
        ):
            coord.shutdown()
        # Each feed's pipeline_manager.disable_file_recording was called
        # before its runtime.stop().
        for runtime in coord._feed_runtimes.values():
            runtime.pipeline_manager.disable_file_recording.assert_called_once()
            runtime.stop.assert_called_once()
        # RecordingState ended at NOT_RECORDING.
        self.assertEqual(
            coord._recording_manager.recording_state.state,
            RecordingState.NOT_RECORDING,
        )

    def test_shutdown_without_recording_skips_drain(self) -> None:
        coord = self._build_coord(recording=False)
        with mock.patch("app.core.application_coordinator.default_health_log"):
            coord.shutdown()
        for runtime in coord._feed_runtimes.values():
            runtime.pipeline_manager.disable_file_recording.assert_not_called()
            runtime.stop.assert_called_once()

    def test_shutdown_calls_play_manager_stop_game_when_recording(self) -> None:
        coord = self._build_coord(recording=True)
        with mock.patch("app.core.application_coordinator.default_health_log"):
            coord.shutdown()
        coord.play_manager.stop_game.assert_called_once_with(12345)

    def test_shutdown_handles_disable_file_recording_exception(self) -> None:
        coord = self._build_coord(recording=True)
        coord._feed_runtimes["cam_a"].pipeline_manager.disable_file_recording.side_effect = (
            RuntimeError("simulated splitmuxsink stuck")
        )
        with mock.patch("app.core.application_coordinator.default_health_log"):
            # Must not raise — shutdown is best-effort by design.
            coord.shutdown()
        # The other feed still got its disable + stop calls.
        coord._feed_runtimes["cam_b"].pipeline_manager.disable_file_recording.assert_called_once()
        coord._feed_runtimes["cam_b"].stop.assert_called_once()
        # And the recording state still progressed despite the exception.
        self.assertEqual(
            coord._recording_manager.recording_state.state,
            RecordingState.NOT_RECORDING,
        )

    def test_shutdown_drives_session_state_to_stopped_when_recording(self) -> None:
        coord = self._build_coord(recording=True)
        # Make the active session state machine report RECORDING so the
        # drain transitions it to STOPPED before SessionManager.close().
        from app.core.state_machine import StateMachine

        session_sm = mock.Mock(spec=StateMachine)
        session_sm.state = SessionState.RECORDING
        coord._session_manager.get_active_session_state.return_value = session_sm
        with mock.patch("app.core.application_coordinator.default_health_log"):
            coord.shutdown()
        session_sm.transition_to.assert_called_once_with(SessionState.STOPPED)

    def test_shutdown_resets_replay_store_per_game_scope(self) -> None:
        coord = self._build_coord(recording=True)
        with mock.patch("app.core.application_coordinator.default_health_log"):
            coord.shutdown()
        coord.replay_store.set_current_game_start_session_time.assert_called_once_with(
            None
        )

    def test_shutdown_marks_flag_before_other_teardown(self) -> None:
        coord = self._build_coord(recording=False)
        # Verify ordering: by the time `runtime.stop` is observed,
        # `_shutting_down` is True.
        observed_flags = []

        def record_flag():
            observed_flags.append(coord._shutting_down)

        for rt in coord._feed_runtimes.values():
            rt.stop.side_effect = record_flag
        with mock.patch("app.core.application_coordinator.default_health_log"):
            coord.shutdown()
        self.assertEqual(observed_flags, [True, True])


class CoordinatorTransportGateTests(unittest.TestCase):
    def test_toggle_long_session_recording_no_op_during_shutdown(self) -> None:
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._shutting_down = True
        coord._session_manager = mock.Mock()
        coord.toggle_long_session_recording()
        coord._session_manager.get_active_session_paths.assert_not_called()

    def test_mark_next_play_no_op_during_shutdown(self) -> None:
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._shutting_down = True
        coord._recording_manager = mock.Mock()
        coord.play_manager = mock.Mock()
        coord.mark_next_play()
        coord._recording_manager.is_any_recording.assert_not_called()
        coord.play_manager.mark_next_play.assert_not_called()


class PlaybackControllerTransportGateTests(unittest.TestCase):
    """Transport methods are no-ops once `_shutting_down` is set."""

    def _build_pc_stub(self):
        from app.core.playback_controller import PlaybackController

        pc = PlaybackController.__new__(PlaybackController)
        pc._shutting_down = True
        pc._live_only = False
        pc._play_manager = mock.Mock()
        pc._replay_store = mock.Mock()
        pc._lock = mock.MagicMock()
        pc.signals = mock.Mock()
        # If gating fails, the methods would touch these and raise:
        pc._replay_actions_allowed = mock.Mock(
            side_effect=AssertionError("should not be reached during shutdown")
        )
        return pc

    def test_pause_playback_short_circuits(self) -> None:
        from app.core.playback_controller import PlaybackController

        pc = self._build_pc_stub()
        PlaybackController.pause_playback(pc)
        pc._replay_actions_allowed.assert_not_called()
        pc.signals.state_changed.emit.assert_not_called()

    def test_rewind_10_seconds_short_circuits(self) -> None:
        from app.core.playback_controller import PlaybackController

        pc = self._build_pc_stub()
        PlaybackController.rewind_10_seconds(pc)
        pc._replay_actions_allowed.assert_not_called()

    def test_replay_current_play_short_circuits(self) -> None:
        from app.core.playback_controller import PlaybackController

        pc = self._build_pc_stub()
        PlaybackController.replay_current_play(pc)
        pc._replay_actions_allowed.assert_not_called()

    def test_jump_to_live_short_circuits(self) -> None:
        from app.core.playback_controller import PlaybackController

        pc = self._build_pc_stub()
        # jump_to_live doesn't go through _replay_actions_allowed but
        # should still be a no-op — verify by spying on the lock.
        PlaybackController.jump_to_live(pc)
        pc._lock.__enter__.assert_not_called()

    def test_set_playback_rate_short_circuits(self) -> None:
        from app.core.playback_controller import PlaybackController

        pc = self._build_pc_stub()
        PlaybackController.set_playback_rate(pc, 0.5)
        pc._replay_actions_allowed.assert_not_called()


class PlaybackControllerShutdownSetsFlagTests(unittest.TestCase):
    def test_shutdown_marks_flag_before_timer_stop(self) -> None:
        from app.core.playback_controller import PlaybackController

        pc = PlaybackController.__new__(PlaybackController)
        pc._shutting_down = False
        pc._segment_decoders = {}
        replay_timer = mock.Mock()
        overlay_timer = mock.Mock()

        def assert_flag_set():
            self.assertTrue(pc._shutting_down)

        replay_timer.stop.side_effect = assert_flag_set
        overlay_timer.stop.side_effect = assert_flag_set
        pc._replay_timer = replay_timer
        pc._overlay_timer = overlay_timer
        PlaybackController.shutdown(pc)
        replay_timer.stop.assert_called_once()
        overlay_timer.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
