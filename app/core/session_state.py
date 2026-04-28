"""Session-level lifecycle state with on-disk persistence (§10.6).

The state is persisted to `<session>/session.json` on every transition. The
absence of `finalized_at` in that file is the marker §11.4's recovery flow
will use to detect `DIRTY` on next launch — but the recovery prompt UI itself
is not part of slice 2.B. This slice only ensures the data is durable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import threading

from app.core.health_events import HealthSeverity, default_log, record_health_event
from app.core.state_machine import StateMachine

LOGGER = logging.getLogger(__name__)

SESSION_MANIFEST_FILENAME = "session.json"


class SessionState(str, Enum):
    CREATED = "created"
    RECORDING = "recording"
    STOPPED = "stopped"
    FINALIZED = "finalized"
    DIRTY = "dirty"
    ARCHIVED = "archived"


_SESSION_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.CREATED: {
        SessionState.RECORDING,
        SessionState.FINALIZED,
        SessionState.DIRTY,
    },
    SessionState.RECORDING: {
        SessionState.STOPPED,
        SessionState.DIRTY,
    },
    SessionState.STOPPED: {
        SessionState.FINALIZED,
        SessionState.DIRTY,
    },
    SessionState.FINALIZED: {
        SessionState.ARCHIVED,
    },
    SessionState.DIRTY: {
        SessionState.FINALIZED,
        SessionState.CREATED,
    },
    SessionState.ARCHIVED: set(),
}


class SessionManifest:
    """Read/write the per-session `session.json` file."""

    def __init__(self, manifest_path: Path) -> None:
        self._path = Path(manifest_path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        *,
        session_id: str,
        state: SessionState,
        created_at: str,
        finalized_at: str | None,
    ) -> None:
        payload = {
            "session_id": session_id,
            "state": state.value,
            "created_at": created_at,
            "finalized_at": finalized_at,
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def read(self) -> dict | None:
        try:
            with self._lock:
                text = self._path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("session manifest at %s is not valid JSON", self._path)
            return None


def make_session_state_machine(
    *,
    session_id: str,
    manifest: SessionManifest,
    created_at: str | None = None,
    initial_state: SessionState = SessionState.CREATED,
) -> StateMachine[SessionState]:
    """Build a `StateMachine[SessionState]` that persists on every transition.

    `initial_state` defaults to `CREATED` (the brand-new-session path).
    The §11.4 Resume action passes `DIRTY` so the machine starts at the
    on-disk state and the operator can drive `DIRTY → CREATED → RECORDING`
    through the normal transition table.
    """

    state_created_at = created_at or datetime.now(timezone.utc).isoformat()
    finalized_holder: dict[str, str | None] = {"value": None}

    def persist(state: SessionState) -> None:
        if state in {SessionState.FINALIZED, SessionState.ARCHIVED}:
            if finalized_holder["value"] is None:
                finalized_holder["value"] = datetime.now(timezone.utc).isoformat()
        manifest.write(
            session_id=session_id,
            state=state,
            created_at=state_created_at,
            finalized_at=finalized_holder["value"],
        )

    def on_enter(old: SessionState, new: SessionState) -> None:
        persist(new)
        log = default_log()
        if new == SessionState.DIRTY:
            if not log.has_open_event(category="session_dirty", feed_id=None):
                record_health_event(
                    severity=HealthSeverity.WARNING,
                    category="session_dirty",
                    message=f"Session {session_id} entered DIRTY from {old.name}.",
                    metadata={"session_id": session_id, "from": old.name},
                )
        elif new == SessionState.FINALIZED:
            log.clear_open_event(category="session_dirty", feed_id=None)
            record_health_event(
                severity=HealthSeverity.INFO,
                category="session_finalized",
                message=f"Session {session_id} finalized.",
                metadata={"session_id": session_id},
            )

    sm = StateMachine[SessionState](
        initial=initial_state,
        transitions=_SESSION_TRANSITIONS,
        name=f"session_state[{session_id}]",
        on_enter=on_enter,
    )
    # Persist the initial state immediately so a crash before the first
    # transition still leaves a manifest behind.
    persist(initial_state)
    return sm
