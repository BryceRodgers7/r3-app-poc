"""Top-level application state (§10.1) aggregated from sub-state machines.

`AppState` is computed on demand from `FeedState` (per feed),
`RecordingState`, and `ReplayState` rather than held as its own state
machine. Treating it as a derived view keeps the four authoritative
machines as the single sources of truth and avoids the consistency
problems of a fifth machine that has to track them.

Precedence (highest first), documented so readers do not have to puzzle
over the conditional cascade:

    1. SHUTTING_DOWN  — coordinator has begun teardown
    2. ERROR          — `RecordingState.RECORDING_ERROR`
    3. REPLAYING / PAUSED / SLOW_MOTION — operator's active replay sub-state
    4. DEGRADED       — any feed degraded/disconnected/failed, or
                        `ReplayState.REPLAY_DEGRADED`
    5. RECORDING      — `RecordingState.RECORDING` with no operator replay
    6. PREVIEWING     — at least one feed live, no recording
    7. STARTING       — feeds present but still connecting
    8. IDLE           — no feeds active

DEGRADED is intentionally lower precedence than the operator's active
replay states so the operator's last user-action context is not buried
under a side indicator. The diagnostics widget surfaces the underlying
sub-states regardless, so the operator can still see what is degraded.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from app.core.feed_state import FeedState
from app.core.recording_state import RecordingState
from app.core.replay_state import ReplayState


class AppState(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    PREVIEWING = "previewing"
    RECORDING = "recording"
    REPLAYING = "replaying"
    PAUSED = "paused"
    SLOW_MOTION = "slow_motion"
    DEGRADED = "degraded"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


_DEGRADED_FEED_STATES = frozenset(
    {FeedState.DEGRADED, FeedState.DISCONNECTED, FeedState.FAILED, FeedState.RECONNECTING}
)


def compute_app_state(
    *,
    feed_states: Iterable[FeedState],
    recording_state: RecordingState,
    replay_state: ReplayState | None,
    shutting_down: bool = False,
) -> AppState:
    """Aggregate the four sub-states into the top-level `AppState`."""
    if shutting_down:
        return AppState.SHUTTING_DOWN

    if recording_state == RecordingState.RECORDING_ERROR:
        return AppState.ERROR

    if replay_state == ReplayState.REPLAYING:
        return AppState.REPLAYING
    if replay_state == ReplayState.PAUSED:
        return AppState.PAUSED
    if replay_state == ReplayState.SLOW_MOTION:
        return AppState.SLOW_MOTION

    feed_states_list = list(feed_states)
    any_degraded = any(s in _DEGRADED_FEED_STATES for s in feed_states_list)
    if any_degraded or replay_state == ReplayState.REPLAY_DEGRADED:
        return AppState.DEGRADED

    if recording_state == RecordingState.RECORDING:
        return AppState.RECORDING

    if any(s == FeedState.LIVE for s in feed_states_list):
        return AppState.PREVIEWING

    if any(s in {FeedState.CONNECTING, FeedState.RECONNECTING} for s in feed_states_list):
        return AppState.STARTING

    if not feed_states_list:
        return AppState.STARTING

    return AppState.IDLE
