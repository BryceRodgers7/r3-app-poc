"""Phase 7.H.2: Next Play button (controls widget + main_window wiring)."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.ui.operator_controls_widget import OperatorControlsWidget


class NextPlayButtonStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # QPushButton (a QWidget subclass) needs QApplication, not
        # QCoreApplication. Reuse an existing instance if another test
        # already created one.
        cls._app = QApplication.instance() or QApplication([])

    def test_button_label_is_next_play(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        self.assertEqual(controls.next_play_button.text(), "Next Play")

    def test_button_disabled_at_construction(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        self.assertFalse(controls.next_play_button.isEnabled())

    def test_set_recording_state_true_enables_button(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        controls.set_recording_state(True)
        self.assertTrue(controls.next_play_button.isEnabled())

    def test_set_recording_state_false_disables_button(self) -> None:
        # Toggle on then off — button must respect the False state.
        controls = OperatorControlsWidget(button_height=72)
        controls.set_recording_state(True)
        controls.set_recording_state(False)
        self.assertFalse(controls.next_play_button.isEnabled())

    def test_button_click_emits_next_play_signal(self) -> None:
        # The button is wired so that clicking it (or programmatically
        # emitting `clicked`) raises `next_play_requested`. Use a
        # plain-function receiver — Mock + Qt signal connections can
        # hang in headless test runs.
        controls = OperatorControlsWidget(button_height=72)
        controls.set_recording_state(True)
        emissions: list[None] = []
        controls.next_play_requested.connect(lambda: emissions.append(None))
        controls.next_play_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_legacy_signal_name_is_gone(self) -> None:
        # Phase 7.H.2 renamed `short_segment_advance_requested`. Lock
        # in that the old name no longer exists so nothing
        # accidentally re-introduces it.
        controls = OperatorControlsWidget(button_height=72)
        self.assertFalse(hasattr(controls, "short_segment_advance_requested"))
        self.assertFalse(hasattr(controls, "next_clip_button"))


class CoordinatorMarkNextPlayWiringTests(unittest.TestCase):
    """`coordinator.mark_next_play` is what the button is wired to.
    These tests exercise the coordinator method directly (the UI
    connection is verified in the controls-widget tests above)."""

    def test_mark_next_play_no_op_when_not_recording(self) -> None:
        from app.core.application_coordinator import ApplicationCoordinator
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._recording_manager = mock.Mock()
        coord._recording_manager.is_any_recording.return_value = False
        coord.referee_controller = mock.Mock()
        coord.play_manager = mock.Mock()
        coord.session_clock = mock.Mock()
        coord.mark_next_play()
        coord.play_manager.mark_next_play.assert_not_called()

    def test_mark_next_play_calls_play_manager_when_recording(self) -> None:
        from app.core.application_coordinator import ApplicationCoordinator
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._recording_manager = mock.Mock()
        coord._recording_manager.is_any_recording.return_value = True
        coord.referee_controller = mock.Mock()
        coord.session_clock = mock.Mock()
        coord.session_clock.now_session_time_ns.return_value = 12_000_000_000
        next_play = mock.Mock()
        next_play.play_number = 3
        coord.play_manager = mock.Mock()
        coord.play_manager.mark_next_play.return_value = next_play
        coord.mark_next_play()
        coord.play_manager.mark_next_play.assert_called_once_with(12_000_000_000)
        coord.referee_controller.signals.status_message.emit.assert_called_once_with(
            "Play #3 started."
        )

    def test_mark_next_play_no_op_without_play_manager(self) -> None:
        # Older test/coordinator paths construct without a PlayManager.
        # Method must be a clean no-op.
        from app.core.application_coordinator import ApplicationCoordinator
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._recording_manager = mock.Mock()
        coord._recording_manager.is_any_recording.return_value = True
        coord.referee_controller = mock.Mock()
        coord.play_manager = None
        coord.session_clock = mock.Mock()
        # Should not raise.
        coord.mark_next_play()


if __name__ == "__main__":
    unittest.main()
