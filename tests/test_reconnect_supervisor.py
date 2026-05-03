"""Phase 10.B — per-feed reconnect supervisor.

Tests use a `FakeScheduler` so the backoff schedule fires on demand,
without real time passing. Rebuild calls go through a recording fake;
each test controls whether the fake declares LIVE during the call (by
flipping the feed-state machine itself) or leaves the supervisor to
fall back to DISCONNECTED for the next attempt.
"""

from __future__ import annotations

from collections.abc import Callable
import unittest

from app.core.feed_state import FeedState, make_feed_state_machine
from app.core.health_events import HealthEventLog, HealthSeverity
import app.core.health_events as health_events
import app.core.reconnect_supervisor as reconnect_supervisor
from app.core.reconnect_supervisor import ReconnectSupervisor


class FakeScheduler:
    """Deterministic stand-in for `threading.Timer`-backed scheduling."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, Callable[[], None]]] = []
        self.cancelled: list[tuple[float, Callable[[], None]]] = []

    def schedule(self, delay: float, callback: Callable[[], None]):
        handle = (delay, callback)
        self.scheduled.append(handle)
        return handle

    def cancel(self, handle) -> None:
        try:
            self.scheduled.remove(handle)
        except ValueError:
            pass
        self.cancelled.append(handle)

    # Test-only helpers
    def fire_next(self) -> tuple[float, Callable[[], None]]:
        handle = self.scheduled.pop(0)
        handle[1]()
        return handle

    @property
    def pending_count(self) -> int:
        return len(self.scheduled)

    @property
    def next_delay(self) -> float:
        return self.scheduled[0][0]


class _TestLogPatch:
    """Swap the process-wide default health log for one isolated to a test."""

    def __init__(self) -> None:
        self.log = HealthEventLog()
        self._saved = None

    def __enter__(self) -> HealthEventLog:
        self._saved = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = self.log
        return self.log

    def __exit__(self, *exc) -> None:
        health_events._DEFAULT_LOG = self._saved


def _build_supervisor(
    *,
    rebuild_callable: Callable[[], bool],
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
):
    """Construct a feed-state machine + supervisor + fake scheduler."""
    feed_state = make_feed_state_machine("cam_a", "Cam A")
    feed_state.transition_to(FeedState.CONNECTING)
    feed_state.transition_to(FeedState.LIVE)
    scheduler = FakeScheduler()
    supervisor = ReconnectSupervisor(
        feed_id="cam_a",
        display_name="Cam A",
        feed_state=feed_state,
        rebuild_callable=rebuild_callable,
        backoff_seconds=backoff,
        scheduler=scheduler,
    )
    supervisor.attach()
    return feed_state, supervisor, scheduler


class BackoffSchedulingTests(unittest.TestCase):
    def test_disconnect_schedules_first_attempt(self) -> None:
        rebuild_calls: list[None] = []
        feed_state, supervisor, scheduler = _build_supervisor(
            rebuild_callable=lambda: rebuild_calls.append(None) or True,
        )
        feed_state.transition_to(FeedState.DISCONNECTED)
        self.assertEqual(scheduler.pending_count, 1)
        self.assertEqual(scheduler.next_delay, 1.0)
        self.assertEqual(rebuild_calls, [])

    def test_attempt_runs_rebuild_and_transitions_through_reconnecting(self) -> None:
        rebuild_calls: list[None] = []
        feed_state, supervisor, scheduler = _build_supervisor(
            # Simulate a rebuild that doesn't yet produce buffers — the
            # state stays in RECONNECTING when rebuild returns, so the
            # supervisor falls back to DISCONNECTED.
            rebuild_callable=lambda: (rebuild_calls.append(None) or True),
        )
        feed_state.transition_to(FeedState.DISCONNECTED)
        scheduler.fire_next()
        self.assertEqual(len(rebuild_calls), 1)
        # Supervisor dropped back to DISCONNECTED and queued the next
        # attempt at the second backoff slot.
        self.assertEqual(feed_state.state, FeedState.DISCONNECTED)
        self.assertEqual(scheduler.pending_count, 1)
        self.assertEqual(scheduler.next_delay, 2.0)

    def test_each_attempt_advances_backoff(self) -> None:
        feed_state, supervisor, scheduler = _build_supervisor(
            rebuild_callable=lambda: True,
            backoff=(1.0, 2.0, 4.0, 8.0),
        )
        feed_state.transition_to(FeedState.DISCONNECTED)
        observed_delays = []
        # Walk through the schedule until the supervisor either succeeds
        # or hits the cap.
        for _ in range(4):
            observed_delays.append(scheduler.next_delay)
            scheduler.fire_next()
        self.assertEqual(observed_delays, [1.0, 2.0, 4.0, 8.0])


class CapBehaviorTests(unittest.TestCase):
    def test_cap_transitions_feed_to_failed(self) -> None:
        with _TestLogPatch() as log:
            feed_state, supervisor, scheduler = _build_supervisor(
                rebuild_callable=lambda: True,
                backoff=(1.0, 2.0),
            )
            feed_state.transition_to(FeedState.DISCONNECTED)
            scheduler.fire_next()  # attempt 1
            scheduler.fire_next()  # attempt 2 -> exhausts schedule
            self.assertEqual(feed_state.state, FeedState.FAILED)
            self.assertTrue(
                log.has_open_event(category="feed_failed_permanent", feed_id="cam_a")
            )

    def test_capped_supervisor_does_not_reschedule_until_recovery(self) -> None:
        # Once the cap is hit, the supervisor stays capped. Subsequent
        # state-machine motion that returns to DISCONNECTED (e.g. a
        # transient FAILED -> RECONNECTING -> DISCONNECTED probe) must
        # not re-fire the failure event or reschedule attempts. A real
        # operator-initiated retry tears down `FeedRuntime` and builds
        # a fresh supervisor — that path is exercised in feed_runtime
        # tests.
        with _TestLogPatch() as log:
            feed_state, supervisor, scheduler = _build_supervisor(
                rebuild_callable=lambda: True,
                backoff=(1.0,),
            )
            feed_state.transition_to(FeedState.DISCONNECTED)
            scheduler.fire_next()  # exhausts schedule -> FAILED
            self.assertEqual(feed_state.state, FeedState.FAILED)
            self.assertEqual(log.category_count("feed_failed_permanent"), 1)
            feed_state.transition_to(FeedState.RECONNECTING)
            feed_state.transition_to(FeedState.DISCONNECTED)
            self.assertEqual(scheduler.pending_count, 0)
            self.assertEqual(log.category_count("feed_failed_permanent"), 1)


class SuccessPathTests(unittest.TestCase):
    def test_buffer_arrival_during_rebuild_resets_counter(self) -> None:
        # Rebuild that simulates buffers arriving: while we're inside
        # the rebuild call, drive RECONNECTING -> LIVE the same way
        # `_promote_feed_state_on_arrival` would.
        feed_state = make_feed_state_machine("cam_a", "Cam A")
        feed_state.transition_to(FeedState.CONNECTING)
        feed_state.transition_to(FeedState.LIVE)
        scheduler = FakeScheduler()

        def rebuild_with_live():
            # The supervisor has already moved us to RECONNECTING.
            self.assertEqual(feed_state.state, FeedState.RECONNECTING)
            feed_state.transition_to(FeedState.LIVE)
            return True

        supervisor = ReconnectSupervisor(
            feed_id="cam_a",
            display_name="Cam A",
            feed_state=feed_state,
            rebuild_callable=rebuild_with_live,
            backoff_seconds=(1.0, 2.0),
            scheduler=scheduler,
        )
        supervisor.attach()
        feed_state.transition_to(FeedState.DISCONNECTED)
        scheduler.fire_next()
        self.assertEqual(feed_state.state, FeedState.LIVE)
        self.assertEqual(scheduler.pending_count, 0)
        self.assertEqual(supervisor.attempt_index, 0)

    def test_recovery_clears_failed_permanent_marker(self) -> None:
        with _TestLogPatch() as log:
            feed_state, supervisor, scheduler = _build_supervisor(
                rebuild_callable=lambda: True,
                backoff=(1.0,),
            )
            feed_state.transition_to(FeedState.DISCONNECTED)
            scheduler.fire_next()  # cap reached -> FAILED + event
            self.assertTrue(
                log.has_open_event(category="feed_failed_permanent", feed_id="cam_a")
            )
            # Operator-initiated retry: FAILED -> RECONNECTING -> LIVE.
            feed_state.transition_to(FeedState.RECONNECTING)
            feed_state.transition_to(FeedState.LIVE)
            self.assertFalse(
                log.has_open_event(category="feed_failed_permanent", feed_id="cam_a")
            )

    def test_disable_cancels_pending_attempts(self) -> None:
        feed_state, supervisor, scheduler = _build_supervisor(
            rebuild_callable=lambda: True,
        )
        feed_state.transition_to(FeedState.DISCONNECTED)
        self.assertEqual(scheduler.pending_count, 1)
        feed_state.transition_to(FeedState.DISABLED)
        self.assertEqual(scheduler.pending_count, 0)


class ShutdownTests(unittest.TestCase):
    def test_shutdown_cancels_pending(self) -> None:
        feed_state, supervisor, scheduler = _build_supervisor(
            rebuild_callable=lambda: True,
        )
        feed_state.transition_to(FeedState.DISCONNECTED)
        self.assertEqual(scheduler.pending_count, 1)
        supervisor.shutdown()
        self.assertEqual(scheduler.pending_count, 0)

    def test_shutdown_detaches_listener(self) -> None:
        rebuild_calls: list[None] = []
        feed_state, supervisor, scheduler = _build_supervisor(
            rebuild_callable=lambda: rebuild_calls.append(None) or True,
        )
        supervisor.shutdown()
        feed_state.transition_to(FeedState.DEGRADED)
        feed_state.transition_to(FeedState.DISCONNECTED)
        self.assertEqual(scheduler.pending_count, 0)
        self.assertEqual(rebuild_calls, [])

    def test_attach_is_idempotent(self) -> None:
        # Calling attach twice should not double-fire on transitions.
        feed_state = make_feed_state_machine("cam_a", "Cam A")
        feed_state.transition_to(FeedState.CONNECTING)
        feed_state.transition_to(FeedState.LIVE)
        scheduler = FakeScheduler()
        supervisor = ReconnectSupervisor(
            feed_id="cam_a",
            display_name="Cam A",
            feed_state=feed_state,
            rebuild_callable=lambda: True,
            backoff_seconds=(1.0,),
            scheduler=scheduler,
        )
        supervisor.attach()
        supervisor.attach()
        feed_state.transition_to(FeedState.DISCONNECTED)
        # Single schedule despite double-attach.
        self.assertEqual(scheduler.pending_count, 1)


class RebuildExceptionTests(unittest.TestCase):
    def test_rebuild_exception_falls_back_to_disconnected(self) -> None:
        def boom():
            raise RuntimeError("simulated rebuild crash")

        feed_state, supervisor, scheduler = _build_supervisor(
            rebuild_callable=boom,
            backoff=(1.0, 2.0),
        )
        feed_state.transition_to(FeedState.DISCONNECTED)
        scheduler.fire_next()
        # State should be back at DISCONNECTED, next attempt scheduled.
        self.assertEqual(feed_state.state, FeedState.DISCONNECTED)
        self.assertEqual(scheduler.pending_count, 1)
        self.assertEqual(scheduler.next_delay, 2.0)


if __name__ == "__main__":
    unittest.main()
