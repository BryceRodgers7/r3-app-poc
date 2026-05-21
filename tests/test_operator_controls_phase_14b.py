"""Phase 14.B: operator-window button surface + gating + Begin/End modal."""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.core.models import (
    CLIP_TYPE_CHALLENGE,
    CLIP_TYPE_PLAY,
    CLIP_TYPE_PRE_GAME,
    CLIP_TYPE_TIMEOUT,
)
from app.ui.operator_controls_widget import OperatorControlsWidget


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


def _make_widget(
    *,
    confirm_returns: bool = True,
    confirm_calls: list | None = None,
) -> OperatorControlsWidget:
    calls = confirm_calls if confirm_calls is not None else []

    def stub_confirm(parent, title, message):
        calls.append((title, message))
        return confirm_returns

    return OperatorControlsWidget(button_height=72, confirm_fn=stub_confirm)


class ButtonSurfaceTests(_QtTestCase):
    """The widget exposes every Phase 14.B button + signal."""

    def test_buttons_present(self) -> None:
        controls = _make_widget()
        for attr in (
            "next_play_button",
            "timeout_button",
            "challenge_button",
            "mark_play_button",
            "long_recording_button",
        ):
            self.assertTrue(hasattr(controls, attr), f"missing {attr}")

    def test_signals_present(self) -> None:
        controls = _make_widget()
        for attr in (
            "long_recording_toggle_requested",
            "next_play_requested",
            "timeout_requested",
            "challenge_requested",
            "mark_play_toggle_requested",
        ):
            self.assertTrue(hasattr(controls, attr), f"missing signal {attr}")

    def test_initial_labels(self) -> None:
        controls = _make_widget()
        self.assertEqual(controls.next_play_button.text(), "Next Play")
        self.assertEqual(controls.timeout_button.text(), "Time-out")
        self.assertEqual(controls.challenge_button.text(), "Challenge")
        self.assertEqual(controls.mark_play_button.text(), "Mark Play")
        self.assertEqual(controls.long_recording_button.text(), "Begin Game")


class ClipStateGatingTests(_QtTestCase):
    """`set_clip_state` enables/disables per the spec's gating rules."""

    def test_all_disabled_when_not_recording(self) -> None:
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=False,
            has_play_started=False,
            current_clip_type=None,
        )
        self.assertFalse(controls.next_play_button.isEnabled())
        self.assertFalse(controls.timeout_button.isEnabled())
        self.assertFalse(controls.challenge_button.isEnabled())
        self.assertFalse(controls.mark_play_button.isEnabled())

    def test_recording_pre_game_only_enables_next_play_and_mark(self) -> None:
        # During pre-game (no play has started yet), Time-out and
        # Challenge are disabled.
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True,
            has_play_started=False,
            current_clip_type=CLIP_TYPE_PRE_GAME,
        )
        self.assertTrue(controls.next_play_button.isEnabled())
        self.assertTrue(controls.mark_play_button.isEnabled())
        self.assertFalse(controls.timeout_button.isEnabled())
        self.assertFalse(controls.challenge_button.isEnabled())

    def test_recording_with_play_started_enables_timeout_and_challenge(self) -> None:
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True,
            has_play_started=True,
            current_clip_type=CLIP_TYPE_PLAY,
        )
        self.assertTrue(controls.next_play_button.isEnabled())
        self.assertTrue(controls.mark_play_button.isEnabled())
        self.assertTrue(controls.timeout_button.isEnabled())
        self.assertTrue(controls.challenge_button.isEnabled())

    def test_challenge_disabled_during_active_challenge(self) -> None:
        # The current clip is already a challenge — pressing Challenge
        # again is rejected by ClipManager, and the UI should reflect
        # that by greying the button out.
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True,
            has_play_started=True,
            current_clip_type=CLIP_TYPE_CHALLENGE,
        )
        self.assertFalse(controls.challenge_button.isEnabled())
        # Next Play and Time-out remain available — pressing either
        # closes the challenge and opens a new clip.
        self.assertTrue(controls.next_play_button.isEnabled())
        self.assertTrue(controls.timeout_button.isEnabled())

    def test_during_timeout_challenge_is_enabled(self) -> None:
        # Operator started a timeout after play 1; pressing Challenge
        # should be permitted (closes timeout, opens challenge).
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True,
            has_play_started=True,
            current_clip_type=CLIP_TYPE_TIMEOUT,
        )
        self.assertTrue(controls.challenge_button.isEnabled())


class BeginEndGameConfirmTests(_QtTestCase):
    """End Game pops a confirm modal; Begin Game does not."""

    def test_begin_game_emits_directly_without_confirm(self) -> None:
        calls: list = []
        controls = _make_widget(confirm_calls=calls)
        # Not recording → click is a "Begin Game" — no modal.
        emissions: list[None] = []
        controls.long_recording_toggle_requested.connect(
            lambda: emissions.append(None)
        )
        controls.long_recording_button.clicked.emit()
        self.assertEqual(len(emissions), 1)
        self.assertEqual(calls, [])

    def test_end_game_consults_confirm_modal_yes(self) -> None:
        calls: list = []
        controls = _make_widget(confirm_returns=True, confirm_calls=calls)
        controls.set_recording_state(True)
        controls.set_recording_label(True)
        emissions: list[None] = []
        controls.long_recording_toggle_requested.connect(
            lambda: emissions.append(None)
        )
        controls.long_recording_button.clicked.emit()
        # Confirm modal was shown; user answered Yes → emit.
        self.assertEqual(len(calls), 1)
        self.assertIn("end this game", calls[0][1].lower())
        self.assertEqual(len(emissions), 1)

    def test_end_game_no_op_when_confirm_returns_no(self) -> None:
        calls: list = []
        controls = _make_widget(confirm_returns=False, confirm_calls=calls)
        controls.set_recording_state(True)
        emissions: list[None] = []
        controls.long_recording_toggle_requested.connect(
            lambda: emissions.append(None)
        )
        controls.long_recording_button.clicked.emit()
        # Modal shown, user said No → no emission.
        self.assertEqual(len(calls), 1)
        self.assertEqual(emissions, [])


class RecordingLabelStyleTests(_QtTestCase):
    """`set_recording_label` flips both text and color."""

    def test_label_starts_begin_game(self) -> None:
        controls = _make_widget()
        self.assertEqual(controls.long_recording_button.text(), "Begin Game")

    def test_label_flips_to_end_game(self) -> None:
        controls = _make_widget()
        controls.set_recording_label(True)
        self.assertEqual(controls.long_recording_button.text(), "End Game")
        # Red styling applied (loose check — we don't pin exact hex).
        style = controls.long_recording_button.styleSheet()
        self.assertIn("background-color: #c33b3b", style)

    def test_label_flips_back_to_begin_game(self) -> None:
        controls = _make_widget()
        controls.set_recording_label(True)
        controls.set_recording_label(False)
        self.assertEqual(controls.long_recording_button.text(), "Begin Game")
        style = controls.long_recording_button.styleSheet()
        self.assertIn("background-color: #2c8a3a", style)


class SignalEmissionTests(_QtTestCase):
    """Each transport button emits its signal on click."""

    def test_next_play(self) -> None:
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True, has_play_started=False, current_clip_type=None,
        )
        emissions: list[None] = []
        controls.next_play_requested.connect(lambda: emissions.append(None))
        controls.next_play_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_timeout(self) -> None:
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True, has_play_started=True,
            current_clip_type=CLIP_TYPE_PLAY,
        )
        emissions: list[None] = []
        controls.timeout_requested.connect(lambda: emissions.append(None))
        controls.timeout_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_challenge(self) -> None:
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True, has_play_started=True,
            current_clip_type=CLIP_TYPE_PLAY,
        )
        emissions: list[None] = []
        controls.challenge_requested.connect(lambda: emissions.append(None))
        controls.challenge_button.clicked.emit()
        self.assertEqual(len(emissions), 1)

    def test_mark_play(self) -> None:
        controls = _make_widget()
        controls.set_clip_state(
            is_recording=True, has_play_started=False, current_clip_type=None,
        )
        emissions: list[None] = []
        controls.mark_play_toggle_requested.connect(
            lambda: emissions.append(None)
        )
        controls.mark_play_button.clicked.emit()
        self.assertEqual(len(emissions), 1)


if __name__ == "__main__":
    unittest.main()
