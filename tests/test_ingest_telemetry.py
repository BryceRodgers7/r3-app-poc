"""Tests for ingest telemetry models and helpers."""

from __future__ import annotations

import unittest

from app.core.models import IngestTelemetry
from app.media.gst_ingest_telemetry import parse_video_dimensions_fps_from_caps


class IngestTelemetryTests(unittest.TestCase):
    def test_summary_line_includes_raw_and_target(self) -> None:
        t = IngestTelemetry(
            target_width=640,
            target_height=360,
            target_fps=15.0,
            raw_width=1920,
            raw_height=1080,
            raw_fps=29.97,
        )
        line = t.summary_line()
        self.assertIn("1920x1080", line)
        self.assertIn("640x360", line)
        self.assertIn("15", line)
        self.assertIn("Raw", line)
        self.assertIn("target", line)

    def test_summary_line_unknown_raw(self) -> None:
        t = IngestTelemetry(
            target_width=640,
            target_height=360,
            target_fps=15.0,
            raw_width=None,
            raw_height=None,
            raw_fps=None,
        )
        self.assertIn("unknown", t.summary_line())

    def test_parse_caps_none(self) -> None:
        w, h, fps = parse_video_dimensions_fps_from_caps(None)
        self.assertIsNone(w)
        self.assertIsNone(h)
        self.assertIsNone(fps)


if __name__ == "__main__":
    unittest.main()
