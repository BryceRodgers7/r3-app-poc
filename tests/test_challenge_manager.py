"""Tests for the challenge timer, validation and file store."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config.challenge_options import DEFAULT_CATEGORIES, DEFAULT_OUTCOMES
from app.core.challenge_manager import (
    CHALLENGES_FILENAME,
    ChallengeError,
    ChallengeManager,
    ChallengeStore,
    challenges_path,
    format_duration,
)

SECOND = 1_000_000_000


def _manager() -> ChallengeManager:
    return ChallengeManager(DEFAULT_CATEGORIES, DEFAULT_OUTCOMES)


def _start(manager: ChallengeManager, at: int = 100 * SECOND, **overrides):
    values = dict(
        session_id="session_001",
        game_subdir="game_001",
        clip_number=7,
        play_number=4,
        now_session_time_ns=at,
    )
    values.update(overrides)
    return manager.start(**values)


def _record(manager: ChallengeManager, now: int, **overrides):
    values = dict(
        challenge_number=1,
        team_side="A",
        team_name="Lakeside Lions",
        category="Player/Ball Contact",
        outcome="Reversed",
        now_session_time_ns=now,
    )
    values.update(overrides)
    return manager.build_record(**values)


class TimerTests(unittest.TestCase):
    def test_no_timer_until_a_challenge_starts(self) -> None:
        manager = _manager()
        self.assertFalse(manager.is_active)
        self.assertIsNone(manager.elapsed_seconds(50 * SECOND))

    def test_the_timer_starts_when_the_challenge_starts(self) -> None:
        manager = _manager()
        _start(manager, at=100 * SECOND)
        self.assertTrue(manager.is_active)
        self.assertEqual(manager.elapsed_seconds(100 * SECOND), 0.0)
        self.assertEqual(manager.elapsed_seconds(142 * SECOND + SECOND // 2), 42.5)

    def test_done_stops_the_timer_and_stores_the_elapsed_time(self) -> None:
        manager = _manager()
        _start(manager, at=100 * SECOND)
        record = _record(manager, now=193 * SECOND + 250_000_000)
        self.assertEqual(record.elapsed_seconds, 93.25)
        self.assertEqual(record.started_session_time_ns, 100 * SECOND)
        self.assertEqual(record.ended_session_time_ns, 193 * SECOND + 250_000_000)
        # The timer is frozen from here on, however late it is asked.
        self.assertEqual(manager.elapsed_seconds(900 * SECOND), 93.25)

    def test_a_retry_after_a_failed_save_keeps_the_first_stop_time(self) -> None:
        manager = _manager()
        _start(manager, at=100 * SECOND)
        first = _record(manager, now=160 * SECOND)
        second = _record(manager, now=400 * SECOND)  # operator pressed Done again later
        self.assertEqual(first.elapsed_seconds, 60.0)
        self.assertEqual(second.elapsed_seconds, 60.0)

    def test_a_failed_validation_does_not_stop_the_timer(self) -> None:
        manager = _manager()
        _start(manager, at=100 * SECOND)
        with self.assertRaises(ChallengeError):
            _record(manager, now=130 * SECOND, category="Not a category")
        self.assertEqual(manager.elapsed_seconds(200 * SECOND), 100.0)

    def test_the_timer_never_goes_negative(self) -> None:
        manager = _manager()
        _start(manager, at=100 * SECOND)
        self.assertEqual(manager.elapsed_seconds(90 * SECOND), 0.0)

    def test_the_stored_elapsed_time_is_rounded_to_milliseconds(self) -> None:
        manager = _manager()
        _start(manager, at=0)
        record = _record(manager, now=1_234_567_891)
        self.assertEqual(record.elapsed_seconds, 1.235)


class LifecycleTests(unittest.TestCase):
    def test_only_one_challenge_can_be_open(self) -> None:
        manager = _manager()
        _start(manager)
        with self.assertRaises(ChallengeError):
            _start(manager)

    def test_the_record_carries_what_was_challenged(self) -> None:
        manager = _manager()
        _start(manager, clip_number=7, play_number=4)
        record = _record(manager, now=130 * SECOND, challenge_number=2)
        self.assertEqual(record.challenge_number, 2)
        self.assertEqual(record.session_id, "session_001")
        self.assertEqual(record.game_subdir, "game_001")
        self.assertEqual(record.play_number, 4)
        self.assertEqual(record.clip_number, 7)
        self.assertEqual(record.team_side, "A")
        self.assertEqual(record.team, "Lakeside Lions")
        self.assertEqual(record.category, "Player/Ball Contact")
        self.assertEqual(record.outcome, "Reversed")
        self.assertTrue(record.started_at)
        self.assertTrue(record.completed_at)

    def test_building_a_record_leaves_the_challenge_open_until_finish(self) -> None:
        manager = _manager()
        _start(manager)
        _record(manager, now=130 * SECOND)
        self.assertTrue(manager.is_active)
        manager.finish()
        self.assertFalse(manager.is_active)
        _start(manager)  # a new challenge can open now

    def test_discard_drops_the_challenge_and_reports_what_was_open(self) -> None:
        manager = _manager()
        _start(manager, clip_number=9)
        dropped = manager.discard()
        self.assertEqual(dropped.clip_number, 9)
        self.assertFalse(manager.is_active)

    def test_discard_with_nothing_open_is_a_no_op(self) -> None:
        self.assertIsNone(_manager().discard())

    def test_a_record_needs_an_open_challenge(self) -> None:
        with self.assertRaises(ChallengeError):
            _record(_manager(), now=1)


class ValidationTests(unittest.TestCase):
    """Category, outcome and team may not be empty (all are NOT NULL)."""

    def setUp(self) -> None:
        self.manager = _manager()
        _start(self.manager)

    def assertRejected(self, **overrides) -> None:
        with self.assertRaises(ChallengeError):
            _record(self.manager, now=130 * SECOND, **overrides)

    def test_the_team_must_be_a_or_b(self) -> None:
        self.assertRejected(team_side="")
        self.assertRejected(team_side="C")
        _record(self.manager, now=130 * SECOND, team_side="B")

    def test_the_team_needs_a_name(self) -> None:
        self.assertRejected(team_name="   ")

    def test_the_category_is_required_and_must_be_configured(self) -> None:
        self.assertRejected(category="")
        self.assertRejected(category="   ")
        self.assertRejected(category="Something else")

    def test_the_outcome_is_required_and_must_be_configured(self) -> None:
        self.assertRejected(outcome="")
        self.assertRejected(outcome="Maybe")

    def test_every_configured_value_is_accepted(self) -> None:
        for category in DEFAULT_CATEGORIES:
            for outcome in DEFAULT_OUTCOMES:
                with self.subTest(category=category, outcome=outcome):
                    record = _record(
                        self.manager, now=130 * SECOND, category=category, outcome=outcome
                    )
                    self.assertEqual((record.category, record.outcome), (category, outcome))

    def test_a_hand_edited_value_over_its_limit_can_never_be_written(self) -> None:
        manager = ChallengeManager(["c" * 41], ["o" * 16])
        _start(manager)
        with self.assertRaises(ChallengeError):
            _record(manager, now=130 * SECOND, category="c" * 41, outcome="o" * 16)


class FormatDurationTests(unittest.TestCase):
    def test_minutes_and_seconds(self) -> None:
        for seconds, text in ((0, "0:00"), (7, "0:07"), (59.9, "0:59"), (60, "1:00"), (754, "12:34")):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), text)

    def test_an_hour_or_more_adds_hours(self) -> None:
        self.assertEqual(format_duration(3600), "1:00:00")
        self.assertEqual(format_duration(3725), "1:02:05")

    def test_negative_reads_as_zero(self) -> None:
        self.assertEqual(format_duration(-5), "0:00")


class _StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.path = challenges_path(self.root)
        self.store = ChallengeStore(self.path, "session_001")

    def make_record(self, number: int = 1, game: str = "game_001", **overrides):
        manager = _manager()
        _start(manager, game_subdir=game)
        return _record(manager, now=160 * SECOND, challenge_number=number, **overrides)


class StoreTests(_StoreTestCase):
    def test_the_file_lives_in_the_session_folder_and_is_json(self) -> None:
        self.assertEqual(self.path, self.root / CHALLENGES_FILENAME)
        self.assertEqual(self.path.suffix, ".json")

    def test_append_writes_the_details_as_json(self) -> None:
        self.store.append(self.make_record())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["session_id"], "session_001")
        (challenge,) = data["challenges"]
        self.assertEqual(challenge["challenge_number"], 1)
        self.assertEqual(challenge["game_subdir"], "game_001")
        self.assertEqual(challenge["play_number"], 4)
        self.assertEqual(challenge["team_side"], "A")
        self.assertEqual(challenge["team"], "Lakeside Lions")
        self.assertEqual(challenge["category"], "Player/Ball Contact")
        self.assertEqual(challenge["outcome"], "Reversed")
        self.assertEqual(challenge["elapsed_seconds"], 60.0)

    def test_records_round_trip(self) -> None:
        record = self.make_record()
        self.store.append(record)
        self.assertEqual(self.store.load(), [record])

    def test_each_append_keeps_the_earlier_records(self) -> None:
        for number in (1, 2, 3):
            self.store.append(self.make_record(number))
        self.assertEqual([r.challenge_number for r in self.store.load()], [1, 2, 3])

    def test_a_fresh_store_object_still_sees_earlier_records(self) -> None:
        # The file is the only memory: a Resume restart builds a new store.
        self.store.append(self.make_record(1))
        again = ChallengeStore(self.path, "session_001")
        again.append(self.make_record(2))
        self.assertEqual([r.challenge_number for r in again.load()], [1, 2])

    def test_numbering_restarts_for_each_game(self) -> None:
        self.assertEqual(self.store.next_number("game_001"), 1)
        self.store.append(self.make_record(1, "game_001"))
        self.store.append(self.make_record(2, "game_001"))
        self.assertEqual(self.store.next_number("game_001"), 3)
        self.assertEqual(self.store.next_number("game_002"), 1)

    def test_a_missing_file_is_an_empty_list(self) -> None:
        self.assertEqual(self.store.load(), [])

    def test_text_with_special_characters_round_trips(self) -> None:
        record = self.make_record(
            category="Net / Antenna Faults & Interference", team_name='Smith & "Sons" Zürich 北京'
        )
        self.store.append(record)
        loaded = self.store.load()[0]
        self.assertEqual(loaded.category, "Net / Antenna Faults & Interference")
        self.assertEqual(loaded.team, 'Smith & "Sons" Zürich 北京')

    def test_a_damaged_file_is_set_aside_not_overwritten(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")
        with self.assertLogs("app.core.challenge_manager", level="ERROR"):
            records = self.store.load()
        self.assertEqual(records, [])
        self.assertTrue(self.path.with_name(CHALLENGES_FILENAME + ".corrupt").exists())
        self.store.append(self.make_record(1))
        self.assertEqual(len(self.store.load()), 1)

    def test_no_temp_file_is_left_behind(self) -> None:
        self.store.append(self.make_record())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [CHALLENGES_FILENAME])

    def test_append_creates_a_missing_session_folder(self) -> None:
        store = ChallengeStore(self.root / "not" / "yet" / CHALLENGES_FILENAME, "s")
        store.append(self.make_record())
        self.assertTrue(store.path.exists())

    def test_append_raises_oserror_when_the_file_cannot_be_written(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a folder", encoding="utf-8")
        store = ChallengeStore(blocker / CHALLENGES_FILENAME, "s")
        with self.assertRaises(OSError):
            store.append(self.make_record())

    def test_nothing_here_uses_the_database(self) -> None:
        import app.core.challenge_manager as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite", source.lower())
        self.assertNotIn("MetadataDb", source)


if __name__ == "__main__":
    unittest.main()
