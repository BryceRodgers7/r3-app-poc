"""`plays.json` sidecar per game (Phase 8.D, updated in Phase 14.A).

For each game in a finalized session, write a single
`<session>/processed/<game_subdir>/plays.json` describing the
operator-marked clips. Editors and scoring tools consume the JSON
to seek inside the matching `<feed>.mp4` produced by Phase 8.B.

Phase 14.A: the data source is the new `clips` table. Every clip
type is emitted (pre-game, play, timeout, challenge) along with the
`marked` flag so downstream tooling can filter as needed. Phase
14.E will rename this file to `clips_json_export.py` to match the
vocabulary; the on-disk sidecar filename and CLI surface stay
backwards-compatible with downstream consumers in this repo.

Shape::

    {
      "session_id": "session_NNN",
      "game_subdir": "game_NNN",
      "clip_count": 3,
      "play_count": 2,
      "game_duration_seconds": 7.7,
      "clips": [
        {"clip_number": 0, "type": "pre-game", "play_number": null, "marked": false,
         "start_seconds": 0.0, "length_seconds": 0.4},
        {"clip_number": 1, "type": "play", "play_number": 1, "marked": false,
         "start_seconds": 0.4, "length_seconds": 4.1},
        {"clip_number": 2, "type": "play", "play_number": 2, "marked": true,
         "start_seconds": 4.5, "length_seconds": 3.2}
      ]
    }

`start_seconds` is **game-relative** (zero is the first clip's
start). `length_seconds` is the clip's duration.
`auto_closed_on_crash` clips are included normally — the JSON
doesn't distinguish them because consumers don't need to.

Open clips (`end_session_time_ns is None`) are excluded with a
warning — by the time the post-processor runs the session must be
finalized, so every clip should be closed; an open clip is a sign
of a recovery edge case.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.models import Clip
from app.storage.metadata_db import MetadataDb
from app.tools.post_session_processor import PROCESSED_DIRNAME

LOGGER = logging.getLogger(__name__)

PLAYS_SIDECAR_FILENAME = "plays.json"


def write_plays_sidecar(
    db: MetadataDb,
    session_path: Path,
    game_subdir: str,
) -> Path:
    """Write `<session>/processed/<game_subdir>/plays.json`.

    Always writes (overwrites any existing file) — the JSON is small,
    deterministic, and a regenerate-on-every-run policy is simpler
    than tracking idempotency.

    Returns the path written.
    """
    session_id = session_path.name
    clips = db.clips_for_game(session_id=session_id, game_subdir=game_subdir)
    payload = _build_payload(
        session_id=session_id,
        game_subdir=game_subdir,
        clips=clips,
    )
    output_dir = session_path / PROCESSED_DIRNAME / game_subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / PLAYS_SIDECAR_FILENAME
    output_path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    LOGGER.info(
        "wrote plays sidecar: %s (%d clip(s), %d play(s))",
        output_path,
        payload["clip_count"],
        payload["play_count"],
    )
    return output_path


def write_plays_sidecars_for_session(
    db: MetadataDb,
    session_path: Path,
    game_subdirs: list[str],
) -> list[Path]:
    """Write one sidecar per game in the supplied list.

    `game_subdirs` is supplied by the caller (typically the unique
    set of game_subdirs from the long-form export plan) so we don't
    need to re-scan the recording folder.
    """
    written: list[Path] = []
    for game_subdir in sorted(set(game_subdirs)):
        try:
            written.append(write_plays_sidecar(db, session_path, game_subdir))
        except Exception:
            LOGGER.exception(
                "failed to write plays sidecar for %s", game_subdir
            )
    return written


def _build_payload(
    *,
    session_id: str,
    game_subdir: str,
    clips: list[Clip],
) -> dict:
    closed_clips = [c for c in clips if c.end_session_time_ns is not None]
    skipped_open = len(clips) - len(closed_clips)
    if skipped_open:
        LOGGER.warning(
            "%s/%s has %d open clip(s) (no end_session_time_ns) — "
            "excluded from sidecar; expected for a finalized session",
            session_id,
            game_subdir,
            skipped_open,
        )

    if not closed_clips:
        return {
            "session_id": session_id,
            "game_subdir": game_subdir,
            "clip_count": 0,
            "play_count": 0,
            "game_duration_seconds": 0.0,
            "clips": [],
        }

    game_origin_ns = closed_clips[0].start_session_time_ns
    last_end_ns = max(c.end_session_time_ns for c in closed_clips)  # type: ignore[type-var]
    game_duration_seconds = max(0.0, (last_end_ns - game_origin_ns) / 1_000_000_000.0)

    clips_payload = []
    play_count = 0
    for clip in closed_clips:
        assert clip.end_session_time_ns is not None
        start_seconds = (clip.start_session_time_ns - game_origin_ns) / 1_000_000_000.0
        length_seconds = (
            clip.end_session_time_ns - clip.start_session_time_ns
        ) / 1_000_000_000.0
        if clip.is_play:
            play_count += 1
        clips_payload.append(
            {
                "clip_number": clip.clip_number,
                "type": clip.type,
                "play_number": clip.play_number,
                "marked": clip.marked,
                "start_seconds": round(start_seconds, 3),
                "length_seconds": round(length_seconds, 3),
            }
        )

    return {
        "session_id": session_id,
        "game_subdir": game_subdir,
        "clip_count": len(clips_payload),
        "play_count": play_count,
        "game_duration_seconds": round(game_duration_seconds, 3),
        "clips": clips_payload,
    }
