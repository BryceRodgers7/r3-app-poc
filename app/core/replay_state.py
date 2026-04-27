"""Operator replay lifecycle state (§10.4).

`ReplayState` is the operator-side replacement for the free-form
`PlaybackMode` checks scattered across `PlaybackController`. Producers are
the controller's transport methods (pause / rewind / set_rate / jump_to_live)
and a recording-state observer that snaps replay back to live when long
recording stops.

Behavior change introduced by this slice (per §10.4 / §15.2):
*replay actions are rejected when `recording_state != RECORDING`.* The
rolling replay buffer is still active before and after long recording, but
the operator UI now reflects the doc's rule that replay is bound to the
active recording.
"""

from __future__ import annotations

from enum import Enum

from app.core.health_events import HealthSeverity, default_log, record_health_event
from app.core.state_machine import StateMachine

ACTIVE_REPLAY_STATES: frozenset["ReplayState"] = frozenset()  # populated below


class ReplayState(str, Enum):
    REPLAY_UNAVAILABLE_NOT_RECORDING = "replay_unavailable_not_recording"
    LIVE_WHILE_RECORDING = "live_while_recording"
    SEEKING = "seeking"
    REPLAYING = "replaying"
    PAUSED = "paused"
    SLOW_MOTION = "slow_motion"
    JUMPING_TO_LIVE = "jumping_to_live"
    REPLAY_DEGRADED = "replay_degraded"


# Build it once the enum exists.
ACTIVE_REPLAY_STATES = frozenset(
    {
        ReplayState.SEEKING,
        ReplayState.REPLAYING,
        ReplayState.PAUSED,
        ReplayState.SLOW_MOTION,
        ReplayState.JUMPING_TO_LIVE,
    }
)


_REPLAY_TRANSITIONS: dict[ReplayState, set[ReplayState]] = {
    ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING: {
        ReplayState.LIVE_WHILE_RECORDING,
    },
    ReplayState.LIVE_WHILE_RECORDING: {
        ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        ReplayState.SEEKING,
        ReplayState.PAUSED,
        ReplayState.REPLAY_DEGRADED,
    },
    ReplayState.SEEKING: {
        ReplayState.REPLAYING,
        ReplayState.LIVE_WHILE_RECORDING,
        ReplayState.JUMPING_TO_LIVE,
        ReplayState.PAUSED,
    },
    ReplayState.REPLAYING: {
        ReplayState.SLOW_MOTION,
        ReplayState.PAUSED,
        ReplayState.JUMPING_TO_LIVE,
        ReplayState.SEEKING,
        ReplayState.REPLAY_DEGRADED,
    },
    ReplayState.SLOW_MOTION: {
        ReplayState.REPLAYING,
        ReplayState.PAUSED,
        ReplayState.JUMPING_TO_LIVE,
        ReplayState.SEEKING,
        ReplayState.REPLAY_DEGRADED,
    },
    ReplayState.PAUSED: {
        ReplayState.REPLAYING,
        ReplayState.SLOW_MOTION,
        ReplayState.JUMPING_TO_LIVE,
        ReplayState.SEEKING,
    },
    ReplayState.JUMPING_TO_LIVE: {
        ReplayState.LIVE_WHILE_RECORDING,
        ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
    },
    ReplayState.REPLAY_DEGRADED: {
        ReplayState.LIVE_WHILE_RECORDING,
        ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
    },
}


def make_replay_state_machine(*, role: str = "operator") -> StateMachine[ReplayState]:
    """Build a `StateMachine[ReplayState]` for one operator output.

    `role` is folded into the machine's name so the invalid-transition
    health events distinguish operator vs (future) other replay surfaces.
    """

    def on_enter(old: ReplayState, new: ReplayState) -> None:
        log = default_log()
        if new == ReplayState.REPLAY_DEGRADED and not log.has_open_event(
            category="replay_degraded", feed_id=None
        ):
            record_health_event(
                severity=HealthSeverity.WARNING,
                category="replay_degraded",
                message=f"Replay degraded ({old.name} -> REPLAY_DEGRADED).",
            )
        elif new in {
            ReplayState.LIVE_WHILE_RECORDING,
            ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        } and old == ReplayState.REPLAY_DEGRADED:
            log.clear_open_event(category="replay_degraded", feed_id=None)

    return StateMachine[ReplayState](
        initial=ReplayState.REPLAY_UNAVAILABLE_NOT_RECORDING,
        transitions=_REPLAY_TRANSITIONS,
        name=f"replay_state[{role}]",
        on_enter=on_enter,
    )
