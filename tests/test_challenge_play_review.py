"""Referee: pick which play to review during a challenge.

The operator may press Next Play before the challenge is called, so the
play to review can be earlier than the one the challenge opened on.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.config.prematch_config import MatchDetails
from app.config.settings import AppSettings
from app.core.app_state import UiState
from app.core.application_coordinator import ApplicationCoordinator, _CoordinatorSignals
from app.core.challenge_manager import ChallengeManager
from app.core.clip_manager import ClipManager
from app.core.models import FeedDefinition, Clip, SessionPaths
from app.storage.metadata_db import MetadataDb
from app.ui.play_selector_widget import PlaySelectorWidget

SECOND = 1_000_000_000


# --------------------------------------------------------------------------
# ClipManager.closed_plays (real database)
# --------------------------------------------------------------------------


class ClosedPlaysTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.db = MetadataDb(Path(self._temp.name) / "metadata.db")
        self.addCleanup(self.db.close)
        self.db.create_session(
            session_id="session_001",
            source_name="Test",
            started_at="2026-11-09T00:00:00+00:00",
        )
        self.cm = ClipManager(self.db)

    def start(self, game: str = "game_001", at: int = 0) -> None:
        self.cm.start_game(session_id="session_001", game_subdir=game, start_session_time_ns=at)

    def numbers(self) -> list[int]:
        return [c.play_number for c in self.cm.closed_plays()]

    def test_empty_before_a_game_starts(self) -> None:
        self.assertEqual(self.cm.closed_plays(), [])

    def test_pre_game_is_not_a_play(self) -> None:
        self.start()
        self.assertEqual(self.numbers(), [])

    def test_only_finished_plays_are_listed_oldest_first(self) -> None:
        self.start()
        self.cm.mark_next_play(4 * SECOND)   # play 1 opens
        self.cm.mark_next_play(8 * SECOND)   # play 1 closes, play 2 opens
        self.cm.mark_next_play(12 * SECOND)  # play 2 closes, play 3 opens (still open)
        self.assertEqual(self.numbers(), [1, 2])

    def test_each_play_carries_its_bounds(self) -> None:
        self.start()
        self.cm.mark_next_play(4 * SECOND)
        self.cm.mark_next_play(9 * SECOND)
        (play,) = self.cm.closed_plays()
        self.assertEqual((play.start_session_time_ns, play.end_session_time_ns), (4 * SECOND, 9 * SECOND))

    def test_timeouts_and_challenges_are_not_plays(self) -> None:
        self.start()
        self.cm.mark_next_play(4 * SECOND)     # play 1
        self.cm.mark_timeout(8 * SECOND)       # play 1 closes; timeout opens
        self.cm.mark_next_play(10 * SECOND)    # timeout closes; play 2 opens
        self.cm.mark_challenge(14 * SECOND)    # play 2 closes; challenge opens
        self.assertEqual(self.numbers(), [1, 2])

    def test_the_play_a_challenge_just_closed_is_included(self) -> None:
        self.start()
        self.cm.mark_next_play(4 * SECOND)
        self.cm.mark_challenge(9 * SECOND)
        self.assertEqual(self.numbers(), [1])

    def test_the_early_next_play_case_lists_both_plays(self) -> None:
        # Play 1 holds the call, but Next Play was pressed early, so play 2
        # is tiny when Challenge arrives. Both must be reviewable.
        self.start()
        self.cm.mark_next_play(4 * SECOND)
        self.cm.mark_next_play(10 * SECOND)
        self.cm.mark_challenge(11 * SECOND)
        plays = self.cm.closed_plays()
        self.assertEqual([p.play_number for p in plays], [1, 2])
        self.assertEqual(plays[1].end_session_time_ns - plays[1].start_session_time_ns, 1 * SECOND)

    def test_only_the_current_games_plays_are_listed(self) -> None:
        self.start("game_001")
        self.cm.mark_next_play(4 * SECOND)
        self.cm.mark_next_play(8 * SECOND)
        self.cm.stop_game(12 * SECOND)
        self.start("game_002", at=20 * SECOND)
        self.cm.mark_next_play(24 * SECOND)
        self.cm.mark_next_play(28 * SECOND)
        plays = self.cm.closed_plays()
        self.assertEqual([p.game_subdir for p in plays], ["game_002"])
        self.assertEqual([p.play_number for p in plays], [1])

    def test_empty_again_after_the_game_stops(self) -> None:
        self.start()
        self.cm.mark_next_play(4 * SECOND)
        self.cm.mark_next_play(8 * SECOND)
        self.cm.stop_game(12 * SECOND)
        self.assertEqual(self.cm.closed_plays(), [])


class ChallengeManagerPlayTests(unittest.TestCase):
    def _open(self, play: int | None = 5) -> ChallengeManager:
        manager = ChallengeManager(["Cat"], ["Out"])
        manager.start(
            session_id="s", game_subdir="g", clip_number=9, play_number=play, now_session_time_ns=0
        )
        return manager

    def test_the_reviewed_play_replaces_the_one_it_opened_on(self) -> None:
        manager = self._open(play=5)
        manager.set_play_number(4)
        self.assertEqual(manager.active.play_number, 4)

    def test_the_record_names_the_reviewed_play(self) -> None:
        manager = self._open(play=5)
        manager.set_play_number(4)
        record = manager.build_record(
            challenge_number=1,
            team_side="A",
            team_name="Lions",
            category="Cat",
            outcome="Out",
            now_session_time_ns=SECOND,
        )
        self.assertEqual(record.play_number, 4)

    def test_it_does_nothing_when_no_challenge_is_open(self) -> None:
        manager = ChallengeManager(["Cat"], ["Out"])
        manager.set_play_number(3)  # must not raise
        self.assertIsNone(manager.active)


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------


def _play(number: int, start: int, end: int) -> Clip:
    return Clip(
        session_id="session_001",
        game_subdir="game_001",
        clip_number=number,
        type="play",
        play_number=number,
        start_session_time_ns=start * SECOND,
        end_session_time_ns=end * SECOND,
        created_at="2026-11-09T00:00:00+00:00",
    )


def _challenge_clip() -> Clip:
    return Clip(
        session_id="session_001",
        game_subdir="game_001",
        clip_number=4,
        type="challenge",
        play_number=None,
        start_session_time_ns=30 * SECOND,
        created_at="2026-11-09T00:00:00+00:00",
    )


class _Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def now_session_time_ns(self) -> int:
        return self.now


class CoordinatorReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name) / "session_001"
        (root / "recording").mkdir(parents=True)
        self.root = root
        settings = AppSettings()
        settings.match_details = MatchDetails(
            date(2026, 11, 9), time(19, 0), team_a="Lions", team_b="Opponent"
        )
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._settings = settings
        coord._shutting_down = False
        coord._recording_manager = mock.Mock()
        coord._recording_manager.is_any_recording.return_value = True
        coord.referee_controller = mock.Mock()
        coord.operator_controller = mock.Mock()
        self.clock = _Clock(35 * SECOND)
        coord.session_clock = self.clock
        coord.clip_manager = mock.Mock()
        coord.clip_manager.mark_challenge.return_value = _challenge_clip()
        # Three finished plays; the operator pressed Next Play early, so
        # play 3 is short and the call is in play 2.
        self.plays = [_play(1, 0, 10), _play(2, 10, 28), _play(3, 28, 30)]
        coord.clip_manager.closed_plays.return_value = self.plays
        coord.clip_manager.current_play_number.return_value = 3
        coord.clip_manager.last_play_bounds.return_value = (28 * SECOND, 30 * SECOND)
        coord.signals = _CoordinatorSignals()
        coord._challenge_active = False
        coord.challenge_manager = ChallengeManager(
            settings.challenge_categories, settings.challenge_outcomes
        )
        coord._session_manager = mock.Mock()
        coord._session_manager.get_active_session_paths.return_value = SessionPaths(
            session_id="session_001", root_dir=root, recording_dir=root / "recording"
        )
        self.coord = coord
        self.review_signals: list[tuple[int, list]] = []
        coord.signals.challenge_review_changed.connect(
            lambda selected, plays: self.review_signals.append((selected, list(plays)))
        )

    def done(self):
        return self.coord.complete_challenge("A", "Center-line Faults", "Reversed")

    def test_opening_a_challenge_offers_every_finished_play(self) -> None:
        self.coord.mark_challenge()
        # It opens on the last play, as before; the others are one step away.
        self.assertEqual(self.review_signals, [(3, [1, 2, 3])])

    def test_the_referee_is_still_pinned_to_the_last_play_at_first(self) -> None:
        self.coord.mark_challenge()
        self.coord.referee_controller.replay_current_play.assert_called_once_with(
            28 * SECOND, 30 * SECOND
        )

    def test_stepping_back_re_pins_the_referee_on_that_play(self) -> None:
        self.coord.mark_challenge()
        self.coord.referee_controller.replay_current_play.reset_mock()
        self.assertTrue(self.coord.review_challenge_play(2))
        self.coord.referee_controller.replay_current_play.assert_called_once_with(
            10 * SECOND, 28 * SECOND
        )

    def test_the_selection_is_announced_for_the_selector(self) -> None:
        self.coord.mark_challenge()
        self.coord.review_challenge_play(2)
        self.assertEqual(self.review_signals[-1], (2, [1, 2, 3]))

    def test_the_open_challenge_records_the_play_under_review(self) -> None:
        self.coord.mark_challenge()
        self.assertEqual(self.coord.challenge_manager.active.play_number, 3)
        self.coord.review_challenge_play(2)
        self.assertEqual(self.coord.challenge_manager.active.play_number, 2)

    def test_the_stored_record_names_the_reviewed_play(self) -> None:
        self.coord.mark_challenge()
        self.coord.review_challenge_play(2)
        record = self.done()
        assert record is not None
        self.assertEqual(record.play_number, 2)
        data = json.loads((self.root / "challenges.json").read_text(encoding="utf-8"))
        self.assertEqual(data["challenges"][0]["play_number"], 2)

    def test_the_last_selection_wins(self) -> None:
        self.coord.mark_challenge()
        for number in (2, 1, 3, 1):
            self.coord.review_challenge_play(number)
        record = self.done()
        assert record is not None
        self.assertEqual(record.play_number, 1)

    def test_the_referee_can_step_forward_again(self) -> None:
        self.coord.mark_challenge()
        self.coord.review_challenge_play(1)
        self.assertTrue(self.coord.review_challenge_play(3))
        self.assertEqual(self.coord.challenge_manager.active.play_number, 3)

    def test_an_unknown_play_is_refused(self) -> None:
        self.coord.mark_challenge()
        self.coord.referee_controller.replay_current_play.reset_mock()
        self.assertFalse(self.coord.review_challenge_play(9))
        self.coord.referee_controller.replay_current_play.assert_not_called()
        self.assertEqual(self.coord.challenge_manager.active.play_number, 3)

    def test_selection_only_works_during_a_challenge(self) -> None:
        self.assertFalse(self.coord.review_challenge_play(1))
        self.coord.referee_controller.replay_current_play.assert_not_called()

    def test_selection_stops_working_once_the_challenge_is_done(self) -> None:
        self.coord.mark_challenge()
        self.done()
        self.coord.referee_controller.replay_current_play.reset_mock()
        self.assertFalse(self.coord.review_challenge_play(1))
        self.coord.referee_controller.replay_current_play.assert_not_called()

    def test_selection_stops_working_after_next_play_clears_the_lockout(self) -> None:
        self.coord.mark_challenge()
        self.coord.clip_manager.mark_next_play.return_value = mock.Mock(play_number=4)
        self.coord.mark_next_play()
        self.assertFalse(self.coord.review_challenge_play(1))

    def test_selection_is_ignored_while_shutting_down(self) -> None:
        self.coord.mark_challenge()
        self.coord._shutting_down = True
        self.assertFalse(self.coord.review_challenge_play(1))

    def test_a_status_message_names_the_play(self) -> None:
        messages: list[str] = []
        self.coord.operator_controller.signals.status_message.emit = messages.append
        self.coord.referee_controller.signals.status_message.emit = messages.append
        self.coord.mark_challenge()
        self.coord.review_challenge_play(2)
        self.assertIn("Reviewing play 2.", messages)

    def test_a_failure_listing_the_plays_does_not_break_the_challenge_press(self) -> None:
        self.coord.clip_manager.closed_plays.side_effect = RuntimeError("database is locked")
        with self.assertLogs("app.core.application_coordinator", level="ERROR"):
            self.coord.mark_challenge()
        self.assertTrue(self.coord.challenge_manager.is_active)
        # The selector gets an empty list, so it simply stays disabled.
        self.assertEqual(self.review_signals, [(3, [])])

    def test_reviewing_does_not_touch_the_clip_rows(self) -> None:
        self.coord.mark_challenge()
        self.coord.review_challenge_play(1)
        self.coord.clip_manager.mark_next_play.assert_not_called()
        self.coord.clip_manager.mark_timeout.assert_not_called()


# --------------------------------------------------------------------------
# PlaySelectorWidget
# --------------------------------------------------------------------------


class PlaySelectorWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.selector = PlaySelectorWidget()
        self.addCleanup(self.selector.deleteLater)
        self.requested: list[int] = []
        self.selector.play_selected.connect(self.requested.append)

    def activate(self, selected: int, plays) -> None:
        self.selector.set_challenge_active(True)
        self.selector.set_plays(selected, plays)

    def test_it_starts_greyed_out(self) -> None:
        self.assertFalse(self.selector.earlier_button.isEnabled())
        self.assertFalse(self.selector.later_button.isEnabled())
        self.assertEqual(self.selector.status_label.text(), "—")

    def test_it_reads_play_n_of_m(self) -> None:
        self.activate(2, [1, 2, 3])
        self.assertEqual(self.selector.status_label.text(), "Play 2 of 3")

    def test_on_the_last_play_only_earlier_is_available(self) -> None:
        self.activate(3, [1, 2, 3])
        self.assertTrue(self.selector.earlier_button.isEnabled())
        self.assertFalse(self.selector.later_button.isEnabled())

    def test_on_the_first_play_only_later_is_available(self) -> None:
        self.activate(1, [1, 2, 3])
        self.assertFalse(self.selector.earlier_button.isEnabled())
        self.assertTrue(self.selector.later_button.isEnabled())

    def test_in_the_middle_both_are_available(self) -> None:
        self.activate(2, [1, 2, 3])
        self.assertTrue(self.selector.earlier_button.isEnabled())
        self.assertTrue(self.selector.later_button.isEnabled())

    def test_a_single_play_leaves_nothing_to_step_to(self) -> None:
        self.activate(1, [1])
        self.assertFalse(self.selector.earlier_button.isEnabled())
        self.assertFalse(self.selector.later_button.isEnabled())

    def test_earlier_asks_for_the_previous_play(self) -> None:
        self.activate(3, [1, 2, 3])
        self.selector.earlier_button.click()
        self.assertEqual(self.requested, [2])

    def test_later_asks_for_the_next_play(self) -> None:
        self.activate(1, [1, 2, 3])
        self.selector.later_button.click()
        self.assertEqual(self.requested, [2])

    def test_a_press_does_not_move_the_selection_by_itself(self) -> None:
        # The coordinator re-pins the replay and answers; the widget only
        # shows what it is told, so it can never disagree with the replay.
        self.activate(3, [1, 2, 3])
        self.selector.earlier_button.click()
        self.assertEqual(self.selector.selected_play(), 3)
        self.assertEqual(self.selector.status_label.text(), "Play 3 of 3")
        self.selector.set_plays(2, [1, 2, 3])
        self.assertEqual(self.selector.status_label.text(), "Play 2 of 3")

    def test_stepping_skips_gaps_in_the_play_numbers(self) -> None:
        self.activate(5, [1, 2, 5])
        self.assertEqual(self.selector.earlier_play(), 2)
        self.assertIsNone(self.selector.later_play())

    def test_unsorted_input_is_handled(self) -> None:
        self.activate(2, [3, 1, 2])
        self.assertEqual((self.selector.earlier_play(), self.selector.later_play()), (1, 3))

    def test_it_stays_off_until_a_challenge_is_active(self) -> None:
        self.selector.set_plays(2, [1, 2, 3])
        self.assertFalse(self.selector.earlier_button.isEnabled())
        self.assertFalse(self.selector.later_button.isEnabled())
        self.selector.set_challenge_active(True)
        self.assertTrue(self.selector.earlier_button.isEnabled())

    def test_ending_the_challenge_greys_it_out_and_forgets_the_plays(self) -> None:
        self.activate(2, [1, 2, 3])
        self.selector.set_challenge_active(False)
        self.assertFalse(self.selector.earlier_button.isEnabled())
        self.assertFalse(self.selector.later_button.isEnabled())
        self.assertEqual(self.selector.status_label.text(), "—")
        self.assertIsNone(self.selector.selected_play())

    def test_nothing_is_emitted_while_inactive(self) -> None:
        self.selector.set_plays(3, [1, 2, 3])
        self.selector._step(-1)
        self.assertEqual(self.requested, [])

    def test_a_challenge_with_no_plays_leaves_it_disabled(self) -> None:
        self.activate(3, [])
        self.assertFalse(self.selector.earlier_button.isEnabled())
        self.assertEqual(self.selector.status_label.text(), "—")


class PlaySelectorStyleTests(unittest.TestCase):
    """The selector is a quiet, secondary control with a divider below it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.selector = PlaySelectorWidget()
        self.addCleanup(self.selector.deleteLater)

    def test_the_buttons_are_short(self) -> None:
        for button in (self.selector.earlier_button, self.selector.later_button):
            self.assertLessEqual(button.minimumHeight(), 32)

    def test_the_buttons_are_much_smaller_than_the_transport_buttons(self) -> None:
        self.assertLess(
            self.selector.earlier_button.minimumHeight(),
            AppSettings().touch_button_height / 2,
        )

    def test_the_border_is_one_pixel(self) -> None:
        for button in (self.selector.earlier_button, self.selector.later_button):
            self.assertIn("border-width: 1px", button.styleSheet())

    def test_the_text_is_muted_not_the_bright_transport_white(self) -> None:
        from app.ui import theme

        for button in (self.selector.earlier_button, self.selector.later_button):
            self.assertIn(f"color: {theme.TEXT_MUTED}", button.styleSheet())
            self.assertNotIn(f"color: {theme.TEXT};", button.styleSheet())

    def test_the_font_is_small(self) -> None:
        self.assertIn("font-size: 13px", self.selector.earlier_button.styleSheet())

    def test_a_one_pixel_separator_closes_the_widget(self) -> None:
        layout = self.selector.layout()
        last = layout.itemAt(layout.count() - 1).widget()
        self.assertIs(last, self.selector.separator)
        self.assertEqual(self.selector.separator.height(), 1)

    def test_the_separator_is_drawn_below_the_buttons(self) -> None:
        self.selector.resize(220, 90)
        self.selector.show()
        QApplication.processEvents()
        buttons_bottom = self.selector.earlier_button.geometry().bottom()
        self.assertGreater(self.selector.separator.geometry().top(), buttons_bottom)

    def test_the_separator_spans_the_full_width(self) -> None:
        self.selector.resize(220, 90)
        self.selector.show()
        QApplication.processEvents()
        self.assertEqual(self.selector.separator.width(), self.selector.width())

    def test_the_buttons_still_press(self) -> None:
        requested: list[int] = []
        self.selector.play_selected.connect(requested.append)
        self.selector.set_challenge_active(True)
        self.selector.set_plays(3, [1, 2, 3])
        self.selector.earlier_button.click()
        self.assertEqual(requested, [2])

    def test_an_enabled_button_still_looks_different_from_a_disabled_one(self) -> None:
        # Muted is not invisible: the disabled colors are darker still.
        sheet = self.selector.earlier_button.styleSheet()
        self.assertIn("QPushButton:disabled", sheet)
        self.assertIn("#626b78", sheet)


# --------------------------------------------------------------------------
# Referee MainWindow wiring
# --------------------------------------------------------------------------


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


def _window(role: str):
    from app.ui.main_window import MainWindow

    coord = mock.Mock()
    coord.signals = _CoordinatorSignals()
    coord.telemetry_hub = None
    window = MainWindow(
        settings=AppSettings(),
        controller=_stub_controller(),
        output_renderer=_stub_renderer(),
        feeds=[FeedDefinition(feed_id="a", display_name="A", source_kind="synthetic")],
        controls_role=role,
        live_only_window=(role == "operator"),
        application_coordinator=coord,
    )
    return window, coord


class RefereeWindowSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def make(self):
        window, coord = _window("referee")
        self.addCleanup(window.deleteLater)
        return window, coord

    def test_the_referee_window_has_the_selector_and_the_operator_window_does_not(self) -> None:
        referee, _ = self.make()
        operator, _ = _window("operator")
        self.addCleanup(operator.deleteLater)
        self.assertIsNotNone(referee.play_selector)
        self.assertIsNone(operator.play_selector)

    def test_it_sits_directly_above_the_transport_buttons(self) -> None:
        window, _ = self.make()
        layout = window.play_selector.parentWidget().layout()
        widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
        self.assertIs(widgets[widgets.index(window.play_selector) + 1], window.referee_controls)

    def test_it_is_greyed_out_until_a_challenge_starts(self) -> None:
        window, _ = self.make()
        self.assertFalse(window.play_selector.earlier_button.isEnabled())
        self.assertFalse(window.play_selector.later_button.isEnabled())

    def test_a_challenge_turns_it_on_with_the_offered_plays(self) -> None:
        window, coord = self.make()
        coord.signals.challenge_state_changed.emit(True)
        coord.signals.challenge_review_changed.emit(3, [1, 2, 3])
        selector = window.play_selector
        self.assertEqual(selector.status_label.text(), "Play 3 of 3")
        self.assertTrue(selector.earlier_button.isEnabled())
        self.assertFalse(selector.later_button.isEnabled())

    def test_pressing_earlier_asks_the_coordinator_to_review_that_play(self) -> None:
        window, coord = self.make()
        coord.signals.challenge_state_changed.emit(True)
        coord.signals.challenge_review_changed.emit(3, [1, 2, 3])
        window.play_selector.earlier_button.click()
        coord.review_challenge_play.assert_called_once_with(2)

    def test_the_coordinators_answer_moves_the_selection(self) -> None:
        window, coord = self.make()
        coord.signals.challenge_state_changed.emit(True)
        coord.signals.challenge_review_changed.emit(3, [1, 2, 3])
        coord.signals.challenge_review_changed.emit(2, [1, 2, 3])
        self.assertEqual(window.play_selector.status_label.text(), "Play 2 of 3")
        self.assertTrue(window.play_selector.later_button.isEnabled())

    def test_the_end_of_the_challenge_turns_it_off(self) -> None:
        window, coord = self.make()
        coord.signals.challenge_state_changed.emit(True)
        coord.signals.challenge_review_changed.emit(3, [1, 2, 3])
        coord.signals.challenge_state_changed.emit(False)
        self.assertFalse(window.play_selector.earlier_button.isEnabled())
        self.assertEqual(window.play_selector.status_label.text(), "—")

    def test_the_selector_buttons_are_lighter_than_the_transport_buttons(self) -> None:
        window, _ = self.make()
        self.assertLess(
            window.play_selector.earlier_button.minimumHeight(),
            window.referee_controls.pause_button.minimumHeight(),
        )

    def test_the_transport_gating_still_works(self) -> None:
        window, coord = self.make()
        coord.signals.challenge_state_changed.emit(True)
        self.assertTrue(window.referee_controls.pause_button.isEnabled())
        coord.signals.challenge_state_changed.emit(False)
        self.assertFalse(window.referee_controls.pause_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
