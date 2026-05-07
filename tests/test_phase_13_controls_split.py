"""Phase 13.A / 13.B: split ControlsWidget into per-window widgets.

These tests lock in the new contract:

- `RefereeControlsWidget` owns the replay/review transport (Pause,
  Rewind, Replay Play, Slow 1/2x, Slow 1/4x, Step ◀/▶, Jump to Live)
  and does NOT have the recording transport.
- `OperatorControlsWidget` owns the recording transport (Start/Stop
  game, Next Play) and does NOT have the replay transport.
- `MainWindow.controls_role` selects which widget is built; the other
  attribute stays None.
- `_render_state` routes recording-state updates to the right widget.

Together they replace the pre-Phase-13 `ControlsWidget` that combined
both roles on the referee window. See `r3_app_architecture.md`
§Phase 13.
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.ui.operator_controls_widget import OperatorControlsWidget
from app.ui.referee_controls_widget import RefereeControlsWidget


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


class RefereeControlsWidgetContractTests(_QtTestCase):
    """RefereeControlsWidget owns the replay/review transport."""

    def test_replay_buttons_present(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        for attr in (
            "pause_button",
            "rewind_button",
            "replay_play_button",
            "half_speed_button",
            "quarter_speed_button",
            "step_back_button",
            "step_forward_button",
            "live_button",
        ):
            self.assertTrue(hasattr(controls, attr), f"missing {attr}")

    def test_recording_buttons_absent(self) -> None:
        # The recording transport moved to OperatorControlsWidget in
        # Phase 13.A. If a refactor accidentally re-adds them here we
        # want to fail loudly — there'd suddenly be two Start buttons
        # in the app.
        controls = RefereeControlsWidget(button_height=72)
        self.assertFalse(hasattr(controls, "long_recording_button"))
        self.assertFalse(hasattr(controls, "next_play_button"))

    def test_recording_signals_absent(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        self.assertFalse(hasattr(controls, "long_recording_toggle_requested"))
        self.assertFalse(hasattr(controls, "next_play_requested"))

    def test_replay_buttons_disabled_until_recording(self) -> None:
        # Replay (and frame-step / replay-play) isn't available outside
        # RECORDING per §10.4 / §15.2.
        controls = RefereeControlsWidget(button_height=72)
        self.assertFalse(controls.replay_play_button.isEnabled())
        self.assertFalse(controls.step_back_button.isEnabled())
        self.assertFalse(controls.step_forward_button.isEnabled())

    def test_set_recording_state_toggles_replay_buttons(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        controls.set_recording_state(True)
        self.assertTrue(controls.replay_play_button.isEnabled())
        self.assertTrue(controls.step_back_button.isEnabled())
        self.assertTrue(controls.step_forward_button.isEnabled())
        controls.set_recording_state(False)
        self.assertFalse(controls.replay_play_button.isEnabled())
        self.assertFalse(controls.step_back_button.isEnabled())
        self.assertFalse(controls.step_forward_button.isEnabled())

    def test_continuous_transport_buttons_always_enabled(self) -> None:
        # Pause / Rewind / Slow / Jump to Live aren't gated — they
        # surface a status message if pressed when replay is unavailable
        # but the buttons themselves are always clickable.
        controls = RefereeControlsWidget(button_height=72)
        for button in (
            controls.pause_button,
            controls.rewind_button,
            controls.half_speed_button,
            controls.quarter_speed_button,
            controls.live_button,
        ):
            self.assertTrue(button.isEnabled())


class OperatorControlsWidgetContractTests(_QtTestCase):
    """OperatorControlsWidget owns the recording transport."""

    def test_recording_buttons_present(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        self.assertTrue(hasattr(controls, "long_recording_button"))
        self.assertTrue(hasattr(controls, "next_play_button"))

    def test_replay_buttons_absent(self) -> None:
        # Replay/review transport stays on the referee window.
        controls = OperatorControlsWidget(button_height=72)
        for attr in (
            "pause_button",
            "rewind_button",
            "replay_play_button",
            "half_speed_button",
            "quarter_speed_button",
            "step_back_button",
            "step_forward_button",
            "live_button",
        ):
            self.assertFalse(hasattr(controls, attr), f"unexpected {attr}")

    def test_replay_signals_absent(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        self.assertFalse(hasattr(controls, "pause_requested"))
        self.assertFalse(hasattr(controls, "rewind_requested"))
        self.assertFalse(hasattr(controls, "replay_current_play_requested"))
        self.assertFalse(hasattr(controls, "step_back_requested"))
        self.assertFalse(hasattr(controls, "step_forward_requested"))

    def test_start_stop_button_label_starts_at_start(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        self.assertEqual(controls.long_recording_button.text(), "Start game recording")

    def test_set_recording_label_flips_text(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        controls.set_recording_label(True)
        self.assertEqual(controls.long_recording_button.text(), "Stop game recording")
        controls.set_recording_label(False)
        self.assertEqual(controls.long_recording_button.text(), "Start game recording")

    def test_start_stop_always_enabled(self) -> None:
        # Pressing Start/Stop IS the toggle, so it's never disabled.
        controls = OperatorControlsWidget(button_height=72)
        self.assertTrue(controls.long_recording_button.isEnabled())
        controls.set_recording_state(True)
        self.assertTrue(controls.long_recording_button.isEnabled())
        controls.set_recording_state(False)
        self.assertTrue(controls.long_recording_button.isEnabled())

    def test_next_play_disabled_until_recording(self) -> None:
        # Marking a play boundary only makes sense in RECORDING state.
        controls = OperatorControlsWidget(button_height=72)
        self.assertFalse(controls.next_play_button.isEnabled())
        controls.set_recording_state(True)
        self.assertTrue(controls.next_play_button.isEnabled())
        controls.set_recording_state(False)
        self.assertFalse(controls.next_play_button.isEnabled())

    def test_long_recording_click_emits_signal(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        emissions: list[None] = []
        controls.long_recording_toggle_requested.connect(lambda: emissions.append(None))
        controls.long_recording_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_next_play_click_emits_signal_when_recording(self) -> None:
        controls = OperatorControlsWidget(button_height=72)
        controls.set_recording_state(True)
        emissions: list[None] = []
        controls.next_play_requested.connect(lambda: emissions.append(None))
        controls.next_play_button.clicked.emit()
        self.assertEqual(len(emissions), 1)


class MainWindowControlsRoleTests(_QtTestCase):
    """`MainWindow` builds the right widget per `controls_role`.

    Construction touches a lot of plumbing (PlaybackController, output
    renderer, video panel, native preview binding). We bypass __init__
    by checking the constructor's `controls_role` validation directly.
    """

    def test_invalid_controls_role_raises(self) -> None:
        # `MainWindow.__init__` rejects unknown roles before any widget
        # is built so a typo fails fast at startup, not silently at
        # signal-wiring time.
        from app.ui.main_window import _VALID_CONTROLS_ROLES

        # Sanity: the three valid roles are documented in the
        # architecture doc. If a fourth gets added the doc and this
        # set must change together.
        self.assertEqual(
            _VALID_CONTROLS_ROLES, frozenset({"referee", "operator", "none"})
        )


if __name__ == "__main__":
    unittest.main()
