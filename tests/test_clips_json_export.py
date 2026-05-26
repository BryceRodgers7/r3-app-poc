"""`plays.json` sidecar tests (Phase 8.D, 14.A clips schema, 14.E rename)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.models import (
    CLIP_TYPE_CHALLENGE,
    CLIP_TYPE_PLAY,
    CLIP_TYPE_PRE_GAME,
    CLIP_TYPE_TIMEOUT,
    Clip,
)
from app.storage.metadata_db import MetadataDb
from app.tools.clips_json_export import (
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


def _seed_clip(
    db: MetadataDb,
    *,
    game_subdir: str,
    clip_number: int,
    clip_type: str,
    start_ns: int,
    end_ns: int | None,
    play_number: int | None = None,
    marked: bool = False,
    auto_closed_on_crash: bool = False,
) -> int:
    cid = db.insert_clip(Clip(
        session_id="session_001",
        game_subdir=game_subdir,
        clip_number=clip_number,
        type=clip_type,
        play_number=play_number,
        marked=marked,
        start_session_time_ns=start_ns,
        created_at="2026-04-30T00:00:00+00:00",
    ))
    if end_ns is not None:
        db.close_clip(cid, end_ns, auto_closed_on_crash=auto_closed_on_crash)
    return cid


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

    def test_writes_well_formed_json_with_metadata_and_clips(self) -> None:
        # pre-game 0..0.4s, play 1 0.4..4.9s, play 2 4.9..8.1s, play 3 8.1..12.4s
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=10_000_000_000, end_ns=10_400_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=1,
                   clip_type=CLIP_TYPE_PLAY, play_number=1,
                   start_ns=10_400_000_000, end_ns=14_900_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=2,
                   clip_type=CLIP_TYPE_PLAY, play_number=2,
                   start_ns=14_900_000_000, end_ns=18_100_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=3,
                   clip_type=CLIP_TYPE_PLAY, play_number=3,
                   start_ns=18_100_000_000, end_ns=22_400_000_000)

        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, PLAYS_SIDECAR_FILENAME)

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["session_id"], "session_001")
        self.assertEqual(payload["game_subdir"], "game_001")
        self.assertEqual(payload["clip_count"], 4)
        self.assertEqual(payload["play_count"], 3)
        # Game duration: last clip's end (22.4s) − first clip's start (10s) = 12.4s.
        self.assertAlmostEqual(payload["game_duration_seconds"], 12.4, places=3)
        self.assertEqual(len(payload["clips"]), 4)
        # Game-relative seconds — first clip starts at 0.0.
        self.assertEqual(payload["clips"][0]["clip_number"], 0)
        self.assertEqual(payload["clips"][0]["type"], CLIP_TYPE_PRE_GAME)
        self.assertIsNone(payload["clips"][0]["play_number"])
        self.assertEqual(payload["clips"][0]["start_seconds"], 0.0)
        self.assertAlmostEqual(payload["clips"][0]["length_seconds"], 0.4, places=3)
        self.assertEqual(payload["clips"][1]["type"], CLIP_TYPE_PLAY)
        self.assertEqual(payload["clips"][1]["play_number"], 1)
        self.assertAlmostEqual(payload["clips"][1]["start_seconds"], 0.4, places=3)
        self.assertAlmostEqual(payload["clips"][1]["length_seconds"], 4.5, places=3)
        self.assertEqual(payload["clips"][3]["play_number"], 3)

    def test_marked_flag_round_trips_into_payload(self) -> None:
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=1,
                   clip_type=CLIP_TYPE_PLAY, play_number=1, marked=True,
                   start_ns=400_000_000, end_ns=4_000_000_000)
        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(payload["clips"][0]["marked"])
        self.assertTrue(payload["clips"][1]["marked"])

    def test_timeout_and_challenge_emitted_with_null_play_number(self) -> None:
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=1,
                   clip_type=CLIP_TYPE_PLAY, play_number=1,
                   start_ns=400_000_000, end_ns=4_000_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=2,
                   clip_type=CLIP_TYPE_TIMEOUT,
                   start_ns=4_000_000_000, end_ns=4_300_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=3,
                   clip_type=CLIP_TYPE_CHALLENGE,
                   start_ns=4_300_000_000, end_ns=4_900_000_000)
        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        types = [c["type"] for c in payload["clips"]]
        self.assertEqual(
            types,
            [CLIP_TYPE_PRE_GAME, CLIP_TYPE_PLAY, CLIP_TYPE_TIMEOUT, CLIP_TYPE_CHALLENGE],
        )
        self.assertIsNone(payload["clips"][2]["play_number"])
        self.assertIsNone(payload["clips"][3]["play_number"])
        # Only the one type='play' clip counts toward play_count.
        self.assertEqual(payload["play_count"], 1)
        self.assertEqual(payload["clip_count"], 4)

    def test_empty_clips_writes_empty_array(self) -> None:
        # No clips in the DB — sidecar still written with empty array.
        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["clips"], [])
        self.assertEqual(payload["clip_count"], 0)
        self.assertEqual(payload["play_count"], 0)
        self.assertEqual(payload["game_duration_seconds"], 0.0)

    def test_open_clip_excluded_with_warning(self) -> None:
        # One closed clip, one open. JSON includes only the closed one.
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
        _seed_clip(self.db, game_subdir="game_001", clip_number=1,
                   clip_type=CLIP_TYPE_PLAY, play_number=1,
                   start_ns=400_000_000, end_ns=None)
        with self.assertLogs(
            "app.tools.clips_json_export", level="WARNING"
        ) as captured:
            path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["clip_count"], 1)
        self.assertEqual(payload["clips"][0]["type"], CLIP_TYPE_PRE_GAME)
        self.assertTrue(any("open clip" in msg for msg in captured.output))

    def test_auto_closed_on_crash_clips_included_normally(self) -> None:
        # The JSON contract doesn't surface the crash flag — consumers
        # don't need it. Lock that in so future changes don't leak the
        # internal field.
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000,
                   auto_closed_on_crash=True)
        path = write_plays_sidecar(self.db, self.session_path, "game_001")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["clip_count"], 1)
        self.assertNotIn(
            "auto_closed_on_crash",
            payload["clips"][0],
        )

    def test_sidecar_path_under_processed_game_subdir(self) -> None:
        _seed_clip(self.db, game_subdir="game_002", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
        path = write_plays_sidecar(self.db, self.session_path, "game_002")
        expected = (
            self.session_path / "processed" / "game_002" / PLAYS_SIDECAR_FILENAME
        )
        self.assertEqual(path, expected)

    def test_idempotent_rewrite(self) -> None:
        # Writing twice produces the same output. Second call
        # overwrites without error.
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
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
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
        _seed_clip(self.db, game_subdir="game_002", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=10_000_000_000, end_ns=10_400_000_000)
        _seed_clip(self.db, game_subdir="game_002", clip_number=1,
                   clip_type=CLIP_TYPE_PLAY, play_number=1,
                   start_ns=10_400_000_000, end_ns=14_000_000_000)
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
        self.assertEqual(g1["clip_count"], 1)
        self.assertEqual(g1["play_count"], 0)
        self.assertEqual(g2["clip_count"], 2)
        self.assertEqual(g2["play_count"], 1)

    def test_deduplicates_repeated_game_subdirs(self) -> None:
        # Caller might pass the same game multiple times (one per
        # feed in the long-form plan). We write one sidecar per
        # unique game.
        _seed_clip(self.db, game_subdir="game_001", clip_number=0,
                   clip_type=CLIP_TYPE_PRE_GAME,
                   start_ns=0, end_ns=400_000_000)
        written = write_plays_sidecars_for_session(
            self.db, self.session_path, ["game_001", "game_001", "game_001"]
        )
        self.assertEqual(len(written), 1)


if __name__ == "__main__":
    unittest.main()
