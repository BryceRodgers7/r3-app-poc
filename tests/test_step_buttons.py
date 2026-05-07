"""Phase 12.B: Step ◀ / Step ▶ button (controls widget)."""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.ui.controls_widget import ControlsWidget


class StepButtonStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # QPushButton needs QApplication. Reuse an existing instance.
        cls._app = QApplication.instance() or QApplication([])

    def test_button_labels(self) -> None:
        controls = ControlsWidget(button_height=72)
        self.assertEqual(controls.step_back_button.text(), "Step ◀")
        self.assertEqual(controls.step_forward_button.text(), "Step ▶")

    def test_buttons_disabled_at_construction(self) -> None:
        controls = ControlsWidget(button_height=72)
        self.assertFalse(controls.step_back_button.isEnabled())
        self.assertFalse(controls.step_forward_button.isEnabled())

    def test_set_recording_state_true_enables_buttons(self) -> None:
        controls = ControlsWidget(button_height=72)
        controls.set_recording_state(True)
        self.assertTrue(controls.step_back_button.isEnabled())
        self.assertTrue(controls.step_forward_button.isEnabled())

    def test_set_recording_state_false_disables_buttons(self) -> None:
        controls = ControlsWidget(button_height=72)
        controls.set_recording_state(True)
        controls.set_recording_state(False)
        self.assertFalse(controls.step_back_button.isEnabled())
        self.assertFalse(controls.step_forward_button.isEnabled())

    def test_step_back_click_emits_step_back_signal(self) -> None:
        controls = ControlsWidget(button_height=72)
        controls.set_recording_state(True)
        emissions: list[None] = []
        controls.step_back_requested.connect(lambda: emissions.append(None))
        controls.step_back_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_step_forward_click_emits_step_forward_signal(self) -> None:
        controls = ControlsWidget(button_height=72)
        controls.set_recording_state(True)
        emissions: list[None] = []
        controls.step_forward_requested.connect(lambda: emissions.append(None))
        controls.step_forward_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_step_back_and_forward_signals_are_distinct(self) -> None:
        # Locks in that one button's click doesn't accidentally fire
        # both signals — would mask an off-by-one wiring bug.
        controls = ControlsWidget(button_height=72)
        controls.set_recording_state(True)
        back_emissions: list[None] = []
        forward_emissions: list[None] = []
        controls.step_back_requested.connect(lambda: back_emissions.append(None))
        controls.step_forward_requested.connect(
            lambda: forward_emissions.append(None)
        )
        controls.step_back_button.clicked.emit()
        self.assertEqual(len(back_emissions), 1)
        self.assertEqual(len(forward_emissions), 0)
        controls.step_forward_button.clicked.emit()
        self.assertEqual(len(back_emissions), 1)
        self.assertEqual(len(forward_emissions), 1)


if __name__ == "__main__":
    unittest.main()
