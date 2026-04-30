"""Per-app monotonic session clock (§8.1).

Defines a single time origin for the lifetime of one application run.
Session time is `time.monotonic_ns() - session_start_monotonic_ns`. Wall
clock is intentionally NOT used — wall clock can jump (NTP, manual
adjustment) which would corrupt the replay timeline (§8.2).

The clock itself is dead-simple. The interesting work happens at the
recording-segment boundary, where each new segment captures
`session_time` at first buffer and stores `pts_to_session_offset_ns`
on its `Segment` row (§6.3, §8.3). A reconnect starts a fresh segment
with a fresh offset — the feed clock is allowed to jump.

Phase 5.A is the data layer only; replay queries that read
`start_session_time_ns` / `end_session_time_ns` land in 5.B and 5.C.
"""

from __future__ import annotations

from collections.abc import Callable
import time


class SessionClock:
    """Monotonic-anchored session clock.

    `now_session_time_ns()` returns nanoseconds since the clock was
    constructed. The optional `clock_ns` parameter lets tests inject a
    deterministic source.

    Phase 7.D adds `rebase(anchor_session_time_ns)`. The session_time
    origin is normally the moment the SessionClock is constructed, but
    on resume-after-crash the coordinator rebases the new clock past
    the latest pre-crash segment's `end_session_time_ns` so post-resume
    session_time is strictly greater than any pre-crash value. That
    keeps integer comparison meaningful across the crash and lets the
    per-game filter (Phase 7.B-ext) treat pre-crash and post-resume
    segments as one continuous game.
    """

    __slots__ = ("_clock_ns", "_start_monotonic_ns")

    def __init__(self, *, clock_ns: Callable[[], int] | None = None) -> None:
        self._clock_ns = clock_ns or time.monotonic_ns
        self._start_monotonic_ns = self._clock_ns()

    @property
    def session_start_monotonic_ns(self) -> int:
        """The monotonic-ns reading at which the clock was constructed."""
        return self._start_monotonic_ns

    def now_session_time_ns(self) -> int:
        """Return nanoseconds elapsed since session start."""
        return self._clock_ns() - self._start_monotonic_ns

    def rebase(self, anchor_session_time_ns: int) -> None:
        """Reset the clock origin so `now_session_time_ns()` returns
        `anchor_session_time_ns` at the moment of this call.

        Used by the resume-after-crash path: `coordinator.initialize`
        loads pre-crash segments from SQLite, finds the latest
        `end_session_time_ns`, and rebases the new SessionClock to
        that value (plus a small gap). All post-resume segments are
        then guaranteed to have session_time strictly greater than
        any pre-crash segment, so the per-game filter and
        cross-segment range queries stay correct across the crash.

        Safe only before any buffer has been processed by the new
        clock. Once `PipelineManager._on_jpegenc_buffer_probe` has
        captured a `session_time` for a writing segment, rebasing
        would corrupt that segment's metadata.
        """
        self._start_monotonic_ns = self._clock_ns() - anchor_session_time_ns
