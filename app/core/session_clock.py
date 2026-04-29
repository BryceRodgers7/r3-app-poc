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
