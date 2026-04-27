"""Unit tests for `app.core.health_events`."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core.health_events import HealthEventLog, HealthSeverity


class HealthEventLogTests(unittest.TestCase):
    def test_record_without_open_still_logs_in_memory_state(self) -> None:
        log = HealthEventLog()
        event = log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="No frames",
            feed_id="cam_a",
        )
        self.assertEqual(event.id, 1)
        self.assertIsNone(event.session_id)
        self.assertTrue(log.has_open_event(category="feed_lost", feed_id="cam_a"))

    def test_record_with_open_path_writes_jsonl_line(self) -> None:
        log = HealthEventLog()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "health_events.jsonl"
            log.open(path, session_id="session_001")
            log.record(
                severity=HealthSeverity.ERROR,
                category="feed_lost",
                message="cam_a went dark",
                feed_id="cam_a",
                metadata={"streak": 3},
            )
            log.close()

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["severity"], "error")
            self.assertEqual(payload["category"], "feed_lost")
            self.assertEqual(payload["session_id"], "session_001")
            self.assertEqual(payload["feed_id"], "cam_a")
            self.assertEqual(payload["metadata"], {"streak": 3})

    def test_record_appends_across_open_calls(self) -> None:
        log = HealthEventLog()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "health_events.jsonl"
            log.open(path, session_id="session_001")
            log.record(severity="warning", category="x", message="first")
            log.close()
            log.open(path, session_id="session_001")
            log.record(severity="warning", category="x", message="second")
            log.close()
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_open_clears_open_category_markers(self) -> None:
        log = HealthEventLog()
        with TemporaryDirectory() as tmp:
            log.open(Path(tmp) / "h.jsonl", session_id="s1")
            log.record(severity="warning", category="feed_lost", message="x", feed_id="cam_a")
            self.assertTrue(log.has_open_event(category="feed_lost", feed_id="cam_a"))
            log.close()
            log.open(Path(tmp) / "h.jsonl", session_id="s2")
            self.assertFalse(log.has_open_event(category="feed_lost", feed_id="cam_a"))
            log.close()

    def test_clear_open_event_allows_re_emission(self) -> None:
        log = HealthEventLog()
        log.record(severity="warning", category="disk_low", message="low")
        self.assertTrue(log.has_open_event(category="disk_low", feed_id=None))
        log.clear_open_event(category="disk_low", feed_id=None)
        self.assertFalse(log.has_open_event(category="disk_low", feed_id=None))

    def test_id_counter_is_monotonic(self) -> None:
        log = HealthEventLog()
        ids = [
            log.record(severity="info", category="c", message="m").id for _ in range(5)
        ]
        self.assertEqual(ids, [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
