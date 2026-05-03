"""Per-feed reconnect supervisor (Phase 10.B).

Drives the `FeedState` machine through `DISCONNECTED → RECONNECTING →
LIVE | FAILED` on a backoff schedule. The supervisor itself owns only
the scheduling and accounting; the actual rebuild of the source-side
ingest pipeline is performed by an injected callable (typically
`FeedRuntime.rebuild`).

Threading: state-machine listeners run on whatever thread fired the
transition (bus thread for ERROR-driven disconnects, telemetry thread
for zero-fps drops, Qt thread for explicit operator-driven calls). The
scheduler thread is whatever the `Scheduler` impl chooses; the default
`_ThreadingScheduler` uses `threading.Timer` (a one-shot daemon
thread). All supervisor state is guarded by a single `RLock` so a
listener that re-enters the supervisor (e.g. via a transition fired
from inside the supervisor itself) does not deadlock.

Tests inject a `FakeScheduler` and exercise the schedule deterministically
without real time passing.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
from typing import Any, Protocol

from app.core.feed_state import FeedState
from app.core.health_events import HealthSeverity, default_log, record_health_event
from app.core.state_machine import StateMachine

LOGGER = logging.getLogger(__name__)


# Backoff schedule: 1s, 2s, 4s, 8s, 16s, then capped at 30s. Ten
# attempts total covers ~3 minutes of backoff before giving up — enough
# for a network blip / NDI sender restart, short of a silent infinite
# retry loop on a genuinely-broken cable.
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (
    1.0,
    2.0,
    4.0,
    8.0,
    16.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
)


class Scheduler(Protocol):
    """Schedule a one-shot callback after `delay` seconds.

    Returns a handle that can be passed to `cancel`. The handle is
    treated as opaque by the supervisor; any object the impl wants to
    use is fine.
    """

    def schedule(self, delay: float, callback: Callable[[], None]) -> Any: ...
    def cancel(self, handle: Any) -> None: ...


class _ThreadingScheduler:
    """Production scheduler backed by `threading.Timer`."""

    def schedule(self, delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        return timer

    def cancel(self, handle: threading.Timer) -> None:
        handle.cancel()


class ReconnectSupervisor:
    """Drives backoff-scheduled reconnect attempts for one feed."""

    def __init__(
        self,
        *,
        feed_id: str,
        display_name: str,
        feed_state: StateMachine[FeedState],
        rebuild_callable: Callable[[], bool],
        backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        scheduler: Scheduler | None = None,
    ) -> None:
        if not backoff_seconds:
            raise ValueError("backoff_seconds must be non-empty")
        self._feed_id = feed_id
        self._display_name = display_name
        self._feed_state = feed_state
        self._rebuild = rebuild_callable
        self._backoff = tuple(backoff_seconds)
        self._scheduler: Scheduler = scheduler if scheduler is not None else _ThreadingScheduler()
        self._lock = threading.RLock()
        self._handle: Any = None
        self._attempt_index = 0
        self._failed_emitted = False
        self._stopped = False
        self._attached = False

    @property
    def attempt_index(self) -> int:
        with self._lock:
            return self._attempt_index

    @property
    def is_pending(self) -> bool:
        with self._lock:
            return self._handle is not None

    def attach(self) -> None:
        """Subscribe to feed-state transitions. Idempotent."""
        with self._lock:
            if self._attached:
                return
            self._attached = True
        self._feed_state.add_listener(self._on_state_change)

    def shutdown(self) -> None:
        """Cancel any pending attempt and stop reacting to transitions."""
        with self._lock:
            self._stopped = True
            self._cancel_pending_locked()
        if self._attached:
            self._feed_state.remove_listener(self._on_state_change)
            self._attached = False

    def _on_state_change(self, old: FeedState, new: FeedState) -> None:
        with self._lock:
            if self._stopped:
                return
            if new == FeedState.DISCONNECTED:
                self._schedule_next_attempt_locked()
            elif new == FeedState.LIVE:
                # Recovery — reset accounting so a future disconnect
                # gets a fresh backoff schedule, and clear the
                # `feed_failed_permanent` open marker so the 10.A
                # banner stops surfacing the prior failure.
                self._cancel_pending_locked()
                if self._failed_emitted:
                    default_log().clear_open_event(
                        category="feed_failed_permanent",
                        feed_id=self._feed_id,
                    )
                self._attempt_index = 0
                self._failed_emitted = False
            elif new == FeedState.DISABLED:
                # Operator stopped the feed; abandon scheduling.
                self._cancel_pending_locked()
            elif new == FeedState.FAILED:
                self._cancel_pending_locked()

    def _schedule_next_attempt_locked(self) -> None:
        if self._handle is not None:
            return
        if self._stopped:
            return
        if self._attempt_index >= len(self._backoff):
            self._mark_failed_locked()
            return
        delay = self._backoff[self._attempt_index]
        LOGGER.info(
            "reconnect supervisor feed=%s scheduling attempt %d in %.1fs",
            self._feed_id,
            self._attempt_index + 1,
            delay,
        )
        self._handle = self._scheduler.schedule(delay, self._fire_attempt)

    def _fire_attempt(self) -> None:
        with self._lock:
            self._handle = None
            if self._stopped:
                return
            if self._feed_state.state != FeedState.DISCONNECTED:
                # State changed under us between schedule and fire
                # (e.g. operator stopped the feed). Bail.
                return
            transitioned = self._feed_state.transition_to(FeedState.RECONNECTING)
            if not transitioned:
                # State machine rejected the move (shouldn't happen for
                # DISCONNECTED → RECONNECTING, but defensive).
                return
            self._attempt_index += 1
            attempt_n = self._attempt_index
        LOGGER.info(
            "reconnect supervisor feed=%s firing attempt %d",
            self._feed_id,
            attempt_n,
        )
        try:
            self._rebuild()
        except Exception:
            LOGGER.exception(
                "reconnect supervisor feed=%s rebuild raised", self._feed_id
            )
        # If the rebuild produced buffers synchronously, the feed-state
        # listener already fired RECONNECTING → LIVE and reset the
        # counter. If it didn't, drop back to DISCONNECTED so the
        # listener re-runs `_schedule_next_attempt_locked` for the next
        # backoff slot.
        with self._lock:
            if self._stopped:
                return
            current = self._feed_state.state
            if current == FeedState.LIVE:
                return
            if current == FeedState.RECONNECTING:
                self._feed_state.transition_to(FeedState.DISCONNECTED)

    def _mark_failed_locked(self) -> None:
        if self._failed_emitted:
            return
        self._failed_emitted = True
        record_health_event(
            severity=HealthSeverity.ERROR,
            category="feed_failed_permanent",
            message=(
                f"{self._display_name}: gave up after {self._attempt_index} "
                f"reconnect attempts"
            ),
            feed_id=self._feed_id,
            metadata={
                "attempts": self._attempt_index,
                "backoff_total_seconds": sum(self._backoff),
            },
        )
        self._feed_state.transition_to(FeedState.FAILED)

    def _cancel_pending_locked(self) -> None:
        if self._handle is not None:
            try:
                self._scheduler.cancel(self._handle)
            except Exception:
                LOGGER.exception(
                    "reconnect supervisor feed=%s cancel raised", self._feed_id
                )
            self._handle = None
