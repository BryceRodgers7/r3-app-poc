"""Unit tests for `app.core.state_machine`."""

from __future__ import annotations

from enum import Enum
import unittest

from app.core.health_events import HealthEventLog
from app.core.state_machine import StateMachine


class _Mode(Enum):
    OFF = "off"
    LOADING = "loading"
    READY = "ready"
    BROKEN = "broken"


_TRANSITIONS = {
    _Mode.OFF: {_Mode.LOADING},
    _Mode.LOADING: {_Mode.READY, _Mode.BROKEN, _Mode.OFF},
    _Mode.READY: {_Mode.BROKEN, _Mode.OFF},
    _Mode.BROKEN: {_Mode.LOADING, _Mode.OFF},
}


class StateMachineTests(unittest.TestCase):
    def test_initial_state_exposed(self) -> None:
        sm = StateMachine[_Mode](
            initial=_Mode.OFF, transitions=_TRANSITIONS, name="m"
        )
        self.assertIs(sm.state, _Mode.OFF)

    def test_legal_transition_changes_state_and_returns_true(self) -> None:
        sm = StateMachine[_Mode](
            initial=_Mode.OFF, transitions=_TRANSITIONS, name="m"
        )
        self.assertTrue(sm.transition_to(_Mode.LOADING))
        self.assertIs(sm.state, _Mode.LOADING)

    def test_illegal_transition_is_rejected_and_records_health_event(self) -> None:
        sm = StateMachine[_Mode](
            initial=_Mode.OFF, transitions=_TRANSITIONS, name="m"
        )
        # Capture the health event recorded by the rejection path. The state
        # machine writes through the module-level default log.
        from app.core import health_events
        health_events._DEFAULT_LOG = HealthEventLog()  # type: ignore[attr-defined]
        try:
            self.assertFalse(sm.transition_to(_Mode.READY))
            self.assertIs(sm.state, _Mode.OFF)
            self.assertTrue(
                health_events._DEFAULT_LOG.has_open_event(  # type: ignore[attr-defined]
                    category="invalid_transition", feed_id=None
                )
            )
        finally:
            health_events._DEFAULT_LOG = HealthEventLog()  # type: ignore[attr-defined]

    def test_same_state_request_returns_false_without_firing_on_enter(self) -> None:
        events: list[tuple[_Mode, _Mode]] = []
        sm = StateMachine[_Mode](
            initial=_Mode.OFF,
            transitions=_TRANSITIONS,
            name="m",
            on_enter=lambda old, new: events.append((old, new)),
        )
        self.assertFalse(sm.transition_to(_Mode.OFF))
        self.assertEqual(events, [])

    def test_on_enter_fires_after_legal_transition(self) -> None:
        events: list[tuple[_Mode, _Mode]] = []
        sm = StateMachine[_Mode](
            initial=_Mode.OFF,
            transitions=_TRANSITIONS,
            name="m",
            on_enter=lambda old, new: events.append((old, new)),
        )
        sm.transition_to(_Mode.LOADING)
        sm.transition_to(_Mode.READY)
        self.assertEqual(events, [(_Mode.OFF, _Mode.LOADING), (_Mode.LOADING, _Mode.READY)])

    def test_can_transition_reflects_table(self) -> None:
        sm = StateMachine[_Mode](
            initial=_Mode.OFF, transitions=_TRANSITIONS, name="m"
        )
        self.assertTrue(sm.can_transition(_Mode.LOADING))
        self.assertFalse(sm.can_transition(_Mode.READY))

    def test_force_bypasses_validation(self) -> None:
        sm = StateMachine[_Mode](
            initial=_Mode.OFF, transitions=_TRANSITIONS, name="m"
        )
        sm.force(_Mode.READY)
        self.assertIs(sm.state, _Mode.READY)


if __name__ == "__main__":
    unittest.main()
