"""Phase 14.A: tests for the `clips` table CRUD and `ClipManager`.

Two surfaces locked in:

  - SQLite schema + CRUD on `MetadataDb` (`insert_clip`, `close_clip`,
    `set_clip_marked`, `clips_for_game`, `open_clips_for_session`).
  - `ClipManager` lifecycle: start_game opens a pre-game clip for a
    fresh game (or continues the most-recent clip type on resume);
    mark_next_play / mark_timeout / mark_challenge advance boundaries
    with the right gating; toggle_clip_mark flips the marked flag;
    stop_game closes the open clip; auto_close_open_clips_for_session
    handles crash recovery.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.clip_manager import ClipManager
from app.core.models import (
    CLIP_TYPE_CHALLENGE,
    CLIP_TYPE_PLAY,
    CLIP_TYPE_PRE_GAME,
    CLIP_TYPE_TIMEOUT,
    Clip,
)
from app.storage.metadata_db import MetadataDb


def _new_db(tmp: str) -> MetadataDb:
    db = MetadataDb(Path(tmp) / "metadata.db")
    db.create_session(
        session_id="session_001",
        source_name="Test",
        started_at="2026-04-30T00:00:00+00:00",
    )
    return db


def _pre_game_clip(
    *,
    game_subdir: str = "game_001",
    clip_number: int = 0,
    start_ns: int = 0,
) -> Clip:
    return Clip(
        session_id="session_001",
        game_subdir=game_subdir,
        clip_number=clip_number,
        type=CLIP_TYPE_PRE_GAME,
        play_number=None,
        start_session_time_ns=start_ns,
        created_at="2026-04-30T00:00:00+00:00",
    )


class ClipsSchemaTests(unittest.TestCase):
    def test_insert_and_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                cid = db.insert_clip(_pre_game_clip())
                self.assertGreater(cid, 0)
                rows = db.clips_for_game("session_001", "game_001")
                self.assertEqual(len(rows), 1)
                c = rows[0]
                self.assertEqual(c.clip_number, 0)
                self.assertEqual(c.type, CLIP_TYPE_PRE_GAME)
                self.assertIsNone(c.play_number)
                self.assertFalse(c.marked)
                self.assertEqual(c.start_session_time_ns, 0)
                self.assertIsNone(c.end_session_time_ns)
                self.assertFalse(c.auto_closed_on_crash)
            finally:
                db.close()

    def test_unique_constraint_on_clip_number_within_game(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                db.insert_clip(_pre_game_clip())
                with self.assertRaises(Exception):
                    db.insert_clip(_pre_game_clip(start_ns=4_000_000_000))
            finally:
                db.close()

    def test_clip_number_resets_per_game(self) -> None:
        # Same clip_number across different games is fine — the
        # constraint is per (session_id, game_subdir).
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                db.insert_clip(_pre_game_clip(game_subdir="game_001"))
                db.insert_clip(_pre_game_clip(
                    game_subdir="game_002", start_ns=100_000_000_000,
                ))
                self.assertEqual(
                    len(db.clips_for_game("session_001", "game_001")), 1
                )
                self.assertEqual(
                    len(db.clips_for_game("session_001", "game_002")), 1
                )
            finally:
                db.close()

    def test_type_check_constraint_rejects_unknown_type(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                bad = Clip(
                    session_id="session_001",
                    game_subdir="game_001",
                    clip_number=0,
                    type="bogus",
                    play_number=None,
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                )
                with self.assertRaises(Exception):
                    db.insert_clip(bad)
            finally:
                db.close()

    def test_play_number_must_be_set_for_play_type(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                bad = Clip(
                    session_id="session_001",
                    game_subdir="game_001",
                    clip_number=0,
                    type=CLIP_TYPE_PLAY,
                    play_number=None,  # violates the CHECK constraint
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                )
                with self.assertRaises(Exception):
                    db.insert_clip(bad)
            finally:
                db.close()

    def test_play_number_must_be_null_for_non_play_type(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                bad = Clip(
                    session_id="session_001",
                    game_subdir="game_001",
                    clip_number=0,
                    type=CLIP_TYPE_TIMEOUT,
                    play_number=5,  # violates the CHECK constraint
                    start_session_time_ns=0,
                    created_at="2026-04-30T00:00:00+00:00",
                )
                with self.assertRaises(Exception):
                    db.insert_clip(bad)
            finally:
                db.close()

    def test_close_clip_sets_end_and_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                cid = db.insert_clip(_pre_game_clip())
                db.close_clip(cid, end_session_time_ns=400_000_000)
                rows = db.clips_for_game("session_001", "game_001")
                self.assertEqual(rows[0].end_session_time_ns, 400_000_000)
                self.assertFalse(rows[0].auto_closed_on_crash)

                cid2 = db.insert_clip(Clip(
                    session_id="session_001",
                    game_subdir="game_001",
                    clip_number=1,
                    type=CLIP_TYPE_PLAY,
                    play_number=1,
                    start_session_time_ns=400_000_000,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                db.close_clip(
                    cid2,
                    end_session_time_ns=4_500_000_000,
                    auto_closed_on_crash=True,
                )
                rows = db.clips_for_game("session_001", "game_001")
                self.assertEqual(len(rows), 2)
                self.assertTrue(rows[1].auto_closed_on_crash)
            finally:
                db.close()

    def test_set_clip_marked_toggles_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                cid = db.insert_clip(_pre_game_clip())
                db.set_clip_marked(cid, True)
                rows = db.clips_for_game("session_001", "game_001")
                self.assertTrue(rows[0].marked)
                db.set_clip_marked(cid, False)
                rows = db.clips_for_game("session_001", "game_001")
                self.assertFalse(rows[0].marked)
            finally:
                db.close()

    def test_open_clips_for_session_filters_to_null_end(self) -> None:
        with TemporaryDirectory() as tmp:
            db = _new_db(tmp)
            try:
                cid_open = db.insert_clip(_pre_game_clip())
                cid_closed = db.insert_clip(Clip(
                    session_id="session_001",
                    game_subdir="game_001",
                    clip_number=1,
                    type=CLIP_TYPE_PLAY,
                    play_number=1,
                    start_session_time_ns=400_000_000,
                    created_at="2026-04-30T00:00:00+00:00",
                ))
                db.close_clip(cid_closed, end_session_time_ns=8_000_000_000)
                open_clips = db.open_clips_for_session("session_001")
                self.assertEqual(len(open_clips), 1)
                self.assertEqual(open_clips[0].clip_id, cid_open)
            finally:
                db.close()


class LegacyPlaysTableMigrationTests(unittest.TestCase):
    """Pre-Phase-14 DBs have a `plays` table; the schema bootstrap
    drops it (operator-confirmed: data is throwaway) and creates
    `clips` fresh."""

    def test_legacy_plays_table_dropped_on_connect(self) -> None:
        import sqlite3

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.db"
            # Pre-create a legacy plays table directly.
            legacy = sqlite3.connect(path)
            try:
                legacy.execute(
                    """
                    CREATE TABLE plays (
                        play_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        game_subdir TEXT NOT NULL,
                        play_number INTEGER NOT NULL,
                        start_session_time_ns INTEGER NOT NULL,
                        end_session_time_ns INTEGER,
                        created_at TEXT NOT NULL,
                        auto_closed_on_crash INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                legacy.execute(
                    "INSERT INTO plays (session_id, game_subdir, play_number, "
                    "start_session_time_ns, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("session_001", "game_001", 1, 0, "2026-01-01T00:00:00+00:00"),
                )
                legacy.commit()
            finally:
                legacy.close()

            db = MetadataDb(path)
            try:
                db.connect()
                # `plays` is gone, `clips` exists and is empty.
                inspector = sqlite3.connect(path)
                try:
                    tables = {
                        row[0]
                        for row in inspector.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    self.assertNotIn("plays", tables)
                    self.assertIn("clips", tables)
                finally:
                    inspector.close()
                self.assertEqual(
                    db.clips_for_game("session_001", "game_001"), []
                )
            finally:
                db.close()


class ClipManagerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.db = _new_db(self._temp_dir.name)
        self.cm = ClipManager(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_start_game_opens_pre_game_clip_for_fresh_game(self) -> None:
        clip = self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.assertEqual(clip.clip_number, 0)
        self.assertEqual(clip.type, CLIP_TYPE_PRE_GAME)
        self.assertIsNone(clip.play_number)
        self.assertEqual(clip.start_session_time_ns, 0)
        self.assertIsNone(clip.end_session_time_ns)
        # Pre-game means no play has started yet.
        self.assertIsNone(self.cm.current_play_number())
        self.assertEqual(self.cm.current_clip_number(), 0)

    def test_mark_next_play_from_pre_game_opens_play_one(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        next_clip = self.cm.mark_next_play(now_session_time_ns=400_000_000)
        self.assertIsNotNone(next_clip)
        assert next_clip is not None
        self.assertEqual(next_clip.clip_number, 1)
        self.assertEqual(next_clip.type, CLIP_TYPE_PLAY)
        self.assertEqual(next_clip.play_number, 1)
        self.assertEqual(self.cm.current_play_number(), 1)
        # The pre-game clip is closed; the new play is open.
        rows = self.db.clips_for_game("session_001", "game_001")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].end_session_time_ns, 400_000_000)
        self.assertIsNone(rows[1].end_session_time_ns)

    def test_mark_next_play_increments_play_number(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        c2 = self.cm.mark_next_play(now_session_time_ns=4_400_000_000)
        assert c2 is not None
        self.assertEqual(c2.play_number, 2)
        self.assertEqual(c2.clip_number, 2)
        c3 = self.cm.mark_next_play(now_session_time_ns=8_400_000_000)
        assert c3 is not None
        self.assertEqual(c3.play_number, 3)
        self.assertEqual(c3.clip_number, 3)

    def test_mark_timeout_rejected_during_pre_game(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        result = self.cm.mark_timeout(now_session_time_ns=400_000_000)
        self.assertIsNone(result)
        # No transition happened; pre-game is still open.
        rows = self.db.clips_for_game("session_001", "game_001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].type, CLIP_TYPE_PRE_GAME)
        self.assertIsNone(rows[0].end_session_time_ns)

    def test_mark_timeout_after_first_play_succeeds(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        t = self.cm.mark_timeout(now_session_time_ns=4_400_000_000)
        assert t is not None
        self.assertEqual(t.type, CLIP_TYPE_TIMEOUT)
        self.assertIsNone(t.play_number)
        # Play counter still reads the last play number across the timeout.
        self.assertEqual(self.cm.current_play_number(), 1)

    def test_mark_next_play_after_timeout_advances_play_number(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        self.cm.mark_timeout(now_session_time_ns=4_400_000_000)
        p2 = self.cm.mark_next_play(now_session_time_ns=4_900_000_000)
        assert p2 is not None
        self.assertEqual(p2.play_number, 2)
        # clip_number is monotonic regardless of type.
        self.assertEqual(p2.clip_number, 3)

    def test_mark_challenge_rejected_during_pre_game(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.assertIsNone(self.cm.mark_challenge(now_session_time_ns=400_000_000))

    def test_mark_challenge_rejected_back_to_back(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        first = self.cm.mark_challenge(now_session_time_ns=4_400_000_000)
        self.assertIsNotNone(first)
        # Second challenge press should be rejected.
        second = self.cm.mark_challenge(now_session_time_ns=4_900_000_000)
        self.assertIsNone(second)
        # The first challenge is still open; nothing was closed.
        rows = self.db.clips_for_game("session_001", "game_001")
        self.assertEqual(rows[-1].type, CLIP_TYPE_CHALLENGE)
        self.assertIsNone(rows[-1].end_session_time_ns)

    def test_mark_next_play_after_challenge_closes_challenge_and_opens_play(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        self.cm.mark_challenge(now_session_time_ns=4_400_000_000)
        p2 = self.cm.mark_next_play(now_session_time_ns=5_400_000_000)
        assert p2 is not None
        self.assertEqual(p2.type, CLIP_TYPE_PLAY)
        self.assertEqual(p2.play_number, 2)

    def test_toggle_clip_mark_flips_flag(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        marked = self.cm.toggle_clip_mark()
        assert marked is not None
        self.assertTrue(marked.marked)
        unmarked = self.cm.toggle_clip_mark()
        assert unmarked is not None
        self.assertFalse(unmarked.marked)
        # Persisted to DB.
        rows = self.db.clips_for_game("session_001", "game_001")
        self.assertFalse(rows[0].marked)

    def test_toggle_clip_mark_noop_when_no_clip_open(self) -> None:
        self.assertIsNone(self.cm.toggle_clip_mark())

    def test_mark_next_play_no_op_when_no_open_clip(self) -> None:
        result = self.cm.mark_next_play(now_session_time_ns=1_000_000_000)
        self.assertIsNone(result)

    def test_stop_game_closes_current_and_clears_pointer(self) -> None:
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        self.cm.stop_game(end_session_time_ns=10_000_000_000)
        self.assertIsNone(self.cm.current_clip())
        self.assertIsNone(self.cm.current_play_number())
        rows = self.db.clips_for_game("session_001", "game_001")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].end_session_time_ns, 400_000_000)
        self.assertEqual(rows[1].end_session_time_ns, 10_000_000_000)
        self.assertFalse(rows[0].auto_closed_on_crash)
        self.assertFalse(rows[1].auto_closed_on_crash)

    def test_stop_game_no_op_when_nothing_open(self) -> None:
        self.cm.stop_game(end_session_time_ns=4_000_000_000)
        self.assertIsNone(self.cm.current_clip())

    def test_per_game_counters_reset(self) -> None:
        # Game 1 → pre-game + play 1 + play 2 → stop.
        # Game 2 → starts with a fresh pre-game; play counter resets.
        self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=0,
        )
        self.cm.mark_next_play(now_session_time_ns=400_000_000)
        self.cm.mark_next_play(now_session_time_ns=4_400_000_000)
        self.cm.stop_game(end_session_time_ns=10_000_000_000)

        c = self.cm.start_game(
            session_id="session_001",
            game_subdir="game_002",
            start_session_time_ns=20_000_000_000,
        )
        self.assertEqual(c.clip_number, 0)
        self.assertEqual(c.type, CLIP_TYPE_PRE_GAME)
        self.assertIsNone(self.cm.current_play_number())
        p1 = self.cm.mark_next_play(now_session_time_ns=20_400_000_000)
        assert p1 is not None
        self.assertEqual(p1.play_number, 1)

    def test_start_game_resume_continues_with_same_type(self) -> None:
        # Simulate a crashed game: pre-game closed, play 1 closed,
        # play 2 was open at crash time and got auto-closed.
        for clip in [
            Clip(
                session_id="session_001",
                game_subdir="game_001",
                clip_number=0,
                type=CLIP_TYPE_PRE_GAME,
                play_number=None,
                start_session_time_ns=0,
                created_at="2026-04-30T00:00:00+00:00",
            ),
            Clip(
                session_id="session_001",
                game_subdir="game_001",
                clip_number=1,
                type=CLIP_TYPE_PLAY,
                play_number=1,
                start_session_time_ns=400_000_000,
                created_at="2026-04-30T00:00:00+00:00",
            ),
            Clip(
                session_id="session_001",
                game_subdir="game_001",
                clip_number=2,
                type=CLIP_TYPE_PLAY,
                play_number=2,
                start_session_time_ns=4_400_000_000,
                created_at="2026-04-30T00:00:00+00:00",
            ),
        ]:
            cid = self.db.insert_clip(clip)
            # All closed at fixed times.
            end_ns = clip.start_session_time_ns + 4_000_000_000
            self.db.close_clip(
                cid,
                end_session_time_ns=end_ns,
                auto_closed_on_crash=(clip.clip_number == 2),
            )
        # Resume continuation: new clip continues the last type (play),
        # with the next clip_number and next play_number.
        clip = self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=20_000_000_000,
        )
        self.assertEqual(clip.clip_number, 3)
        self.assertEqual(clip.type, CLIP_TYPE_PLAY)
        self.assertEqual(clip.play_number, 3)
        self.assertEqual(self.cm.current_play_number(), 3)

    def test_start_game_resume_after_timeout_continues_with_timeout(self) -> None:
        # Pre-game + play 1 + timeout (auto-closed on crash). Resume
        # picks up another timeout. play_number stays None on the new
        # clip but current_play_number() carries the cached value.
        for clip, end in [
            (Clip(
                session_id="session_001", game_subdir="game_001",
                clip_number=0, type=CLIP_TYPE_PRE_GAME, play_number=None,
                start_session_time_ns=0,
                created_at="2026-04-30T00:00:00+00:00",
            ), 400_000_000),
            (Clip(
                session_id="session_001", game_subdir="game_001",
                clip_number=1, type=CLIP_TYPE_PLAY, play_number=1,
                start_session_time_ns=400_000_000,
                created_at="2026-04-30T00:00:00+00:00",
            ), 4_400_000_000),
            (Clip(
                session_id="session_001", game_subdir="game_001",
                clip_number=2, type=CLIP_TYPE_TIMEOUT, play_number=None,
                start_session_time_ns=4_400_000_000,
                created_at="2026-04-30T00:00:00+00:00",
            ), 4_900_000_000),
        ]:
            cid = self.db.insert_clip(clip)
            self.db.close_clip(cid, end_session_time_ns=end)
        clip = self.cm.start_game(
            session_id="session_001",
            game_subdir="game_001",
            start_session_time_ns=20_000_000_000,
        )
        self.assertEqual(clip.type, CLIP_TYPE_TIMEOUT)
        self.assertIsNone(clip.play_number)
        # The cached last play number is restored from the DB.
        self.assertEqual(self.cm.current_play_number(), 1)


class AutoCloseOpenClipsTests(unittest.TestCase):
    """`auto_close_open_clips_for_session` is the §11.4 recovery hook
    that closes any clip whose `end_session_time_ns` is NULL using a
    fallback (the latest finalized segment's end). All such closures
    flag `auto_closed_on_crash = True`."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.db = _new_db(self._temp_dir.name)
        self.cm = ClipManager(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_closes_only_clips_with_null_end(self) -> None:
        # Game 1: pre-game closed, play 1 still open. Game 2: pre-game open.
        cid1 = self.db.insert_clip(_pre_game_clip(game_subdir="game_001"))
        self.db.close_clip(cid1, end_session_time_ns=400_000_000)
        self.db.insert_clip(Clip(
            session_id="session_001",
            game_subdir="game_001",
            clip_number=1,
            type=CLIP_TYPE_PLAY,
            play_number=1,
            start_session_time_ns=400_000_000,
            created_at="2026-04-30T00:00:00+00:00",
        ))
        self.db.insert_clip(_pre_game_clip(
            game_subdir="game_002", start_ns=20_000_000_000,
        ))
        closed = self.cm.auto_close_open_clips_for_session(
            session_id="session_001",
            fallback_end_session_time_ns=30_000_000_000,
        )
        self.assertEqual(closed, 2)
        # Pre-existing pre-game stays as it was.
        rows = self.db.clips_for_game("session_001", "game_001")
        self.assertEqual(rows[0].end_session_time_ns, 400_000_000)
        self.assertFalse(rows[0].auto_closed_on_crash)
        # The two recovered clips both got the fallback end + flag.
        self.assertEqual(rows[1].end_session_time_ns, 30_000_000_000)
        self.assertTrue(rows[1].auto_closed_on_crash)
        rows_g2 = self.db.clips_for_game("session_001", "game_002")
        self.assertEqual(rows_g2[0].end_session_time_ns, 30_000_000_000)
        self.assertTrue(rows_g2[0].auto_closed_on_crash)

    def test_idempotent(self) -> None:
        closed = self.cm.auto_close_open_clips_for_session(
            session_id="session_001",
            fallback_end_session_time_ns=10_000_000_000,
        )
        self.assertEqual(closed, 0)


if __name__ == "__main__":
    unittest.main()
