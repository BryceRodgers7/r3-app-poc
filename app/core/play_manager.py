"""Phase 7.H.1: orchestration for operator-marked play boundaries.

Every moment of a recording belongs to a play. `PlayManager` owns the
in-memory "currently-open play" pointer and persists boundary
transitions to the `plays` SQLite table. The pointer is the authoritative
source for the playback overlay's `Play #N` badge (Phase 7.H.3) and for
the upcoming Replay-Play transport (Phase 7.H.4).

Lifecycle (driven by `ApplicationCoordinator.toggle_long_session_recording`
and the `mark_next_play` button):

  - `start_game(session_id, game_subdir, start_session_time_ns)` opens
    Play #1 of the new game.
  - `mark_next_play(now_session_time_ns)` closes the currently-open
    play and immediately opens the next play (`play_number + 1`).
  - `stop_game(end_session_time_ns)` closes the currently-open play
    and clears the pointer.

Crash recovery uses `auto_close_open_plays_for_session` (called from
the §11.4 recovery scan) to close any play whose
`end_session_time_ns` is NULL using the latest finalized segment's
end. The `auto_closed_on_crash` flag distinguishes operator-driven
closures from system-driven ones in the persisted history.

Threading: lifecycle hooks fire from the Qt main thread (operator
button presses), but `current_play()` is read from any thread that
needs the play number for an overlay tick. A coarse-grained lock
covers the in-memory pointer; the underlying `MetadataDb` writes are
serialized by its own lock.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.core.models import Play
from app.storage.metadata_db import MetadataDb

LOGGER = logging.getLogger(__name__)


class PlayManager:
    """Owns the per-game play sequence and persists boundaries.

    A single instance is held by `ApplicationCoordinator` and
    receives lifecycle calls from `toggle_long_session_recording`
    (start / stop) and from the Next Play button.
    """

    def __init__(self, db: MetadataDb) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._current: Play | None = None

    # ----------------------------------------------------------------
    # Lifecycle hooks
    # ----------------------------------------------------------------

    def start_game(
        self,
        *,
        session_id: str,
        game_subdir: str,
        start_session_time_ns: int,
    ) -> Play:
        """Open the next play of a game.

        Called from the start path of `toggle_long_session_recording`,
        immediately after the per-feed `enable_file_recording` calls
        and after the per-game replay-store filter is set. The
        `start_session_time_ns` should match the `game_start_session_time_ns`
        captured at the same moment so the play's start aligns with
        the per-game replay scope.

        For a brand-new game folder this opens **Play #1**. For the
        Phase 7.D resume-continuation case (where the operator picks
        Resume and the existing `game_NNN/` is reused), this opens
        `play_number = max(existing) + 1` — pre-crash plays must
        already be auto-closed via `auto_close_open_plays_for_session`
        before this is called, otherwise the in-DB UNIQUE constraint
        would catch it.

        Defensive: if a prior play is still open in memory (shouldn't
        happen — `stop_game` is always called first), close it locally
        so we don't lose the pointer.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._current is not None:
                LOGGER.warning(
                    "start_game called with an existing open play %s/play_%d; "
                    "discarding the in-memory pointer (DB row stays open)",
                    self._current.game_subdir,
                    self._current.play_number,
                )
            existing = self._db.plays_for_game(session_id, game_subdir)
            next_number = max((p.play_number for p in existing), default=0) + 1
            play = Play(
                session_id=session_id,
                game_subdir=game_subdir,
                play_number=next_number,
                start_session_time_ns=start_session_time_ns,
                created_at=created_at,
            )
            play_id = self._db.insert_play(play)
            self._current = Play(
                play_id=play_id,
                session_id=play.session_id,
                game_subdir=play.game_subdir,
                play_number=play.play_number,
                start_session_time_ns=play.start_session_time_ns,
                created_at=play.created_at,
            )
            LOGGER.info(
                "play opened: %s/play_%d at session_time=%dns",
                self._current.game_subdir,
                self._current.play_number,
                self._current.start_session_time_ns,
            )
            return self._current

    def mark_next_play(self, now_session_time_ns: int) -> Play | None:
        """Close the currently-open play and open the next.

        No-op (returns None) when no play is open — defensive guard
        for a button press that arrives before `start_game` (the UI
        layer disables the button when not RECORDING, but a stale
        signal could still race). On the happy path, returns the
        newly-opened next play.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            current = self._current
            if current is None or current.play_id is None:
                LOGGER.warning(
                    "mark_next_play called with no open play — ignoring"
                )
                return None
            self._db.close_play(current.play_id, now_session_time_ns)
            next_play = Play(
                session_id=current.session_id,
                game_subdir=current.game_subdir,
                play_number=current.play_number + 1,
                start_session_time_ns=now_session_time_ns,
                created_at=created_at,
            )
            play_id = self._db.insert_play(next_play)
            self._current = Play(
                play_id=play_id,
                session_id=next_play.session_id,
                game_subdir=next_play.game_subdir,
                play_number=next_play.play_number,
                start_session_time_ns=next_play.start_session_time_ns,
                created_at=next_play.created_at,
            )
            LOGGER.info(
                "play boundary: closed %s/play_%d at session_time=%dns; "
                "opened %s/play_%d",
                current.game_subdir,
                current.play_number,
                now_session_time_ns,
                self._current.game_subdir,
                self._current.play_number,
            )
            return self._current

    def stop_game(self, end_session_time_ns: int) -> None:
        """Close the currently-open play and clear the pointer.

        Called from the stop path of `toggle_long_session_recording`,
        immediately before the per-feed `disable_file_recording`
        calls. The `end_session_time_ns` should be captured at the
        same moment as the recording-stop transition.
        """
        with self._lock:
            current = self._current
            if current is None or current.play_id is None:
                # No play to close. Either start_game was never
                # called or stop_game was double-fired.
                return
            self._db.close_play(current.play_id, end_session_time_ns)
            LOGGER.info(
                "play closed (game stop): %s/play_%d at session_time=%dns",
                current.game_subdir,
                current.play_number,
                end_session_time_ns,
            )
            self._current = None

    # ----------------------------------------------------------------
    # Read accessors
    # ----------------------------------------------------------------

    def current_play(self) -> Play | None:
        """Return a snapshot of the currently-open play, or None."""
        with self._lock:
            return self._current

    def current_play_number(self) -> int | None:
        """Convenience for the playback-overlay badge."""
        with self._lock:
            return self._current.play_number if self._current is not None else None

    # ----------------------------------------------------------------
    # Crash-recovery helpers
    # ----------------------------------------------------------------

    def auto_close_open_plays_for_session(
        self,
        *,
        session_id: str,
        fallback_end_session_time_ns: int,
    ) -> int:
        """Close every play in `session_id` whose `end_session_time_ns`
        is still NULL.

        Called from the §11.4 recovery scan when a session is being
        marked DIRTY → adopted on resume. The fallback end is the
        latest finalized segment's `end_session_time_ns` for that
        session (the last frame the operator could possibly have
        seen). All such closures get `auto_closed_on_crash = TRUE`
        so the persisted history distinguishes operator-driven
        boundaries from system-driven ones.

        Returns the number of plays closed.
        """
        open_plays = self._db.open_plays_for_session(session_id)
        closed = 0
        for play in open_plays:
            if play.play_id is None:
                continue
            self._db.close_play(
                play.play_id,
                fallback_end_session_time_ns,
                auto_closed_on_crash=True,
            )
            closed += 1
        if closed:
            LOGGER.info(
                "auto-closed %d open play(s) in %s at end_session_time=%dns",
                closed,
                session_id,
                fallback_end_session_time_ns,
            )
        return closed
