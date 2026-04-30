"""Phase 8.D — `plays.json` sidecar per game.

For each game in a finalized session, write a single
`<session>/processed/<game_subdir>/plays.json` describing the
operator-marked play boundaries. Editors and scoring tools consume
the JSON to seek inside the matching `<feed>.mp4` produced by
Phase 8.B.

The JSON is per-game, not per-feed — plays are operator-scoped
(§6.7), so the same play list applies to every camera angle.

Shape::

    {
      "session_id": "session_NNN",
      "game_subdir": "game_NNN",
      "play_count": 2,
      "game_duration_seconds": 7.7,
      "plays": [
        {"play_number": 1, "start_seconds": 0.0,  "length_seconds": 4.5},
        {"play_number": 2, "start_seconds": 4.5,  "length_seconds": 3.2}
      ]
    }

`start_seconds` is **game-relative** (zero is the first play's
start). `length_seconds` is the play's duration. `auto_closed_on_crash`
plays are included normally — the JSON doesn't distinguish them
because consumers don't need to.

Open plays (`end_session_time_ns is None`) are excluded with a
warning — by the time the post-processor runs the session must be
finalized, so every play should be closed; an open play is a sign
of a recovery edge case.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.models import Play
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
    plays = db.plays_for_game(session_id=session_id, game_subdir=game_subdir)
    payload = _build_payload(
        session_id=session_id,
        game_subdir=game_subdir,
        plays=plays,
    )
    output_dir = session_path / PROCESSED_DIRNAME / game_subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / PLAYS_SIDECAR_FILENAME
    output_path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    LOGGER.info(
        "wrote plays sidecar: %s (%d plays)",
        output_path,
        len(payload["plays"]),
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
    plays: list[Play],
) -> dict:
    closed_plays = [p for p in plays if p.end_session_time_ns is not None]
    skipped_open = len(plays) - len(closed_plays)
    if skipped_open:
        LOGGER.warning(
            "%s/%s has %d open play(s) (no end_session_time_ns) — "
            "excluded from sidecar; expected for a finalized session",
            session_id,
            game_subdir,
            skipped_open,
        )

    if not closed_plays:
        return {
            "session_id": session_id,
            "game_subdir": game_subdir,
            "play_count": 0,
            "game_duration_seconds": 0.0,
            "plays": [],
        }

    game_origin_ns = closed_plays[0].start_session_time_ns
    last_end_ns = max(p.end_session_time_ns for p in closed_plays)  # type: ignore[type-var]
    game_duration_seconds = max(0.0, (last_end_ns - game_origin_ns) / 1_000_000_000.0)

    plays_payload = []
    for play in closed_plays:
        assert play.end_session_time_ns is not None
        start_seconds = (play.start_session_time_ns - game_origin_ns) / 1_000_000_000.0
        length_seconds = (
            play.end_session_time_ns - play.start_session_time_ns
        ) / 1_000_000_000.0
        plays_payload.append(
            {
                "play_number": play.play_number,
                "start_seconds": round(start_seconds, 3),
                "length_seconds": round(length_seconds, 3),
            }
        )

    return {
        "session_id": session_id,
        "game_subdir": game_subdir,
        "play_count": len(plays_payload),
        "game_duration_seconds": round(game_duration_seconds, 3),
        "plays": plays_payload,
    }
