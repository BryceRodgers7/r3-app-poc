"""Generic state-machine helper used by Phase 2 state models.

Each instance owns one current state plus a static transitions table and
rejects moves that are not in the table. Rejected transitions emit a
`health_event(category="invalid_transition")` so they show up in the
operator's diagnostics rather than being silently swallowed.

The helper is deliberately small — Phase 2 needs four of these
(`FeedState`, `RecordingState`, `SessionState`, `ReplayState`) and they all
share the same shape. Threading model: a single internal lock around the
state read/write; `on_enter` hooks fire outside the lock so they cannot
re-enter the machine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
import threading
from typing import Generic, TypeVar

from app.core.health_events import HealthSeverity, record_health_event

E = TypeVar("E", bound=Enum)


class StateMachine(Generic[E]):
    """Validate transitions of one named state variable."""

    def __init__(
        self,
        *,
        initial: E,
        transitions: Mapping[E, set[E] | frozenset[E]],
        name: str = "state_machine",
        on_enter: Callable[[E, E], None] | None = None,
    ) -> None:
        self._current: E = initial
        self._transitions: dict[E, frozenset[E]] = {
            state: frozenset(targets) for state, targets in transitions.items()
        }
        self._name = name
        self._on_enter = on_enter
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> E:
        with self._lock:
            return self._current

    def can_transition(self, target: E) -> bool:
        with self._lock:
            return target in self._transitions.get(self._current, frozenset())

    def transition_to(self, target: E) -> bool:
        """Move to `target` if legal; return True if the state changed.

        Same-state requests return False without firing `on_enter`. Illegal
        requests record an `invalid_transition` health event and return
        False.
        """
        with self._lock:
            old = self._current
            if target == old:
                return False
            allowed = self._transitions.get(old, frozenset())
            if target not in allowed:
                record_health_event(
                    severity=HealthSeverity.WARNING,
                    category="invalid_transition",
                    message=(
                        f"{self._name}: rejected {old.name} -> {target.name}"
                    ),
                    metadata={
                        "machine": self._name,
                        "from": old.name,
                        "to": target.name,
                    },
                )
                return False
            self._current = target
        if self._on_enter is not None:
            self._on_enter(old, target)
        return True

    def force(self, target: E) -> None:
        """Set state without transition validation. For initialization only."""
        with self._lock:
            self._current = target
