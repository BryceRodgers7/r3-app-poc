"""Unit tests for `app.core.feed_state`."""

from __future__ import annotations

import unittest

from app.core import health_events
from app.core.feed_state import FeedState, make_feed_state_machine
from app.core.health_events import HealthEventLog


class FeedStateTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Replace the process-wide log so we can inspect emissions.
        self._orig_log = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = HealthEventLog()
        self.log = health_events._DEFAULT_LOG

    def tearDown(self) -> None:
        health_events._DEFAULT_LOG = self._orig_log

    def test_default_initial_state_is_disabled(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        self.assertIs(sm.state, FeedState.DISABLED)

    def test_typical_startup_path(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        self.assertTrue(sm.transition_to(FeedState.CONNECTING))
        self.assertTrue(sm.transition_to(FeedState.LIVE))
        self.assertIs(sm.state, FeedState.LIVE)

    def test_disconnected_must_hop_through_reconnecting(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        sm.transition_to(FeedState.DISCONNECTED)
        # DISCONNECTED -> LIVE directly should be rejected.
        self.assertFalse(sm.transition_to(FeedState.LIVE))
        self.assertIs(sm.state, FeedState.DISCONNECTED)
        # RECONNECTING -> LIVE is legal.
        self.assertTrue(sm.transition_to(FeedState.RECONNECTING))
        self.assertTrue(sm.transition_to(FeedState.LIVE))

    def test_disconnect_emits_feed_lost_health_event(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        sm.transition_to(FeedState.DISCONNECTED)
        self.assertTrue(self.log.has_open_event(category="feed_lost", feed_id="cam_a"))

    def test_recovery_emits_feed_recovered_and_clears_marker(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        sm.transition_to(FeedState.DISCONNECTED)
        sm.transition_to(FeedState.RECONNECTING)
        sm.transition_to(FeedState.LIVE)
        self.assertFalse(self.log.has_open_event(category="feed_lost", feed_id="cam_a"))

    def test_degraded_emits_feed_degraded_event(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        sm.transition_to(FeedState.DEGRADED)
        self.assertTrue(self.log.has_open_event(category="feed_degraded", feed_id="cam_a"))

    def test_degraded_recovery_clears_marker_when_returning_to_live(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        sm.transition_to(FeedState.DEGRADED)
        sm.transition_to(FeedState.LIVE)
        self.assertFalse(self.log.has_open_event(category="feed_degraded", feed_id="cam_a"))

    def test_initial_connecting_to_live_does_not_emit_recovery(self) -> None:
        sm = make_feed_state_machine("cam_a", "Cam A")
        sm.transition_to(FeedState.CONNECTING)
        sm.transition_to(FeedState.LIVE)
        # CONNECTING -> LIVE is the normal startup path, not a recovery.
        self.assertFalse(self.log.has_open_event(category="feed_lost", feed_id="cam_a"))


if __name__ == "__main__":
    unittest.main()
