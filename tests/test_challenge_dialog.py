"""Tests for the challenge details screen (app/ui/challenge_dialog.py)."""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.config.challenge_options import DEFAULT_CATEGORIES, DEFAULT_OUTCOMES
from app.ui.challenge_dialog import ChallengeDialog

TEAMS = {"A": "Lakeside Lions", "B": "Opponent"}


class _Confirm:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _parent, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return self.answer


class _Elapsed:
    def __init__(self, seconds: float | None = 0.0) -> None:
        self.seconds = seconds

    def __call__(self) -> float | None:
        return self.seconds


class _DialogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def make(
        self,
        *,
        categories=DEFAULT_CATEGORIES,
        outcomes=DEFAULT_OUTCOMES,
        teams=TEAMS,
        elapsed: _Elapsed | None = None,
        answer: bool = True,
    ) -> tuple[ChallengeDialog, _Confirm, _Elapsed]:
        confirm = _Confirm(answer)
        clock = elapsed or _Elapsed()
        dialog = ChallengeDialog(teams, categories, outcomes, clock, confirm_fn=confirm)
        self.addCleanup(dialog.deleteLater)
        return dialog, confirm, clock

    @staticmethod
    def choose_all(dialog: ChallengeDialog, team="B", category=2, outcome=1) -> None:
        dialog.team_buttons[team].click()
        dialog.category_buttons[category].click()
        dialog.outcome_buttons[outcome].click()


class ContentTests(_DialogTestCase):
    def test_team_buttons_show_the_stored_team_a_and_team_b_names(self) -> None:
        dialog, _, _ = self.make()
        self.assertEqual(dialog.team_buttons["A"].text(), "Lakeside Lions")
        self.assertEqual(dialog.team_buttons["B"].text(), "Opponent")

    def test_a_team_name_with_an_ampersand_is_shown_whole_and_stored_whole(self) -> None:
        dialog, _, _ = self.make(teams={"A": "Smith & Sons", "B": "Opponent"})
        # Qt would hide a lone "&" as a mnemonic marker; the button text
        # doubles it so the "&" is drawn.
        self.assertEqual(dialog.team_buttons["A"].text(), "Smith && Sons")

    def test_categories_come_from_the_config_in_order(self) -> None:
        dialog, _, _ = self.make()
        values = [str(b.property("value")) for b in dialog.category_buttons]
        self.assertEqual(values, list(DEFAULT_CATEGORIES))

    def test_the_ampersand_in_a_category_is_drawn_and_the_raw_text_is_kept(self) -> None:
        dialog, _, _ = self.make()
        button = dialog.category_buttons[2]
        self.assertEqual(button.text(), "Net / Antenna Faults && Interference")
        button.click()
        self.assertEqual(dialog.selected_category(), "Net / Antenna Faults & Interference")

    def test_outcomes_come_from_the_config_in_order(self) -> None:
        dialog, _, _ = self.make()
        values = [str(b.property("value")) for b in dialog.outcome_buttons]
        self.assertEqual(values, list(DEFAULT_OUTCOMES))

    def test_a_changed_config_changes_the_buttons(self) -> None:
        dialog, _, _ = self.make(categories=["Ball In", "Net"], outcomes=["Upheld", "Overturned"])
        self.assertEqual(len(dialog.category_buttons), 2)
        self.assertEqual(len(dialog.outcome_buttons), 2)
        dialog.outcome_buttons[1].click()
        self.assertEqual(dialog.selected_outcome(), "Overturned")

    def test_nothing_is_preselected(self) -> None:
        dialog, _, _ = self.make()
        self.assertIsNone(dialog.selected_team())
        self.assertIsNone(dialog.selected_category())
        self.assertIsNone(dialog.selected_outcome())

    def test_each_group_allows_one_choice(self) -> None:
        dialog, _, _ = self.make()
        dialog.team_buttons["A"].click()
        dialog.team_buttons["B"].click()
        self.assertEqual(dialog.selected_team(), "B")
        dialog.category_buttons[0].click()
        dialog.category_buttons[3].click()
        self.assertEqual(dialog.selected_category(), DEFAULT_CATEGORIES[3])
        self.assertEqual(sum(b.isChecked() for b in dialog.category_buttons), 1)


class ChallengeDoneTests(_DialogTestCase):
    def test_done_is_off_until_team_category_and_outcome_are_all_chosen(self) -> None:
        dialog, _, _ = self.make()
        self.assertFalse(dialog.done_button.isEnabled())
        dialog.team_buttons["A"].click()
        self.assertFalse(dialog.done_button.isEnabled())
        dialog.category_buttons[0].click()
        self.assertFalse(dialog.done_button.isEnabled())
        dialog.outcome_buttons[0].click()
        self.assertTrue(dialog.done_button.isEnabled())

    def test_each_missing_choice_keeps_done_off(self) -> None:
        for skip in ("team", "category", "outcome"):
            with self.subTest(skip=skip):
                dialog, _, _ = self.make()
                if skip != "team":
                    dialog.team_buttons["A"].click()
                if skip != "category":
                    dialog.category_buttons[0].click()
                if skip != "outcome":
                    dialog.outcome_buttons[0].click()
                self.assertFalse(dialog.done_button.isEnabled())
                self.assertEqual(dialog.missing_details(), [skip])

    def test_the_hint_names_what_is_still_to_choose(self) -> None:
        dialog, _, _ = self.make()
        self.assertEqual(dialog.hint_label.text(), "Still to choose: team, category, outcome.")
        dialog.team_buttons["A"].click()
        self.assertEqual(dialog.hint_label.text(), "Still to choose: category, outcome.")
        dialog.category_buttons[0].click()
        dialog.outcome_buttons[0].click()
        self.assertIn("Challenge Done", dialog.hint_label.text())

    def test_done_asks_the_owner_to_finish_with_the_choices(self) -> None:
        dialog, _, _ = self.make()
        received: list[tuple[str, str, str]] = []
        dialog.finish_requested.connect(lambda *args: received.append(args))
        self.choose_all(dialog, team="B", category=2, outcome=1)
        dialog.done_button.click()
        self.assertEqual(
            received, [("B", "Net / Antenna Faults & Interference", "Reversed")]
        )

    def test_done_does_not_close_the_dialog_by_itself(self) -> None:
        # The owner closes it once the details are stored; if the save fails
        # the dialog must stay up so nothing is lost.
        dialog, _, _ = self.make()
        dialog.show()
        self.choose_all(dialog)
        dialog.done_button.click()
        self.assertTrue(dialog.isVisible())

    def test_a_disabled_done_sends_nothing(self) -> None:
        dialog, _, _ = self.make()
        received: list = []
        dialog.finish_requested.connect(lambda *args: received.append(args))
        dialog._on_done_clicked()  # as if a click slipped through
        self.assertEqual(received, [])


class TimerTests(_DialogTestCase):
    def test_the_timer_shows_the_elapsed_time(self) -> None:
        clock = _Elapsed(0.0)
        dialog, _, _ = self.make(elapsed=clock)
        self.assertEqual(dialog.timer_text(), "0:00")
        clock.seconds = 93.4
        dialog._refresh_timer()
        self.assertEqual(dialog.timer_text(), "1:33")

    def test_the_timer_ticks_on_its_own(self) -> None:
        clock = _Elapsed(5.0)
        dialog, _, _ = self.make(elapsed=clock)
        dialog.show()
        clock.seconds = 65.0
        QTest.qWait(600)  # longer than one tick
        self.assertEqual(dialog.timer_text(), "1:05")

    def test_no_open_challenge_reads_zero(self) -> None:
        dialog, _, _ = self.make(elapsed=_Elapsed(None))
        self.assertEqual(dialog.timer_text(), "0:00")

    def test_an_hour_or_more_shows_hours(self) -> None:
        dialog, _, _ = self.make(elapsed=_Elapsed(3725))
        self.assertEqual(dialog.timer_text(), "1:02:05")

    def test_pressing_done_refreshes_the_timer_first(self) -> None:
        clock = _Elapsed(10.0)
        dialog, _, _ = self.make(elapsed=clock)
        self.choose_all(dialog)
        clock.seconds = 47.0
        dialog.done_button.click()
        self.assertEqual(dialog.timer_text(), "0:47")

    def test_closing_stops_the_timer(self) -> None:
        dialog, _, _ = self.make()
        dialog.show()
        dialog.close_challenge()
        self.assertFalse(dialog._timer.isActive())


class DiscardTests(_DialogTestCase):
    def test_discard_asks_first_and_then_asks_the_owner_to_discard(self) -> None:
        dialog, confirm, _ = self.make(answer=True)
        discarded: list[bool] = []
        dialog.discard_requested.connect(lambda: discarded.append(True))
        dialog.discard_button.click()
        self.assertEqual(len(confirm.calls), 1)
        self.assertIn("Discard", confirm.calls[0][0])
        self.assertEqual(discarded, [True])

    def test_discard_does_nothing_when_the_operator_says_no(self) -> None:
        dialog, confirm, _ = self.make(answer=False)
        discarded: list[bool] = []
        dialog.discard_requested.connect(lambda: discarded.append(True))
        dialog.discard_button.click()
        self.assertEqual(len(confirm.calls), 1)
        self.assertEqual(discarded, [])

    def test_discard_works_before_anything_is_chosen(self) -> None:
        dialog, _, _ = self.make(answer=True)
        self.assertTrue(dialog.discard_button.isEnabled())


class WindowBehaviorTests(_DialogTestCase):
    def test_it_is_not_modal_so_the_referee_window_stays_usable(self) -> None:
        dialog, _, _ = self.make()
        self.assertFalse(dialog.isModal())

    def test_it_has_no_close_button(self) -> None:
        dialog, _, _ = self.make()
        self.assertFalse(dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint)

    def test_escape_does_not_dismiss_it(self) -> None:
        dialog, _, _ = self.make()
        dialog.show()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertTrue(dialog.isVisible())

    def test_the_window_close_request_does_not_dismiss_it(self) -> None:
        dialog, _, _ = self.make()
        dialog.show()
        dialog.close()
        self.assertTrue(dialog.isVisible())

    def test_the_owner_can_close_it(self) -> None:
        dialog, _, _ = self.make()
        dialog.show()
        dialog.close_challenge()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_enter_in_the_dialog_does_not_press_a_button(self) -> None:
        dialog, _, _ = self.make()
        for button in (dialog.done_button, dialog.discard_button, *dialog.category_buttons):
            self.assertFalse(button.autoDefault())


if __name__ == "__main__":
    unittest.main()
