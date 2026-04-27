"""Per-feed connection lifecycle (§10.2).

`FeedState` is the explicit replacement for the scattered
`is_connected`/`is_started` booleans on `FeedRuntime`. Producers (pipeline
bus events, telemetry zero-fps streak, source factory) drive transitions;
consumers (UI, telemetry roll-ups, post-session metadata) read `state`.

The state machine emits `feed_lost` / `feed_recovered` health events from
its `on_enter` hook, so any code path that drives a transition automatically
keeps the health log consistent without duplicating emit calls.
"""

from __future__ import annotations

from enum import Enum

from app.core.health_events import HealthSeverity, default_log, record_health_event
from app.core.state_machine import StateMachine


class FeedState(str, Enum):
    DISABLED = "disabled"
    CONNECTING = "connecting"
    LIVE = "live"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


_FEED_TRANSITIONS: dict[FeedState, set[FeedState]] = {
    FeedState.DISABLED: {FeedState.CONNECTING},
    FeedState.CONNECTING: {
        FeedState.LIVE,
        FeedState.FAILED,
        FeedState.DISCONNECTED,
        FeedState.DISABLED,
    },
    FeedState.LIVE: {
        FeedState.DEGRADED,
        FeedState.DISCONNECTED,
        FeedState.DISABLED,
    },
    FeedState.DEGRADED: {
        FeedState.LIVE,
        FeedState.DISCONNECTED,
        FeedState.DISABLED,
    },
    FeedState.DISCONNECTED: {
        FeedState.RECONNECTING,
        FeedState.FAILED,
        FeedState.DISABLED,
    },
    FeedState.RECONNECTING: {
        FeedState.LIVE,
        FeedState.FAILED,
        FeedState.DISCONNECTED,
        FeedState.DISABLED,
    },
    FeedState.FAILED: {
        FeedState.RECONNECTING,
        FeedState.DISABLED,
    },
}


_BAD_STATES = {
    FeedState.DISCONNECTED,
    FeedState.FAILED,
    FeedState.DEGRADED,
    FeedState.RECONNECTING,
}


def make_feed_state_machine(feed_id: str, display_name: str) -> StateMachine[FeedState]:
    """Build a `StateMachine[FeedState]` with health-event hooks for one feed."""

    def on_enter(old: FeedState, new: FeedState) -> None:
        log = default_log()
        if new in {FeedState.DISCONNECTED, FeedState.FAILED}:
            if not log.has_open_event(category="feed_lost", feed_id=feed_id):
                record_health_event(
                    severity=HealthSeverity.WARNING,
                    category="feed_lost",
                    message=f"{display_name} state {old.name} -> {new.name}",
                    feed_id=feed_id,
                    metadata={"from": old.name, "to": new.name},
                )
            return
        if new == FeedState.DEGRADED and not log.has_open_event(
            category="feed_degraded", feed_id=feed_id
        ):
            record_health_event(
                severity=HealthSeverity.WARNING,
                category="feed_degraded",
                message=f"{display_name} state {old.name} -> DEGRADED",
                feed_id=feed_id,
                metadata={"from": old.name, "to": new.name},
            )
            return
        if new == FeedState.LIVE and old in _BAD_STATES:
            if log.has_open_event(category="feed_lost", feed_id=feed_id):
                record_health_event(
                    severity=HealthSeverity.INFO,
                    category="feed_recovered",
                    message=f"{display_name} state {old.name} -> LIVE",
                    feed_id=feed_id,
                )
                log.clear_open_event(category="feed_lost", feed_id=feed_id)
            if log.has_open_event(category="feed_degraded", feed_id=feed_id):
                log.clear_open_event(category="feed_degraded", feed_id=feed_id)

    return StateMachine[FeedState](
        initial=FeedState.DISABLED,
        transitions=_FEED_TRANSITIONS,
        name=f"feed_state[{feed_id}]",
        on_enter=on_enter,
    )
