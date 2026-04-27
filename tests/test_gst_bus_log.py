"""Unit tests for `app.media.gst_bus_log`."""

from __future__ import annotations

import logging
import unittest

from app.media.gst_bus_log import level_for_type, log_bus_message


class LevelMappingTests(unittest.TestCase):
    def test_known_types_map_to_expected_levels(self) -> None:
        self.assertEqual(level_for_type("ERROR"), logging.ERROR)
        self.assertEqual(level_for_type("WARNING"), logging.WARNING)
        self.assertEqual(level_for_type("INFO"), logging.INFO)
        self.assertEqual(level_for_type("EOS"), logging.INFO)
        self.assertEqual(level_for_type("STATE_CHANGED"), logging.DEBUG)

    def test_unknown_type_falls_back_to_debug(self) -> None:
        self.assertEqual(level_for_type("ASYNC_DONE"), logging.DEBUG)
        self.assertEqual(level_for_type("not-a-real-type"), logging.DEBUG)

    def test_type_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(level_for_type("error"), logging.ERROR)
        self.assertEqual(level_for_type("Warning"), logging.WARNING)


class LogBusMessageTests(unittest.TestCase):
    def test_error_emits_at_error_level_with_feed_id_and_role(self) -> None:
        with self.assertLogs("app.gst_bus", level="DEBUG") as captured:
            log_bus_message(
                feed_id="cam_a",
                pipeline_role="live",
                type_name="ERROR",
                summary="capture failed",
                details="v4l2src: device busy",
            )
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIn("feed_id=cam_a", captured.output[0])
        self.assertIn("role=live", captured.output[0])
        self.assertIn("type=ERROR", captured.output[0])
        self.assertIn("capture failed", captured.output[0])
        self.assertIn("v4l2src: device busy", captured.output[0])

    def test_warning_emits_at_warning_level(self) -> None:
        with self.assertLogs("app.gst_bus", level="DEBUG") as captured:
            log_bus_message(
                feed_id="cam_b",
                pipeline_role="replay",
                type_name="WARNING",
                summary="qos drop",
            )
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIn("feed_id=cam_b", captured.output[0])
        self.assertIn("role=replay", captured.output[0])

    def test_eos_emits_at_info_level_without_summary(self) -> None:
        with self.assertLogs("app.gst_bus", level="DEBUG") as captured:
            log_bus_message(
                feed_id="cam_c",
                pipeline_role="replay-audio",
                type_name="EOS",
            )
        self.assertEqual(captured.records[0].levelno, logging.INFO)
        self.assertIn("type=EOS", captured.output[0])

    def test_unknown_type_logs_at_debug_level(self) -> None:
        with self.assertLogs("app.gst_bus", level="DEBUG") as captured:
            log_bus_message(
                feed_id="cam_a",
                pipeline_role="live",
                type_name="ASYNC_DONE",
            )
        self.assertEqual(captured.records[0].levelno, logging.DEBUG)

    def test_missing_feed_id_falls_back_to_question_mark(self) -> None:
        with self.assertLogs("app.gst_bus", level="DEBUG") as captured:
            log_bus_message(
                feed_id="",
                pipeline_role="",
                type_name="INFO",
                summary="x",
            )
        self.assertIn("feed_id=?", captured.output[0])
        self.assertIn("role=?", captured.output[0])


if __name__ == "__main__":
    unittest.main()
