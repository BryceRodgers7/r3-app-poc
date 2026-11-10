"""Challenge details: the open challenge, its timer, and the stored record.

When the operator presses **Challenge**, a challenge opens and its timer
starts. The operator fills in who challenged, the category and the
outcome, then presses **Challenge Done**: the timer stops and one record
is appended to `<session>/challenges.json`. **Discard** drops an open
challenge without a record (a mis-press).

This is deliberately separate from the clip boundaries in `ClipManager`
(the `challenge` clip row and the referee lockout), which end on Next Play,
Time-out or End Game as before. A challenge record can outlive its clip: it
ends only on Done or Discard, because the timer is defined as Challenge
press to Challenge Done press.

Nothing here touches the database. Records go to a JSON file, in the
session folder, one file per session (each record names its game).

The clock is the session clock (`SessionClock.now_session_time_ns`), so a
record's times line up with the recorded video and with the clip rows.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.challenge_options import CATEGORY_MAX_LENGTH, OUTCOME_MAX_LENGTH

LOGGER = logging.getLogger(__name__)

CHALLENGES_FILENAME = "challenges.json"
SCHEMA_VERSION = 1

TEAM_A = "A"
TEAM_B = "B"
TEAM_SIDES = (TEAM_A, TEAM_B)


class ChallengeError(ValueError):
    """A challenge could not be finished because the details are not valid."""


@dataclass(slots=True)
class ActiveChallenge:
    """The challenge that is open right now."""

    session_id: str
    game_subdir: str
    clip_number: int | None
    play_number: int | None
    started_session_time_ns: int
    started_at: str  # wall-clock UTC, ISO 8601
    # Set by the first Challenge Done press. Once set the timer is frozen,
    # so a retry after a failed save does not keep counting.
    stopped_session_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class ChallengeRecord:
    """One finished challenge, as written to `challenges.json`."""

    challenge_number: int  # 1-based, per game
    session_id: str
    game_subdir: str
    play_number: int | None  # the play that was challenged
    clip_number: int | None  # the `challenge` clip that was opened
    team_side: str  # "A" or "B"
    team: str  # the team's name as entered in the pre-match dialog
    category: str
    outcome: str
    elapsed_seconds: float
    started_session_time_ns: int
    ended_session_time_ns: int
    started_at: str
    completed_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "challenge_number": self.challenge_number,
            "game_subdir": self.game_subdir,
            "play_number": self.play_number,
            "clip_number": self.clip_number,
            "team_side": self.team_side,
            "team": self.team,
            "category": self.category,
            "outcome": self.outcome,
            "elapsed_seconds": self.elapsed_seconds,
            "started_session_time_ns": self.started_session_time_ns,
            "ended_session_time_ns": self.ended_session_time_ns,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_json(cls, session_id: str, data: dict[str, Any]) -> "ChallengeRecord":
        return cls(
            challenge_number=int(data["challenge_number"]),
            session_id=session_id,
            game_subdir=str(data["game_subdir"]),
            play_number=_optional_int(data.get("play_number")),
            clip_number=_optional_int(data.get("clip_number")),
            team_side=str(data["team_side"]),
            team=str(data["team"]),
            category=str(data["category"]),
            outcome=str(data["outcome"]),
            elapsed_seconds=float(data["elapsed_seconds"]),
            started_session_time_ns=int(data["started_session_time_ns"]),
            ended_session_time_ns=int(data["ended_session_time_ns"]),
            started_at=str(data["started_at"]),
            completed_at=str(data["completed_at"]),
        )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[arg-type]


def format_duration(seconds: float) -> str:
    """Render a duration as `m:ss`, or `h:mm:ss` from one hour up."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class ChallengeManager:
    """Tracks the one open challenge and builds its record.

    Holds no I/O: the coordinator starts it on the Challenge press and,
    on Done, asks it for a record, stores that, then calls `finish()`.
    Only one challenge can be open at a time.
    """

    def __init__(self, categories: Sequence[str], outcomes: Sequence[str]) -> None:
        self._categories = tuple(categories)
        self._outcomes = tuple(outcomes)
        self._active: ActiveChallenge | None = None

    @property
    def categories(self) -> tuple[str, ...]:
        return self._categories

    @property
    def outcomes(self) -> tuple[str, ...]:
        return self._outcomes

    @property
    def active(self) -> ActiveChallenge | None:
        return self._active

    @property
    def is_active(self) -> bool:
        return self._active is not None

    def start(
        self,
        *,
        session_id: str,
        game_subdir: str,
        clip_number: int | None,
        play_number: int | None,
        now_session_time_ns: int,
    ) -> ActiveChallenge:
        """Open a challenge and start its timer."""
        if self._active is not None:
            raise ChallengeError("A challenge is already open.")
        self._active = ActiveChallenge(
            session_id=session_id,
            game_subdir=game_subdir,
            clip_number=clip_number,
            play_number=play_number,
            started_session_time_ns=now_session_time_ns,
            started_at=_utc_now_iso(),
        )
        return self._active

    def set_play_number(self, play_number: int | None) -> None:
        """Record which play is actually under review.

        The challenge opens on the last play, but the referee can step back
        to an earlier one (the operator may have pressed Next Play before
        the challenge was called). The stored record names the play that
        was reviewed. No-op when no challenge is open.
        """
        if self._active is not None:
            self._active.play_number = play_number

    def elapsed_seconds(self, now_session_time_ns: int) -> float | None:
        """Seconds on the challenge timer, or None when none is open.

        Frozen at the first Challenge Done press.
        """
        active = self._active
        if active is None:
            return None
        end = (
            active.stopped_session_time_ns
            if active.stopped_session_time_ns is not None
            else now_session_time_ns
        )
        return max(0.0, (end - active.started_session_time_ns) / 1e9)

    def build_record(
        self,
        *,
        challenge_number: int,
        team_side: str,
        team_name: str,
        category: str,
        outcome: str,
        now_session_time_ns: int,
    ) -> ChallengeRecord:
        """Validate the details, stop the timer and return the record.

        Raises `ChallengeError` (and leaves the timer running) when a
        detail is missing or not one of the configured choices. The open
        challenge stays open until `finish()`, so a failed save can be
        retried with the same elapsed time.
        """
        active = self._require_active()
        if team_side not in TEAM_SIDES:
            raise ChallengeError("Choose the team that challenged.")
        if not team_name.strip():
            raise ChallengeError("The challenging team has no name.")
        if not category.strip() or category not in self._categories:
            raise ChallengeError("Choose a challenge category.")
        if not outcome.strip() or outcome not in self._outcomes:
            raise ChallengeError("Choose the challenge outcome.")
        # Guarded again here so a hand-edited list can never write a value
        # the char(N) limits forbid.
        if len(category) > CATEGORY_MAX_LENGTH or len(outcome) > OUTCOME_MAX_LENGTH:
            raise ChallengeError("Category or outcome is longer than its limit.")

        if active.stopped_session_time_ns is None:
            active.stopped_session_time_ns = now_session_time_ns
        elapsed = (active.stopped_session_time_ns - active.started_session_time_ns) / 1e9
        return ChallengeRecord(
            challenge_number=challenge_number,
            session_id=active.session_id,
            game_subdir=active.game_subdir,
            play_number=active.play_number,
            clip_number=active.clip_number,
            team_side=team_side,
            team=team_name.strip(),
            category=category,
            outcome=outcome,
            elapsed_seconds=round(max(0.0, elapsed), 3),
            started_session_time_ns=active.started_session_time_ns,
            ended_session_time_ns=active.stopped_session_time_ns,
            started_at=active.started_at,
            completed_at=_utc_now_iso(),
        )

    def finish(self) -> None:
        """Close the open challenge after its record is stored."""
        self._active = None

    def discard(self) -> ActiveChallenge | None:
        """Drop the open challenge without a record. Returns what was open."""
        active, self._active = self._active, None
        return active

    def _require_active(self) -> ActiveChallenge:
        if self._active is None:
            raise ChallengeError("No challenge is open.")
        return self._active


@dataclass(slots=True)
class ChallengeStore:
    """The session's `challenges.json`. Not a database: one JSON file."""

    path: Path
    session_id: str

    def load(self) -> list[ChallengeRecord]:
        """Every stored record, oldest first. A missing file is an empty list.

        A damaged file is logged and treated as empty so the next save does
        not crash; the damaged file is kept aside as `.corrupt` first so
        nothing is overwritten silently.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            raw = json.loads(text)
            return [
                ChallengeRecord.from_json(self.session_id, item)
                for item in raw["challenges"]
            ]
        except (ValueError, KeyError, TypeError):
            LOGGER.exception("Could not read %s; setting it aside.", self.path)
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
            except OSError:
                LOGGER.exception("Could not move the damaged %s aside.", self.path)
            return []

    def next_number(self, game_subdir: str) -> int:
        """The next 1-based challenge number within `game_subdir`."""
        return 1 + sum(1 for r in self.load() if r.game_subdir == game_subdir)

    def append(self, record: ChallengeRecord) -> None:
        """Add `record` to the file, rewriting it atomically.

        Raises `OSError` when the file cannot be written.
        """
        records = self.load()
        records.append(record)
        payload = {
            "version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "challenges": [r.to_json() for r in records],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp_path, self.path)


def challenges_path(session_root: Path) -> Path:
    """Where a session's challenge records live."""
    return session_root / CHALLENGES_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
