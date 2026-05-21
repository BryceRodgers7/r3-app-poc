"""Phase 14.A: orchestration for operator-marked clip boundaries.

Replaces the pre-Phase-14 `PlayManager`. Every moment of a recording
belongs to a clip; the operator drives transitions via four
buttons:

  - **Start Game** opens a `pre-game` clip (one per game).
  - **Next Play** closes the current clip and opens a `play` clip.
  - **Time-out** closes the current clip and opens a `timeout` clip.
  - **Challenge** closes the current clip and opens a `challenge`
    clip; rejected back-to-back so two challenges can't be opened
    without an intervening play / timeout.

`clip_number` is 0-indexed and monotonic per game across every
type. `play_number` is non-NULL only for clips of type `play` and
is 1-indexed per game so operator counters can report "Play 3"
without leaking timeouts into the count.

Mark Play (the `marked` flag) is a separate toggle the operator
can apply to any clip type; downstream consumers filter as needed.

Crash recovery uses `auto_close_open_clips_for_session` (called
from the §11.4 recovery scan) to close any clip whose
`end_session_time_ns` is NULL, using the latest finalized
segment's end. The `auto_closed_on_crash` flag distinguishes
operator-driven closures from system-driven ones.

Threading: lifecycle hooks fire from the Qt main thread (operator
button presses), but `current_play_number()` / `current_clip()`
are read from any thread driving an overlay tick. A coarse-grained
lock covers the in-memory pointer; the underlying `MetadataDb`
writes are serialized by its own lock.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.core.models import (
    CLIP_TYPE_CHALLENGE,
    CLIP_TYPE_PLAY,
    CLIP_TYPE_PRE_GAME,
    CLIP_TYPE_TIMEOUT,
    Clip,
)
from app.storage.metadata_db import MetadataDb

LOGGER = logging.getLogger(__name__)


class ClipManager:
    """Owns the per-game clip sequence and persists boundaries.

    A single instance is held by `ApplicationCoordinator` and
    receives lifecycle calls from `toggle_long_session_recording`
    (start / stop), `mark_next_play`, `mark_timeout`,
    `mark_challenge`, and `toggle_clip_mark`.
    """

    def __init__(self, db: MetadataDb) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._current: Clip | None = None
        # Cached so `current_play_number()` can answer during a
        # non-play clip (timeout / challenge) without a DB query each
        # overlay tick. Reset on `start_game` / `stop_game`.
        self._last_play_number: int | None = None

    # ----------------------------------------------------------------
    # Lifecycle hooks
    # ----------------------------------------------------------------

    def start_game(
        self,
        *,
        session_id: str,
        game_subdir: str,
        start_session_time_ns: int,
    ) -> Clip:
        """Open the first clip of a game (or continue after resume).

        Called from the start path of `toggle_long_session_recording`,
        immediately after the per-feed `enable_file_recording` calls
        and after the per-game replay-store filter is set. The
        `start_session_time_ns` should match the
        `game_start_session_time_ns` captured at the same moment so
        the clip's start aligns with the per-game replay scope.

        Fresh game (no existing clips in `(session_id, game_subdir)`):
        opens clip #0 of type `pre-game`. The first subsequent
        `mark_next_play` opens clip #1 with `play_number = 1`.

        Phase 7.D resume continuation (existing clips present — the
        recovery scan has already auto-closed any open one): opens a
        new clip of the **same type as the most recently closed
        clip**, with the next `clip_number` and (if the matched type
        is `play`) the next `play_number`. This matches operator
        intent — pick up where the crashed game left off rather than
        opening a fresh pre-game.

        Defensive: if a prior clip is still open in memory (shouldn't
        happen — `stop_game` is always called first), discard the
        in-memory pointer so we don't lose track.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._current is not None:
                LOGGER.warning(
                    "start_game called with an existing open clip %s/clip_%d; "
                    "discarding the in-memory pointer (DB row stays open)",
                    self._current.game_subdir,
                    self._current.clip_number,
                )
                self._current = None
            existing = self._db.clips_for_game(session_id, game_subdir)
            next_clip_number = (
                max((c.clip_number for c in existing), default=-1) + 1
            )
            if not existing:
                clip_type = CLIP_TYPE_PRE_GAME
                play_number: int | None = None
                self._last_play_number = None
            else:
                most_recent = max(existing, key=lambda c: c.clip_number)
                clip_type = most_recent.type
                if clip_type == CLIP_TYPE_PLAY:
                    next_play = (
                        max(
                            (
                                c.play_number
                                for c in existing
                                if c.play_number is not None
                            ),
                            default=0,
                        )
                        + 1
                    )
                    play_number = next_play
                    self._last_play_number = next_play
                else:
                    play_number = None
                    self._last_play_number = max(
                        (
                            c.play_number
                            for c in existing
                            if c.play_number is not None
                        ),
                        default=None,
                    )
            clip = Clip(
                session_id=session_id,
                game_subdir=game_subdir,
                clip_number=next_clip_number,
                type=clip_type,
                play_number=play_number,
                start_session_time_ns=start_session_time_ns,
                created_at=created_at,
            )
            clip_id = self._db.insert_clip(clip)
            self._current = Clip(
                clip_id=clip_id,
                session_id=clip.session_id,
                game_subdir=clip.game_subdir,
                clip_number=clip.clip_number,
                type=clip.type,
                play_number=clip.play_number,
                start_session_time_ns=clip.start_session_time_ns,
                created_at=clip.created_at,
            )
            LOGGER.info(
                "clip opened: %s/clip_%d type=%s play_number=%s at session_time=%dns",
                self._current.game_subdir,
                self._current.clip_number,
                self._current.type,
                self._current.play_number,
                self._current.start_session_time_ns,
            )
            return self._current

    def mark_next_play(self, now_session_time_ns: int) -> Clip | None:
        """Close the currently-open clip and open the next play.

        No-op (returns None) when no clip is open — defensive guard
        for a button press that arrives before `start_game`. On the
        happy path, returns the newly-opened play clip.
        """
        return self._open_next_clip_locked(
            now_session_time_ns=now_session_time_ns,
            new_type=CLIP_TYPE_PLAY,
            require_play_started=False,
            reject_if_current_type=None,
        )

    def mark_timeout(self, now_session_time_ns: int) -> Clip | None:
        """Close the currently-open clip and open a timeout clip.

        Rejected (returns None) before the first Next Play press of
        the game — the spec disables Time-out during pre-game.
        """
        return self._open_next_clip_locked(
            now_session_time_ns=now_session_time_ns,
            new_type=CLIP_TYPE_TIMEOUT,
            require_play_started=True,
            reject_if_current_type=None,
        )

    def mark_challenge(self, now_session_time_ns: int) -> Clip | None:
        """Close the currently-open clip and open a challenge clip.

        Rejected (returns None) when:
          - no play has been opened yet in this game (challenge
            requires a play to review)
          - the currently-open clip is already a challenge (no
            back-to-back challenges per spec — operator must press
            Next Play or Time-out first)
        """
        return self._open_next_clip_locked(
            now_session_time_ns=now_session_time_ns,
            new_type=CLIP_TYPE_CHALLENGE,
            require_play_started=True,
            reject_if_current_type=CLIP_TYPE_CHALLENGE,
        )

    def _open_next_clip_locked(
        self,
        *,
        now_session_time_ns: int,
        new_type: str,
        require_play_started: bool,
        reject_if_current_type: str | None,
    ) -> Clip | None:
        """Close the current clip and open one of `new_type`.

        Returns the newly-opened clip on success; None on rejection.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            current = self._current
            if current is None or current.clip_id is None:
                LOGGER.warning(
                    "open-next-clip (type=%s) called with no open clip — ignoring",
                    new_type,
                )
                return None
            if require_play_started and self._last_play_number is None:
                LOGGER.info(
                    "open-next-clip (type=%s) rejected: no play has started yet",
                    new_type,
                )
                return None
            if (
                reject_if_current_type is not None
                and current.type == reject_if_current_type
            ):
                LOGGER.info(
                    "open-next-clip (type=%s) rejected: current clip is already %s",
                    new_type,
                    reject_if_current_type,
                )
                return None
            self._db.close_clip(current.clip_id, now_session_time_ns)
            if new_type == CLIP_TYPE_PLAY:
                next_play_number = (
                    self._last_play_number + 1
                    if self._last_play_number is not None
                    else 1
                )
                play_number: int | None = next_play_number
            else:
                play_number = None
            next_clip = Clip(
                session_id=current.session_id,
                game_subdir=current.game_subdir,
                clip_number=current.clip_number + 1,
                type=new_type,
                play_number=play_number,
                start_session_time_ns=now_session_time_ns,
                created_at=created_at,
            )
            clip_id = self._db.insert_clip(next_clip)
            self._current = Clip(
                clip_id=clip_id,
                session_id=next_clip.session_id,
                game_subdir=next_clip.game_subdir,
                clip_number=next_clip.clip_number,
                type=next_clip.type,
                play_number=next_clip.play_number,
                start_session_time_ns=next_clip.start_session_time_ns,
                created_at=next_clip.created_at,
            )
            if play_number is not None:
                self._last_play_number = play_number
            LOGGER.info(
                "clip boundary: closed %s/clip_%d at session_time=%dns; "
                "opened %s/clip_%d type=%s play_number=%s",
                current.game_subdir,
                current.clip_number,
                now_session_time_ns,
                self._current.game_subdir,
                self._current.clip_number,
                self._current.type,
                self._current.play_number,
            )
            return self._current

    def toggle_clip_mark(self) -> Clip | None:
        """Flip the `marked` flag on the currently-open clip.

        No-op (returns None) when no clip is open. Returns the
        updated clip snapshot on success.
        """
        with self._lock:
            current = self._current
            if current is None or current.clip_id is None:
                LOGGER.warning("toggle_clip_mark called with no open clip — ignoring")
                return None
            new_marked = not current.marked
            self._db.set_clip_marked(current.clip_id, new_marked)
            self._current = Clip(
                clip_id=current.clip_id,
                session_id=current.session_id,
                game_subdir=current.game_subdir,
                clip_number=current.clip_number,
                type=current.type,
                play_number=current.play_number,
                marked=new_marked,
                start_session_time_ns=current.start_session_time_ns,
                end_session_time_ns=current.end_session_time_ns,
                created_at=current.created_at,
                auto_closed_on_crash=current.auto_closed_on_crash,
            )
            LOGGER.info(
                "clip mark toggled: %s/clip_%d marked=%s",
                self._current.game_subdir,
                self._current.clip_number,
                self._current.marked,
            )
            return self._current

    def stop_game(self, end_session_time_ns: int) -> None:
        """Close the currently-open clip and clear the pointer.

        Called from the stop path of `toggle_long_session_recording`,
        immediately before the per-feed `disable_file_recording`
        calls. The `end_session_time_ns` should be captured at the
        same moment as the recording-stop transition.
        """
        with self._lock:
            current = self._current
            if current is None or current.clip_id is None:
                return
            self._db.close_clip(current.clip_id, end_session_time_ns)
            LOGGER.info(
                "clip closed (game stop): %s/clip_%d type=%s at session_time=%dns",
                current.game_subdir,
                current.clip_number,
                current.type,
                end_session_time_ns,
            )
            self._current = None
            self._last_play_number = None

    # ----------------------------------------------------------------
    # Read accessors
    # ----------------------------------------------------------------

    def current_clip(self) -> Clip | None:
        """Return a snapshot of the currently-open clip, or None."""
        with self._lock:
            return self._current

    def current_clip_number(self) -> int | None:
        """Convenience for the operator-window clip counter."""
        with self._lock:
            return self._current.clip_number if self._current is not None else None

    def current_play_number(self) -> int | None:
        """Return the most-recent play number opened in this game.

        During a `play` clip this is the current clip's `play_number`.
        During a `timeout` / `challenge` clip this is the last
        opened play's number (cached on transition). Returns None
        during pre-game and after `stop_game`.
        """
        with self._lock:
            if self._current is None:
                return None
            if self._current.play_number is not None:
                return self._current.play_number
            return self._last_play_number

    # ----------------------------------------------------------------
    # Crash-recovery helpers
    # ----------------------------------------------------------------

    def auto_close_open_clips_for_session(
        self,
        *,
        session_id: str,
        fallback_end_session_time_ns: int,
    ) -> int:
        """Close every clip in `session_id` whose `end_session_time_ns`
        is still NULL.

        Called from the §11.4 recovery scan when a session is being
        marked DIRTY → adopted on resume. The fallback end is the
        latest finalized segment's `end_session_time_ns` for that
        session (the last frame the operator could possibly have
        seen). All such closures get `auto_closed_on_crash = TRUE`
        so the persisted history distinguishes operator-driven
        boundaries from system-driven ones.

        Returns the number of clips closed.
        """
        open_clips = self._db.open_clips_for_session(session_id)
        closed = 0
        for clip in open_clips:
            if clip.clip_id is None:
                continue
            self._db.close_clip(
                clip.clip_id,
                fallback_end_session_time_ns,
                auto_closed_on_crash=True,
            )
            closed += 1
        if closed:
            LOGGER.info(
                "auto-closed %d open clip(s) in %s at end_session_time=%dns",
                closed,
                session_id,
                fallback_end_session_time_ns,
            )
        return closed
