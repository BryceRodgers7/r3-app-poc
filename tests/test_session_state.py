"""Unit tests for `app.core.session_state`."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core import health_events
from app.core.health_events import HealthEventLog
from app.core.session_state import (
    SESSION_MANIFEST_FILENAME,
    SessionManifest,
    SessionState,
    make_session_state_machine,
)


class SessionStateTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_log = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = HealthEventLog()
        self.log = health_events._DEFAULT_LOG

    def tearDown(self) -> None:
        health_events._DEFAULT_LOG = self._orig_log

    def test_initial_state_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            payload = manifest.read()
            self.assertIsNotNone(payload)
            self.assertEqual(payload["state"], "created")
            self.assertEqual(payload["session_id"], "s1")
            self.assertIsNone(payload["finalized_at"])

    def test_recording_to_finalized_path(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            sm.transition_to(SessionState.RECORDING)
            sm.transition_to(SessionState.STOPPED)
            sm.transition_to(SessionState.FINALIZED)
            payload = manifest.read()
            self.assertEqual(payload["state"], "finalized")
            self.assertIsNotNone(payload["finalized_at"])

    def test_dirty_marker_set_when_transitioning_to_dirty(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            sm.transition_to(SessionState.RECORDING)
            sm.transition_to(SessionState.DIRTY)
            payload = manifest.read()
            self.assertEqual(payload["state"], "dirty")
            self.assertIsNone(payload["finalized_at"])
            self.assertTrue(self.log.has_open_event(category="session_dirty", feed_id=None))

    def test_finalized_clears_dirty_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            sm.transition_to(SessionState.RECORDING)
            sm.transition_to(SessionState.DIRTY)
            sm.transition_to(SessionState.FINALIZED)
            self.assertFalse(self.log.has_open_event(category="session_dirty", feed_id=None))
            self.assertTrue(
                self.log.has_open_event(category="session_finalized", feed_id=None)
            )

    def test_archived_is_terminal(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            sm.transition_to(SessionState.RECORDING)
            sm.transition_to(SessionState.STOPPED)
            sm.transition_to(SessionState.FINALIZED)
            sm.transition_to(SessionState.ARCHIVED)
            self.assertFalse(sm.transition_to(SessionState.RECORDING))
            self.assertFalse(sm.transition_to(SessionState.CREATED))

    def test_cannot_go_recording_after_finalized(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            sm.transition_to(SessionState.FINALIZED)
            self.assertFalse(sm.transition_to(SessionState.RECORDING))

    def test_stopped_to_recording_for_multi_game_session(self) -> None:
        # Multi-game-per-session flow: after game 1's Stop the session
        # is in STOPPED; the operator's next Start must flip the
        # manifest back to RECORDING so external tooling reading
        # session.json sees an accurate "in progress" marker.
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            sm = make_session_state_machine(session_id="s1", manifest=manifest)
            sm.transition_to(SessionState.RECORDING)
            sm.transition_to(SessionState.STOPPED)
            self.assertTrue(sm.transition_to(SessionState.RECORDING))
            self.assertEqual(sm.state, SessionState.RECORDING)
            payload = manifest.read()
            self.assertEqual(payload["state"], "recording")
            self.assertIsNone(payload["finalized_at"])


class SessionManifestTests(unittest.TestCase):
    def test_atomic_write_via_tmp_file(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / SESSION_MANIFEST_FILENAME)
            manifest.write(
                session_id="s1",
                state=SessionState.RECORDING,
                created_at="2026-04-26T18:00:00+00:00",
                finalized_at=None,
            )
            text = (Path(tmp) / SESSION_MANIFEST_FILENAME).read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertEqual(payload["state"], "recording")
            # No leftover .tmp file.
            self.assertFalse((Path(tmp) / (SESSION_MANIFEST_FILENAME + ".tmp")).exists())

    def test_read_returns_none_for_missing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            manifest = SessionManifest(Path(tmp) / "nonexistent.json")
            self.assertIsNone(manifest.read())

    def test_read_returns_none_for_corrupt_json(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / SESSION_MANIFEST_FILENAME
            path.write_text("{not json", encoding="utf-8")
            manifest = SessionManifest(path)
            self.assertIsNone(manifest.read())


if __name__ == "__main__":
    unittest.main()
