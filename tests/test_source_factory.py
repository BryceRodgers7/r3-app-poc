"""Focused tests for preferred live-source selection."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config.settings import AppSettings
from app.core.models import MediaFrame
from app.media.source_factory import PreferredSourceChain, build_default_source
from app.media.source_interface import SourceInterface


class _FakeSource(SourceInterface):
    def __init__(self, name: str, *, connects: bool, status_message: str | None = None) -> None:
        self._name = name
        self._connects = connects
        self._connected = False
        self._status_message = status_message
        self.connect_attempts = 0

    def connect_source(self) -> bool:
        self.connect_attempts += 1
        self._connected = self._connects
        return self._connected

    def disconnect_source(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_display_name(self) -> str:
        return self._name

    def create_pipeline_fragment(self) -> str:
        return self._name

    def read_frame(self) -> MediaFrame | None:
        return None

    def get_frame_size(self) -> tuple[int, int]:
        return 640, 360

    def get_nominal_fps(self) -> float:
        return 15.0

    def get_status_message(self) -> str | None:
        return self._status_message


class PreferredSourceChainTests(unittest.TestCase):
    def test_prefers_first_source_that_connects(self) -> None:
        primary = _FakeSource("GStreamer", connects=True)
        secondary = _FakeSource("OpenCV", connects=True)
        chain = PreferredSourceChain([primary, secondary])

        self.assertTrue(chain.connect_source())
        self.assertEqual(chain.get_display_name(), "GStreamer")
        self.assertEqual(primary.connect_attempts, 1)
        self.assertEqual(secondary.connect_attempts, 0)

    def test_falls_back_to_second_source(self) -> None:
        primary = _FakeSource("GStreamer", connects=False)
        secondary = _FakeSource(
            "OpenCV",
            connects=True,
            status_message="Camera opened but returned black frames; using synthetic fallback.",
        )
        chain = PreferredSourceChain([primary, secondary])

        self.assertTrue(chain.connect_source())
        self.assertEqual(chain.get_display_name(), "OpenCV")
        self.assertEqual(
            chain.get_status_message(),
            "Camera opened but returned black frames; using synthetic fallback.",
        )
        self.assertEqual(primary.connect_attempts, 1)
        self.assertEqual(secondary.connect_attempts, 1)

    def test_build_default_source_orders_gstreamer_before_test_source(self) -> None:
        settings = AppSettings()
        gst_source = _FakeSource("GStreamer", connects=False)
        opencv_source = _FakeSource("OpenCV", connects=True)

        with patch("app.media.source_factory.GStreamerCameraSource", return_value=gst_source), patch(
            "app.media.source_factory.TestSource", return_value=opencv_source
        ):
            source = build_default_source(settings)

        self.assertTrue(source.connect_source())
        self.assertEqual(source.get_display_name(), "OpenCV")
        self.assertEqual(gst_source.connect_attempts, 1)
        self.assertEqual(opencv_source.connect_attempts, 1)


if __name__ == "__main__":
    unittest.main()
