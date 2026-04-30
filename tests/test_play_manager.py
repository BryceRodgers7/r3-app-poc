"""Phase 7.H.1: tests for the `plays` table CRUD and `PlayManager`.

Two surfaces locked in:

  - SQLite schema + CRUD on `MetadataDb` (`insert_play`, `close_play`,
    `plays_for_game`, `open_plays_for_session`).
  - `PlayManager` lifecycle: start_game opens the next play number
    (1 for a fresh game, max+1 for a resumed game), mark_next_play
    advances boundaries, stop_game closes the open play, and
    auto_close_open_plays_for_session handles crash recovery.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.models import Play
from app.core.play_manager import PlayManager
from app.storage.metadata_db import MetadataDb


def _new_db(tmp: str) -> MetadataDb:
    db = MetadataDb(Path(tmp) / "metadata.db")
    db.create_session(
        session_id="session_001",
        source_name="Test",
        started_at="2026-04-30T00:00:00+00:00",
    )
    return db


class PlaysSchemaTests(unittest.TestCase):
    def test_insert_and_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                pid = db.insert_play(
                    Play(
                        session_id="session_001",
                        game_subdir="game_001",
                        play_number=1,
                        start_session_time_ns=0,
                        created_at="2026-04-30T00:00:00+00:00",
                    )
                )
                self.assertGreater(pid, 0)
                rows = db.plays_for_game("session_001", "game_001")
                self.assertEqual(len(rows), 1)
                p = rows[0]
                self.assertEqual(p.play_number, 1)
                self.assertEqual(p.start_session_time_ns, 0)
                self.assertIsNone(p.end_session_time_ns)
                self.assertFalse(p.auto_closed_on_crash)
            finally:
                db.close()

    def test_unique_constraint_on_play_number_within_game(self) -> None:
        # `UNIQUE(session_id, game_subdir, play_number)` — two plays
        # with the same number in the same game must collide.
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_001",
                    play_number=1,
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                with self.assertRaises(Exception):
                    db.insert_play(Play(
                        session_id="session_001",
                        game_subdir="game_001",
                        play_number=1,
                        start_session_time_ns=4_000_000_000,
                        created_at="2026-04-30T00:00:00+00:00",
                    ))
            finally:
                db.close()

    def test_play_number_resets_per_game(self) -> None:
        # Same play_number across different games is fine — the
        # constraint is per (session_id, game_subdir).
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_001",
                    play_number=1,
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_002",
                    play_number=1,
                    start_session_time_ns=100_000_000_000,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                self.assertEqual(
                    len(db.plays_for_game("session_001", "game_001")), 1
                )
                self.assertEqual(
                    len(db.plays_for_game("session_001", "game_002")), 1
                )
            finally:
                db.close()

    def test_close_play_sets_end_and_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                pid = db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_001",
                    play_number=1,
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                db.close_play(pid, end_session_time_ns=4_500_000_000)
                rows = db.plays_for_game("session_001", "game_001")
                self.assertEqual(rows[0].end_session_time_ns, 4_500_000_000)
                self.assertFalse(rows[0].auto_closed_on_crash)

                pid2 = db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_001",
                    play_number=2,
                    start_session_time_ns=4_500_000_000,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                db.close_play(
                    pid2,
                    end_session_time_ns=9_000_000_000,
                    auto_closed_on_crash=True,
                )
                rows = db.plays_for_game("session_001", "game_001")
                self.assertEqual(len(rows), 2)
                self.assertTrue(rows[1].auto_closed_on_crash)
            finally:
                db.close()

    def test_open_plays_for_session_filters_to_null_end(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                pid_open = db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_001",
                    play_number=1,
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                pid_closed = db.insert_play(Play(
                    session_id="session_001",
                    game_subdir="game_001",
                    play_number=2,
                    start_session_time_ns=4_000_000_000,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                db.close_play(pid_closed, end_session_time_ns=8_000_000_000)
                open_plays = db.open_plays_for_session("session_001")
                self.assertEqual(len(open_plays), 1)
                self.assertEqual(open_plays[0].play_id, pid_open)
            finally:
                db.close()


class PlayManagerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.db = _new_db(self._temp_dir.name)
        self.pm = PlayManager(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_start_game_opens_play_one_for_fresh_game(self) -> None:
        play = self.pm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.assertEqual(play.play_number, 1)
        self.assertEqual(play.start_session_time_ns, 0)
        self.assertIsNone(play.end_session_time_ns)
        self.assertEqual(self.pm.current_play_number(), 1)

    def test_start_game_picks_max_plus_one_when_existing_plays_present(self) -> None:
        # Pre-existing plays from a prior crashed-and-resumed game
        # (already auto-closed). Resume + Start should open Play #4.
        for n in range(1, 4):
            pid = self.db.insert_play(Play(
                session_id="session_001",
                game_subdir="game_001",
                play_number=n,
                start_session_time_ns=(n - 1) * 4_000_000_000,
                created_at="2026-04-30T00:00:00+00:00",
            ))
            self.db.close_play(pid, end_session_time_ns=n * 4_000_000_000)
        play = self.pm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=20_000_000_000,
        )
        self.assertEqual(play.play_number, 4)

    def test_mark_next_play_closes_current_and_opens_next(self) -> None:
        self.pm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        next_play = self.pm.mark_next_play(now_session_time_ns=5_000_000_000)
        self.assertIsNotNone(next_play)
        assert next_play is not None
        self.assertEqual(next_play.play_number, 2)
        self.assertEqual(next_play.start_session_time_ns, 5_000_000_000)
        # DB has play 1 closed, play 2 open.
        rows = self.db.plays_for_game("session_001", "game_001")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].end_session_time_ns, 5_000_000_000)
        self.assertIsNone(rows[1].end_session_time_ns)

    def test_mark_next_play_no_op_when_no_open_play(self) -> None:
        result = self.pm.mark_next_play(now_session_time_ns=1_000_000_000)
        self.assertIsNone(result)

    def test_stop_game_closes_current_and_clears_pointer(self) -> None:
        self.pm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.pm.mark_next_play(now_session_time_ns=4_000_000_000)
        self.pm.stop_game(end_session_time_ns=10_000_000_000)
        self.assertIsNone(self.pm.current_play())
        rows = self.db.plays_for_game("session_001", "game_001")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].end_session_time_ns, 4_000_000_000)
        self.assertEqual(rows[1].end_session_time_ns, 10_000_000_000)
        self.assertFalse(rows[0].auto_closed_on_crash)
        self.assertFalse(rows[1].auto_closed_on_crash)

    def test_stop_game_no_op_when_nothing_open(self) -> None:
        # Defensive — Stop fired with no game in progress.
        self.pm.stop_game(end_session_time_ns=4_000_000_000)
        self.assertIsNone(self.pm.current_play())

    def test_per_game_play_counter_resets(self) -> None:
        # Start game 1, mark a couple of plays, stop. Start game 2 —
        # play counter starts at 1 again (different game_subdir).
        self.pm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.pm.mark_next_play(now_session_time_ns=4_000_000_000)
        self.pm.mark_next_play(now_session_time_ns=8_000_000_000)
        self.pm.stop_game(end_session_time_ns=10_000_000_000)

        play = self.pm.start_game(
            session_id="session_001",
            game_subdir="game_002",
            start_session_time_ns=20_000_000_000,
        )
        self.assertEqual(play.play_number, 1)


class AutoCloseOpenPlaysTests(unittest.TestCase):
    """`auto_close_open_plays_for_session` is the §11.4 recovery hook
    that closes any play whose `end_session_time_ns` is NULL using a
    fallback (the latest finalized segment's end). All such closures
    flag `auto_closed_on_crash = True`.
    """

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.db = _new_db(self._temp_dir.name)
        self.pm = PlayManager(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_closes_only_plays_with_null_end(self) -> None:
        # Game 1: Play 1 closed, Play 2 still open. Game 2: Play 1 open.
        pid1 = self.db.insert_play(Play(
            session_id="session_001",
            game_subdir="game_001",
            play_number=1,
            start_session_time_ns=0,
            created_at="2026-04-30T00:00:00+00:00",
        ))
        self.db.close_play(pid1, end_session_time_ns=4_000_000_000)
        self.db.insert_play(Play(
            session_id="session_001",
            game_subdir="game_001",
            play_number=2,
            start_session_time_ns=4_000_000_000,
            created_at="2026-04-30T00:00:00+00:00",
        ))
        self.db.insert_play(Play(
            session_id="session_001",
            game_subdir="game_002",
            play_number=1,
            start_session_time_ns=20_000_000_000,
            created_at="2026-04-30T00:00:00+00:00",
        ))
        closed = self.pm.auto_close_open_plays_for_session(
            session_id="session_001",
            fallback_end_session_time_ns=30_000_000_000,
        )
        self.assertEqual(closed, 2)
        # Pre-closed Play 1 stays as it was (not auto_closed_on_crash).
        rows = self.db.plays_for_game("session_001", "game_001")
        self.assertEqual(rows[0].end_session_time_ns, 4_000_000_000)
        self.assertFalse(rows[0].auto_closed_on_crash)
        # The two recovered plays both got the fallback end + flag.
        self.assertEqual(rows[1].end_session_time_ns, 30_000_000_000)
        self.assertTrue(rows[1].auto_closed_on_crash)
        rows_g2 = self.db.plays_for_game("session_001", "game_002")
        self.assertEqual(rows_g2[0].end_session_time_ns, 30_000_000_000)
        self.assertTrue(rows_g2[0].auto_closed_on_crash)

    def test_idempotent(self) -> None:
        # Calling auto-close on a session with no open plays is a no-op.
        closed = self.pm.auto_close_open_plays_for_session(
            session_id="session_001",
            fallback_end_session_time_ns=10_000_000_000,
        )
        self.assertEqual(closed, 0)


if __name__ == "__main__":
    unittest.main()
