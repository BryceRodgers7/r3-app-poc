"""Tests for the synthetic dev-fallback source.

Phase 2.5 stripped USB / OpenCV / DSHOW / MSMF logic from `TestSource`;
what remains is a deterministic synthetic frame generator. These tests
cover the public contract a `SourceInterface` consumer relies on.
"""

from __future__ import annotations

import unittest

from app.media.test_source import TestSource


class TestSourceTests(unittest.TestCase):
    def _build(self) -> TestSource:
        return TestSource(
            source_name="Dev Source",
            feed_id="feed_dev",
            frame_width=64,
            frame_height=36,
            target_fps=30.0,
        )

    def test_connect_source_succeeds_without_hardware(self) -> None:
        source = self._build()
        self.assertTrue(source.connect_source())
        self.assertTrue(source.is_connected())
        self.assertIn("Synthetic", source.get_display_name())

    def test_read_frame_returns_a_non_black_frame_with_expected_shape(self) -> None:
        source = self._build()
        source.connect_source()
        frame = source.read_frame()
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.image.shape, (36, 64, 3))
        self.assertEqual(frame.feed_id, "feed_dev")
        self.assertGreater(int(frame.image.max()), 0)

    def test_read_frame_increments_frame_id_monotonically(self) -> None:
        source = self._build()
        source.connect_source()
        f1 = source.read_frame()
        f2 = source.read_frame()
        assert f1 is not None and f2 is not None
        self.assertGreater(f2.frame_id, f1.frame_id)

    def test_disconnect_clears_connected_flag(self) -> None:
        source = self._build()
        source.connect_source()
        source.disconnect_source()
        self.assertFalse(source.is_connected())
        self.assertIsNone(source.read_frame())

    def test_ingest_telemetry_reports_target_dimensions(self) -> None:
        source = self._build()
        source.connect_source()
        telemetry = source.get_ingest_telemetry()
        self.assertIsNotNone(telemetry)
        assert telemetry is not None
        self.assertEqual(telemetry.target_width, 64)
        self.assertEqual(telemetry.target_height, 36)
        self.assertEqual(telemetry.target_fps, 30.0)


if __name__ == "__main__":
    unittest.main()
