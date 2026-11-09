"""Operator window: "Elapsed: h:mm" readout and the challenge details wiring."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QMessageBox

from app.config.settings import AppSettings
from app.core.app_state import UiState
from app.core.application_coordinator import _CoordinatorSignals
from app.core.challenge_manager import ChallengeError, ChallengeManager
from app.core.models import FeedDefinition
from app.ui import theme
from app.ui.operator_controls_widget import OperatorControlsWidget, format_elapsed


class FormatElapsedTests(unittest.TestCase):
    def test_hours_and_whole_minutes(self) -> None:
        cases = {
            None: "Elapsed: 0:00",
            0: "Elapsed: 0:00",
            59: "Elapsed: 0:00",  # seconds are dropped, not rounded up
            60: "Elapsed: 0:01",
            754: "Elapsed: 0:12",
            3599: "Elapsed: 0:59",
            3600: "Elapsed: 1:00",
            3725: "Elapsed: 1:02",
            7385: "Elapsed: 2:03",
            36000: "Elapsed: 10:00",
        }
        for seconds, text in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_elapsed(seconds), text)

    def test_negative_reads_as_zero(self) -> None:
        self.assertEqual(format_elapsed(-30), "Elapsed: 0:00")

    def test_minutes_are_always_two_digits(self) -> None:
        self.assertEqual(format_elapsed(5 * 60), "Elapsed: 0:05")


class ElapsedLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.controls = OperatorControlsWidget(button_height=72)
        self.addCleanup(self.controls.deleteLater)

    def test_it_starts_at_zero_and_muted(self) -> None:
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 0:00")
        self.assertIn(theme.TEXT_MUTED, self.controls.elapsed_label.styleSheet())

    def test_set_elapsed_updates_the_text(self) -> None:
        self.controls.set_elapsed(3725, running=True)
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 1:02")

    def test_it_is_bright_while_a_game_runs_and_muted_otherwise(self) -> None:
        self.controls.set_elapsed(120, running=True)
        self.assertIn(theme.TEXT, self.controls.elapsed_label.styleSheet())
        self.assertNotIn(theme.TEXT_MUTED, self.controls.elapsed_label.styleSheet())
        self.controls.set_elapsed(120, running=False)
        self.assertIn(theme.TEXT_MUTED, self.controls.elapsed_label.styleSheet())
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 0:02")  # held

    def test_the_label_sits_directly_above_the_begin_game_button(self) -> None:
        layout = self.controls.layout()
        widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
        index = widgets.index(self.controls.elapsed_label)
        self.assertIs(widgets[index + 1], self.controls.long_recording_button)

    def test_the_label_text_is_only_rewritten_when_it_changes(self) -> None:
        self.controls.set_elapsed(61, running=True)
        with mock.patch.object(self.controls.elapsed_label, "setText") as set_text:
            self.controls.set_elapsed(75, running=True)  # still 0:01
        set_text.assert_not_called()


class ChallengeGatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.controls = OperatorControlsWidget(button_height=72)
        self.addCleanup(self.controls.deleteLater)

    def press_state(self, clip_type="play") -> None:
        self.controls.set_clip_state(
            is_recording=True, has_play_started=True, current_clip_type=clip_type
        )

    def test_challenge_is_available_during_a_play(self) -> None:
        self.press_state()
        self.assertTrue(self.controls.challenge_button.isEnabled())

    def test_challenge_stays_off_while_the_details_screen_is_open(self) -> None:
        self.press_state()
        self.controls.set_challenge_record_open(True)
        self.assertFalse(self.controls.challenge_button.isEnabled())
        # The per-tick refresh must not turn it back on.
        self.press_state()
        self.assertFalse(self.controls.challenge_button.isEnabled())

    def test_challenge_comes_back_after_a_play_starts(self) -> None:
        self.press_state()
        self.controls.set_challenge_record_open(True)
        self.controls.set_challenge_record_open(False)
        self.press_state("play")  # Next Play ended the challenge clip
        self.assertTrue(self.controls.challenge_button.isEnabled())

    def test_finishing_the_details_does_not_allow_back_to_back_challenges(self) -> None:
        # The challenge clip stays open until Next Play / Time-out, and the
        # existing no-back-to-back rule keeps the button off until then.
        self.press_state("challenge")
        self.controls.set_challenge_record_open(False)
        self.press_state("challenge")
        self.assertFalse(self.controls.challenge_button.isEnabled())


def _stub_controller() -> mock.Mock:
    controller = mock.Mock()
    controller.get_state.return_value = UiState()
    controller.available_session_time_range.return_value = (None, None)
    controller.get_playback_session_time_ns.return_value = None
    return controller


def _stub_renderer() -> mock.Mock:
    renderer = mock.Mock()
    renderer.widgets_by_feed_id = {}
    return renderer


class OperatorWindowTests(unittest.TestCase):
    """`MainWindow` (operator role) with a coordinator double."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from app.ui.main_window import MainWindow

        settings = AppSettings()
        self.coordinator = mock.Mock()
        self.coordinator.signals = _CoordinatorSignals()
        self.coordinator.challenge_manager = ChallengeManager(
            settings.challenge_categories, settings.challenge_outcomes
        )
        self.coordinator.challenge_team_names.return_value = {
            "A": "Lakeside Lions",
            "B": "Opponent",
        }
        self.coordinator.challenge_elapsed_seconds.return_value = 12.0
        self.coordinator.game_elapsed_seconds.return_value = None
        feeds = [FeedDefinition(feed_id="a", display_name="A", source_kind="synthetic")]
        self.window = MainWindow(
            settings=settings,
            controller=_stub_controller(),
            output_renderer=_stub_renderer(),
            feeds=feeds,
            controls_role="operator",
            live_only_window=True,
            application_coordinator=self.coordinator,
        )
        self.addCleanup(self.window.deleteLater)
        self.controls = self.window.operator_controls
        assert self.controls is not None

    def start_challenge(self) -> None:
        self.coordinator.signals.challenge_details_started.emit()
        self.addCleanup(self.window._close_challenge_dialog)

    # ---------------------------------------------------- challenge screen

    def test_starting_a_challenge_opens_the_details_screen_with_the_teams(self) -> None:
        self.start_challenge()
        dialog = self.window._challenge_dialog
        assert dialog is not None
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.team_buttons["A"].text(), "Lakeside Lions")
        self.assertEqual(dialog.team_buttons["B"].text(), "Opponent")
        self.assertEqual(len(dialog.category_buttons), 4)
        self.assertEqual(len(dialog.outcome_buttons), 3)

    def test_the_challenge_button_is_held_off_while_it_is_open(self) -> None:
        self.controls.set_clip_state(
            is_recording=True, has_play_started=True, current_clip_type="play"
        )
        self.assertTrue(self.controls.challenge_button.isEnabled())
        self.start_challenge()
        self.assertFalse(self.controls.challenge_button.isEnabled())

    def test_a_second_start_signal_does_not_open_a_second_screen(self) -> None:
        self.start_challenge()
        first = self.window._challenge_dialog
        self.coordinator.signals.challenge_details_started.emit()
        self.assertIs(self.window._challenge_dialog, first)

    def test_done_calls_the_coordinator_with_the_choices(self) -> None:
        self.start_challenge()
        dialog = self.window._challenge_dialog
        dialog.team_buttons["A"].click()
        dialog.category_buttons[1].click()
        dialog.outcome_buttons[2].click()
        dialog.done_button.click()
        self.coordinator.complete_challenge.assert_called_once_with(
            "A", "Player/Ball Contact", "Inconclusive"
        )

    def test_the_screen_closes_when_the_challenge_finishes(self) -> None:
        self.start_challenge()
        dialog = self.window._challenge_dialog
        self.coordinator.signals.challenge_details_finished.emit()
        self.assertIsNone(self.window._challenge_dialog)
        self.assertFalse(dialog.isVisible())

    def test_challenge_can_be_pressed_again_once_the_screen_closes(self) -> None:
        self.start_challenge()
        self.coordinator.signals.challenge_details_finished.emit()
        self.controls.set_clip_state(
            is_recording=True, has_play_started=True, current_clip_type="play"
        )
        self.assertTrue(self.controls.challenge_button.isEnabled())

    def test_a_refused_save_keeps_the_screen_open_and_tells_the_operator(self) -> None:
        self.coordinator.complete_challenge.side_effect = OSError("disk is full")
        self.start_challenge()
        dialog = self.window._challenge_dialog
        dialog.team_buttons["A"].click()
        dialog.category_buttons[0].click()
        dialog.outcome_buttons[0].click()
        with mock.patch.object(QMessageBox, "warning") as warning, self.assertLogs(
            "app.ui.main_window", level="ERROR"
        ):
            dialog.done_button.click()
        warning.assert_called_once()
        self.assertIn("disk is full", warning.call_args.args[2])
        self.assertIs(self.window._challenge_dialog, dialog)
        self.assertTrue(dialog.isVisible())

    def test_an_invalid_choice_is_reported_and_the_screen_stays_open(self) -> None:
        self.coordinator.complete_challenge.side_effect = ChallengeError("Choose a challenge category.")
        self.start_challenge()
        dialog = self.window._challenge_dialog
        dialog.team_buttons["A"].click()
        dialog.category_buttons[0].click()
        dialog.outcome_buttons[0].click()
        with mock.patch.object(QMessageBox, "warning") as warning:
            dialog.done_button.click()
        warning.assert_called_once()
        self.assertIsNotNone(self.window._challenge_dialog)

    def test_discard_goes_to_the_coordinator(self) -> None:
        self.start_challenge()
        dialog = self.window._challenge_dialog
        dialog._confirm = lambda *args: True
        dialog.discard_button.click()
        self.coordinator.discard_challenge.assert_called_once_with()

    def test_closing_the_operator_window_closes_the_screen_too(self) -> None:
        # The dialog is its own top-level window; left open it would keep
        # the application running after the operator window is gone.
        self.start_challenge()
        dialog = self.window._challenge_dialog
        self.window.close()
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.window._challenge_dialog)

    # ------------------------------------------------------ elapsed readout

    def test_the_readout_follows_the_coordinators_game_clock(self) -> None:
        self.coordinator.game_elapsed_seconds.return_value = 3725
        self.window._refresh_elapsed()
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 1:02")

    def test_before_a_game_begins_it_reads_zero(self) -> None:
        self.coordinator.game_elapsed_seconds.return_value = None
        self.window._refresh_elapsed()
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 0:00")

    def test_the_readout_is_bright_only_while_recording(self) -> None:
        self.coordinator.game_elapsed_seconds.return_value = 600
        self.window._render_state(UiState(is_recording=True))
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 0:10")
        self.assertIn(theme.TEXT, self.controls.elapsed_label.styleSheet())
        self.assertNotIn(theme.TEXT_MUTED, self.controls.elapsed_label.styleSheet())
        self.window._render_state(UiState(is_recording=False))
        self.assertIn(theme.TEXT_MUTED, self.controls.elapsed_label.styleSheet())

    def test_it_refreshes_on_a_timer(self) -> None:
        timer = self.window._elapsed_timer
        assert timer is not None
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 1000)
        self.coordinator.game_elapsed_seconds.return_value = 1500
        timer.timeout.emit()
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 0:25")

    def test_a_non_numeric_game_clock_reads_zero(self) -> None:
        self.coordinator.game_elapsed_seconds.return_value = "not a number"
        self.window._refresh_elapsed()
        self.assertEqual(self.controls.elapsed_label.text(), "Elapsed: 0:00")


class RefereeWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_the_referee_window_has_no_elapsed_readout_and_no_challenge_screen(self) -> None:
        from app.ui.main_window import MainWindow

        window = MainWindow(
            settings=AppSettings(),
            controller=_stub_controller(),
            output_renderer=_stub_renderer(),
            feeds=[FeedDefinition(feed_id="a", display_name="A", source_kind="synthetic")],
            controls_role="referee",
            application_coordinator=None,
        )
        self.addCleanup(window.deleteLater)
        self.assertIsNone(window.operator_controls)
        self.assertIsNone(window._elapsed_timer)
        self.assertIsNone(window._challenge_dialog)


if __name__ == "__main__":
    unittest.main()
