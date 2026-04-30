"""Phase 8.D — `plays.json` sidecar tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.models import Play
from app.storage.metadata_db import MetadataDb
from app.tools.plays_json_export import (
    PLAYS_SIDECAR_FILENAME,
    write_plays_sidecar,
    write_plays_sidecars_for_session,
)


def _new_db_with_session(tmp: str) -> MetadataDb:
    db = MetadataDb(Path(tmp) / "metadata.db")
    db.create_session(
        session_id="session_001",
        source_name="Test",
        started_at="2026-04-30T00:00:00+00:00",
    )
    return db


def _seed_play(
    db: MetadataDb,
    *,
    game_subdir: str,
    play_number: int,
    start_ns: int,
    end_ns: int | None,
    auto_closed_on_crash: bool = False,
) -> int:
    pid = db.insert_play(Play(
        session_id="session_001",
        game_subdir=game_subdir,
        play_number=play_number,
        start_session_time_ns=start_ns,
        created_at="2026-04-30T00:00:00+00:00",
    ))
    if end_ns is not None:
        db.close_play(pid, end_ns, auto_closed_on_crash=auto_closed_on_crash)
    return pid


class PlaysSidecarShapeTests(unittest.TestCase):
    """Lock in the JSON shape so downstream tooling (editor, scoring
    UI) can rely on a stable contract."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.session_path = Path(self._temp_dir.name) / "session_001"
        self.session_path.mkdir(parents=True, exist_ok=True)
        self.db = _new_db_with_session(self._temp_dir.name)

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_writes_well_formed_json_with_metadata_and_plays(self) -> None:
        # Three plays: 0..4.5s, 4.5..7.7s, 7.7..12.0s.
        _seed_play(self.db, game_subdir="game_001", play_number=1,
                   start_ns=10_000_000_000, end_ns=14_500_000_000)
        _seed_play(self.db, game_subdir="game_001", play_number=2,
                   start_ns=14_500_000_000, end_ns=17_700_000_000)
        _seed_play(self.db, game_subdir="game_001", play_number=3,
                   start_ns=17_700_000_000, end_ns=22_000_000_000)

        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, PLAYS_SIDECAR_FILENAME)

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["session_id"], "session_001")
        self.assertEqual(payload["game_subdir"], "game_001")
        self.assertEqual(payload["play_count"], 3)
        # Game duration: last play's end (22s) − first play's start (10s) = 12s.
        self.assertAlmostEqual(payload["game_duration_seconds"], 12.0, places=3)
        self.assertEqual(len(payload["plays"]), 3)
        # Game-relative seconds — first play starts at 0.0.
        self.assertEqual(payload["plays"][0]["play_number"], 1)
        self.assertEqual(payload["plays"][0]["start_seconds"], 0.0)
        self.assertEqual(payload["plays"][0]["length_seconds"], 4.5)
        self.assertEqual(payload["plays"][1]["start_seconds"], 4.5)
        self.assertAlmostEqual(payload["plays"][1]["length_seconds"], 3.2, places=3)
        self.assertAlmostEqual(payload["plays"][2]["start_seconds"], 7.7, places=3)
        self.assertAlmostEqual(payload["plays"][2]["length_seconds"], 4.3, places=3)

    def test_empty_plays_writes_plays_array_empty(self) -> None:
        # No plays in the DB — sidecar still written with empty array.
        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["plays"], [])
        self.assertEqual(payload["play_count"], 0)
        self.assertEqual(payload["game_duration_seconds"], 0.0)

    def test_open_play_excluded_with_warning(self) -> None:
        # One closed play, one open. JSON includes only the closed one.
        _seed_play(self.db, game_subdir="game_001", play_number=1,
                   start_ns=0, end_ns=4_000_000_000)
        _seed_play(self.db, game_subdir="game_001", play_number=2,
                   start_ns=4_000_000_000, end_ns=None)
        with self.assertLogs(
            "app.tools.plays_json_export", level="WARNING"
        ) as captured:
            path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["play_count"], 1)
        self.assertEqual(payload["plays"][0]["play_number"], 1)
        self.assertTrue(any("open play" in msg for msg in captured.output))

    def test_auto_closed_on_crash_plays_included_normally(self) -> None:
        # The JSON contract doesn't surface the crash flag — consumers
        # don't need it. Lock that in so future changes don't leak the
        # internal field.
        _seed_play(self.db, game_subdir="game_001", play_number=1,
                   start_ns=0, end_ns=4_000_000_000,
                   auto_closed_on_crash=True)
        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["play_count"], 1)
        self.assertNotIn(
            "auto_closed_on_crash",
            payload["plays"][0],
        )

    def test_sidecar_path_under_processed_game_subdir(self) -> None:
        _seed_play(self.db, game_subdir="game_002", play_number=1,
                   start_ns=0, end_ns=4_000_000_000)
        path = write_plays_sidecar(self.db, self.session_path, "game_002")
        expected = (
            self.session_path / "processed" / "game_002" / PLAYS_SIDECAR_FILENAME
        )
        self.assertEqual(path, expected)

    def test_idempotent_rewrite(self) -> None:
        # Writing twice produces the same output. Second call
        # overwrites without error.
        _seed_play(self.db, game_subdir="game_001", play_number=1,
                   start_ns=0, end_ns=4_000_000_000)
        first = write_plays_sidecar(self.db, self.session_path, "game_001")
        first_text = first.read_text(encoding="utf-8")
        second = write_plays_sidecar(self.db, self.session_path, "game_001")
        second_text = second.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(first_text, second_text)


class WriteForSessionTests(unittest.TestCase):
    """`write_plays_sidecars_for_session` iterates the supplied list."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.session_path = Path(self._temp_dir.name) / "session_001"
        self.session_path.mkdir(parents=True, exist_ok=True)
        self.db = _new_db_with_session(self._temp_dir.name)

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_writes_one_sidecar_per_game(self) -> None:
        _seed_play(self.db, game_subdir="game_001", play_number=1,
                   start_ns=0, end_ns=4_000_000_000)
        _seed_play(self.db, game_subdir="game_002", play_number=1,
                   start_ns=10_000_000_000, end_ns=14_000_000_000)
        _seed_play(self.db, game_subdir="game_002", play_number=2,
                   start_ns=14_000_000_000, end_ns=18_000_000_000)
        written = write_plays_sidecars_for_session(
            self.db, self.session_path, ["game_001", "game_002"]
        )
        self.assertEqual(len(written), 2)
        for p in written:
            self.assertTrue(p.exists())

        g1 = json.loads(
            (self.session_path / "processed" / "game_001"
             / PLAYS_SIDECAR_FILENAME).read_text(encoding="utf-8")
        )
        g2 = json.loads(
            (self.session_path / "processed" / "game_002"
             / PLAYS_SIDECAR_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(g1["play_count"], 1)
        self.assertEqual(g2["play_count"], 2)

    def test_deduplicates_repeated_game_subdirs(self) -> None:
        # Caller might pass the same game multiple times (one per
        # feed in the long-form plan). We write one sidecar per
        # unique game.
        _seed_play(self.db, game_subdir="game_001", play_number=1,
                   start_ns=0, end_ns=4_000_000_000)
        written = write_plays_sidecars_for_session(
            self.db, self.session_path, ["game_001", "game_001", "game_001"]
        )
        self.assertEqual(len(written), 1)


if __name__ == "__main__":
    unittest.main()
