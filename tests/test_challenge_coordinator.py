"""Coordinator wiring for the challenge details and the game-elapsed clock."""

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
from app.core.application_coordinator import ApplicationCoordinator, _CoordinatorSignals
from app.core.challenge_manager import (
    CHALLENGES_FILENAME,
    ChallengeError,
    ChallengeManager,
)
from app.core.models import Clip, SessionPaths

SECOND = 1_000_000_000


class _Clock:
    """Session clock whose time the test moves."""

    def __init__(self, now: int = 0) -> None:
        self.now = now

    def now_session_time_ns(self) -> int:
        return self.now


def _challenge_clip(clip_number: int = 5, game: str = "game_001") -> Clip:
    return Clip(
        session_id="session_001",
        game_subdir=game,
        clip_number=clip_number,
        type="challenge",
        play_number=None,
        start_session_time_ns=0,
        created_at="2026-11-09T00:00:00+00:00",
    )


class _CoordinatorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.session_root = Path(self._temp.name) / "session_001"
        (self.session_root / "recording").mkdir(parents=True)
        self.paths = SessionPaths(
            session_id="session_001",
            root_dir=self.session_root,
            recording_dir=self.session_root / "recording",
        )
        self.clock = _Clock(100 * SECOND)

        settings = AppSettings()
        settings.match_details = MatchDetails(
            match_date=date(2026, 11, 9),
            scheduled_start=time(19, 0),
            team_a="Lakeside Lions",
            team_b="Opponent",
        )
        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._settings = settings
        coord._shutting_down = False
        coord._recording_manager = mock.Mock()
        coord._recording_manager.is_any_recording.return_value = True
        coord.referee_controller = mock.Mock()
        coord.operator_controller = mock.Mock()
        coord.session_clock = self.clock
        coord.clip_manager = mock.Mock()
        coord.clip_manager.mark_challenge.return_value = _challenge_clip()
        coord.clip_manager.current_play_number.return_value = 4
        coord.clip_manager.last_play_bounds.return_value = (50 * SECOND, 90 * SECOND)
        coord.signals = _CoordinatorSignals()
        coord._challenge_active = False
        coord.challenge_manager = ChallengeManager(
            settings.challenge_categories, settings.challenge_outcomes
        )
        coord._session_manager = mock.Mock()
        coord._session_manager.get_active_session_paths.return_value = self.paths
        coord._game_started_ns = None
        coord._game_ended_ns = None
        self.coord = coord

        self.started: list[bool] = []
        self.finished: list[bool] = []
        coord.signals.challenge_details_started.connect(lambda: self.started.append(True))
        coord.signals.challenge_details_finished.connect(lambda: self.finished.append(True))

    def challenges_file(self) -> Path:
        return self.session_root / CHALLENGES_FILENAME

    def done(self, **overrides):
        values = dict(
            team_side="B",
            category="Center-line Faults",
            outcome="Confirmed",
        )
        values.update(overrides)
        return self.coord.complete_challenge(**values)


class StartChallengeTests(_CoordinatorTestCase):
    def test_pressing_challenge_starts_the_timer_and_the_details_screen(self) -> None:
        self.coord.mark_challenge()
        self.assertEqual(self.started, [True])
        active = self.coord.challenge_manager.active
        assert active is not None
        self.assertEqual(active.started_session_time_ns, 100 * SECOND)
        self.assertEqual(active.game_subdir, "game_001")
        self.assertEqual(active.clip_number, 5)
        # The challenged play is the last one opened, not the challenge clip.
        self.assertEqual(active.play_number, 4)

    def test_the_referee_lockout_still_installs_as_before(self) -> None:
        emitted: list[bool] = []
        self.coord.signals.challenge_state_changed.connect(emitted.append)
        self.coord.mark_challenge()
        self.coord.referee_controller.replay_current_play.assert_called_once_with(
            50 * SECOND, 90 * SECOND
        )
        self.assertEqual(emitted, [True])

    def test_a_rejected_challenge_opens_nothing(self) -> None:
        self.coord.clip_manager.mark_challenge.return_value = None
        self.coord.mark_challenge()
        self.assertEqual(self.started, [])
        self.assertFalse(self.coord.challenge_manager.is_active)

    def test_not_recording_opens_nothing(self) -> None:
        self.coord._recording_manager.is_any_recording.return_value = False
        self.coord.mark_challenge()
        self.assertEqual(self.started, [])
        self.coord.clip_manager.mark_challenge.assert_not_called()

    def test_a_second_press_while_the_details_screen_is_open_is_refused(self) -> None:
        self.coord.mark_challenge()
        self.coord.clip_manager.mark_challenge.reset_mock()
        self.coord.mark_challenge()
        self.coord.clip_manager.mark_challenge.assert_not_called()
        self.assertEqual(self.started, [True])  # still just the first

    def test_the_timer_reads_from_the_press_to_now(self) -> None:
        self.assertIsNone(self.coord.challenge_elapsed_seconds())
        self.coord.mark_challenge()
        self.clock.now += 42 * SECOND
        self.assertEqual(self.coord.challenge_elapsed_seconds(), 42.0)


class TeamNameTests(_CoordinatorTestCase):
    def test_the_teams_are_the_names_stored_for_team_a_and_team_b(self) -> None:
        self.assertEqual(
            self.coord.challenge_team_names(), {"A": "Lakeside Lions", "B": "Opponent"}
        )

    def test_blank_names_fall_back_to_team_a_and_team_b(self) -> None:
        self.coord._settings.match_details.team_a = None
        self.coord._settings.match_details.team_b = None
        self.assertEqual(self.coord.challenge_team_names(), {"A": "Team A", "B": "Team B"})

    def test_no_match_at_all_falls_back_too(self) -> None:
        self.coord._settings.match_details = None
        self.assertEqual(self.coord.challenge_team_names(), {"A": "Team A", "B": "Team B"})


class CompleteChallengeTests(_CoordinatorTestCase):
    def test_done_stores_the_details_and_the_elapsed_time(self) -> None:
        self.coord.mark_challenge()
        self.clock.now += 93 * SECOND + 500_000_000
        record = self.done(team_side="B", category="Center-line Faults", outcome="Confirmed")
        assert record is not None
        self.assertEqual(record.team, "Opponent")
        self.assertEqual(record.elapsed_seconds, 93.5)

        data = json.loads(self.challenges_file().read_text(encoding="utf-8"))
        (stored,) = data["challenges"]
        self.assertEqual(stored["team_side"], "B")
        self.assertEqual(stored["team"], "Opponent")
        self.assertEqual(stored["category"], "Center-line Faults")
        self.assertEqual(stored["outcome"], "Confirmed")
        self.assertEqual(stored["elapsed_seconds"], 93.5)
        self.assertEqual(stored["play_number"], 4)
        self.assertEqual(stored["challenge_number"], 1)
        self.assertEqual(stored["game_subdir"], "game_001")

    def test_done_closes_the_challenge_and_the_screen(self) -> None:
        self.coord.mark_challenge()
        self.done()
        self.assertFalse(self.coord.challenge_manager.is_active)
        self.assertEqual(self.finished, [True])
        self.assertIsNone(self.coord.challenge_elapsed_seconds())

    def test_done_ends_the_referee_lockout_and_returns_to_live(self) -> None:
        emitted: list[bool] = []
        self.coord.signals.challenge_state_changed.connect(emitted.append)
        self.coord.mark_challenge()
        self.done()
        self.coord.referee_controller.clear_clip_bounds.assert_called_once()
        self.coord.referee_controller.jump_to_live.assert_called_once()
        self.assertEqual(emitted, [True, False])

    def test_a_later_next_play_does_not_clear_the_lockout_twice(self) -> None:
        emitted: list[bool] = []
        self.coord.signals.challenge_state_changed.connect(emitted.append)
        self.coord.mark_challenge()
        self.done()
        self.coord.clip_manager.mark_next_play.return_value = mock.Mock(play_number=5)
        self.coord.mark_next_play()
        self.assertEqual(emitted, [True, False])  # no second False

    def test_done_does_not_touch_the_clip_rows(self) -> None:
        self.coord.mark_challenge()
        self.done()
        self.coord.clip_manager.mark_next_play.assert_not_called()
        self.coord.clip_manager.mark_timeout.assert_not_called()
        self.coord.clip_manager.stop_game.assert_not_called()

    def test_done_with_nothing_open_does_nothing(self) -> None:
        self.assertIsNone(self.done())
        self.assertFalse(self.challenges_file().exists())

    def test_a_second_done_does_not_write_a_second_record(self) -> None:
        self.coord.mark_challenge()
        self.done()
        self.assertIsNone(self.done())
        data = json.loads(self.challenges_file().read_text(encoding="utf-8"))
        self.assertEqual(len(data["challenges"]), 1)

    def test_an_unknown_category_is_refused_and_the_challenge_stays_open(self) -> None:
        self.coord.mark_challenge()
        with self.assertRaises(ChallengeError):
            self.done(category="Not a category")
        self.assertTrue(self.coord.challenge_manager.is_active)
        self.assertFalse(self.challenges_file().exists())
        self.assertEqual(self.finished, [])

    def test_a_missing_choice_is_refused(self) -> None:
        self.coord.mark_challenge()
        for overrides in ({"team_side": ""}, {"category": ""}, {"outcome": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ChallengeError):
                self.done(**overrides)

    def test_a_failed_save_keeps_the_challenge_open_and_the_timer_frozen(self) -> None:
        self.coord.mark_challenge()
        self.clock.now += 60 * SECOND
        # Block the session folder so the write fails.
        blocker = Path(self._temp.name) / "blocker"
        blocker.write_text("not a folder", encoding="utf-8")
        real_paths = self.coord._session_manager.get_active_session_paths.return_value
        self.coord._session_manager.get_active_session_paths.return_value = SessionPaths(
            session_id="session_001", root_dir=blocker, recording_dir=blocker / "recording"
        )
        with self.assertRaises(OSError):
            self.done()
        self.assertTrue(self.coord.challenge_manager.is_active)
        self.assertEqual(self.finished, [])

        # Fixed: the retry succeeds and keeps the time of the first press.
        self.clock.now += 300 * SECOND
        self.coord._session_manager.get_active_session_paths.return_value = real_paths
        record = self.done()
        assert record is not None
        self.assertEqual(record.elapsed_seconds, 60.0)

    def test_challenges_are_numbered_within_the_game(self) -> None:
        for expected in (1, 2):
            self.coord.mark_challenge()
            record = self.done()
            assert record is not None
            self.assertEqual(record.challenge_number, expected)
            # The challenge clip is still open in real use; the next press
            # follows a Next Play. The stub clip manager does not care.

    def test_numbering_restarts_in_the_next_game(self) -> None:
        self.coord.mark_challenge()
        self.done()
        self.coord.clip_manager.mark_challenge.return_value = _challenge_clip(2, "game_002")
        self.coord.mark_challenge()
        record = self.done()
        assert record is not None
        self.assertEqual((record.game_subdir, record.challenge_number), ("game_002", 1))

    def test_a_resumed_session_continues_the_numbering_from_the_file(self) -> None:
        self.coord.mark_challenge()
        self.done()
        # A new run adopts the session: new manager, same file.
        self.coord.challenge_manager = ChallengeManager(
            self.coord._settings.challenge_categories, self.coord._settings.challenge_outcomes
        )
        self.coord.mark_challenge()
        record = self.done()
        assert record is not None
        self.assertEqual(record.challenge_number, 2)


class DiscardChallengeTests(_CoordinatorTestCase):
    def test_discard_drops_the_challenge_without_a_record(self) -> None:
        self.coord.mark_challenge()
        self.coord.discard_challenge()
        self.assertFalse(self.coord.challenge_manager.is_active)
        self.assertEqual(self.finished, [True])
        self.assertFalse(self.challenges_file().exists())

    def test_discard_releases_the_referee(self) -> None:
        self.coord.mark_challenge()
        self.coord.discard_challenge()
        self.coord.referee_controller.jump_to_live.assert_called_once()

    def test_discard_with_nothing_open_does_nothing(self) -> None:
        self.coord.discard_challenge()
        self.assertEqual(self.finished, [])

    def test_a_new_challenge_can_start_after_a_discard(self) -> None:
        self.coord.mark_challenge()
        self.coord.discard_challenge()
        self.coord.mark_challenge()
        self.assertTrue(self.coord.challenge_manager.is_active)
        self.assertEqual(self.started, [True, True])


class ShutdownTests(_CoordinatorTestCase):
    def test_shutdown_warns_when_a_challenge_is_unfinished(self) -> None:
        self.coord.mark_challenge()
        self.coord.telemetry_hub = mock.Mock()
        self.coord._feed_runtimes = {}
        self.coord._session_manager = mock.Mock()
        with mock.patch.object(self.coord, "_drain_active_recording_locked"):
            with mock.patch("app.core.application_coordinator.default_health_log"):
                with self.assertLogs("app.core.application_coordinator", level="WARNING") as logs:
                    self.coord.shutdown()
        self.assertTrue(any("unfinished challenge" in line for line in logs.output))


class GameElapsedTests(_CoordinatorTestCase):
    def test_none_until_a_game_begins(self) -> None:
        self.assertIsNone(self.coord.game_elapsed_seconds())

    def test_counts_from_begin_game_while_the_game_runs(self) -> None:
        self.coord._game_started_ns = 100 * SECOND
        self.clock.now = 100 * SECOND + 65 * 60 * SECOND
        self.assertEqual(self.coord.game_elapsed_seconds(), 65 * 60.0)

    def test_holds_its_final_value_after_end_game(self) -> None:
        self.coord._game_started_ns = 100 * SECOND
        self.coord._game_ended_ns = 100 * SECOND + 2400 * SECOND
        self.clock.now = 100 * SECOND + 9000 * SECOND
        self.assertEqual(self.coord.game_elapsed_seconds(), 2400.0)

    def _toggle(self):
        """Run Begin Game / End Game through the real toggle method."""
        coord = self.coord
        coord._recording_manager.recording_state = mock.Mock()
        coord._session_manager.get_active_session_state.return_value = mock.Mock()
        coord._feed_runtimes = {}
        coord._resume_continuation = None
        coord.replay_store = mock.Mock()
        coord.signals = _CoordinatorSignals()
        coord._session_manager.get_active_session_paths.return_value = self.paths
        with mock.patch.object(coord, "_check_disk_preflight", return_value=True), mock.patch.object(
            coord, "_clear_disk_full_blocked_marker"
        ):
            coord.toggle_long_session_recording()

    def test_begin_game_starts_the_clock_and_end_game_stops_it(self) -> None:
        self.coord._recording_manager.is_any_recording.return_value = False
        self.clock.now = 500 * SECOND
        self._toggle()  # Begin Game
        self.assertEqual(self.coord._game_started_ns, 500 * SECOND)
        self.assertIsNone(self.coord._game_ended_ns)
        self.clock.now = 500 * SECOND + 125 * SECOND
        self.assertEqual(self.coord.game_elapsed_seconds(), 125.0)

        self.coord._recording_manager.is_any_recording.return_value = True
        self.clock.now = 500 * SECOND + 3000 * SECOND
        self._toggle()  # End Game
        self.assertEqual(self.coord._game_ended_ns, 500 * SECOND + 3000 * SECOND)
        self.clock.now += 99999 * SECOND
        self.assertEqual(self.coord.game_elapsed_seconds(), 3000.0)

    def test_the_next_game_restarts_the_clock_from_zero(self) -> None:
        self.coord._recording_manager.is_any_recording.return_value = False
        self.clock.now = 500 * SECOND
        self._toggle()
        self.coord._recording_manager.is_any_recording.return_value = True
        self.clock.now = 900 * SECOND
        self._toggle()
        self.coord._recording_manager.is_any_recording.return_value = False
        self.clock.now = 2000 * SECOND
        self._toggle()  # Begin Game again
        self.assertEqual(self.coord._game_started_ns, 2000 * SECOND)
        self.assertIsNone(self.coord._game_ended_ns)
        self.assertEqual(self.coord.game_elapsed_seconds(), 0.0)


if __name__ == "__main__":
    unittest.main()
