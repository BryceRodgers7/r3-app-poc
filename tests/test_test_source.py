"""Focused tests for camera backend selection in TestSource."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app.media.test_source import TestSource


class _FakeCapture:
    def __init__(self, frames: list[np.ndarray | None], opened: bool = True) -> None:
        self._frames = list(frames)
        self._opened = opened
        self.released = False
        self.settings: dict[int, float] = {}

    def isOpened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._frames:
            return False, None
        frame = self._frames.pop(0)
        return frame is not None, frame

    def set(self, prop_id: int, value: float) -> bool:
        self.settings[prop_id] = value
        return True

    def release(self) -> None:
        self.released = True


class TestSourceTests(unittest.TestCase):
    def test_connect_source_skips_black_directshow_frames(self) -> None:
        black_frame = np.zeros((32, 48, 3), dtype=np.uint8)
        color_frame = np.full((32, 48, 3), 120, dtype=np.uint8)
        secondary_backend = getattr(cv2, "CAP_MSMF", cv2.CAP_ANY)
        captures = {
            cv2.CAP_DSHOW: _FakeCapture([black_frame] * 12),
            secondary_backend: _FakeCapture([color_frame]),
        }

        def fake_video_capture(camera_index: int, backend_id: int) -> _FakeCapture:
            self.assertEqual(camera_index, 0)
            capture = captures.get(backend_id)
            if capture is None:
                return _FakeCapture([], opened=False)
            return capture

        source = TestSource(
            source_name="Test Source",
            camera_index=0,
            frame_width=64,
            frame_height=36,
            target_fps=15.0,
        )

        with patch("app.media.test_source.cv2.VideoCapture", side_effect=fake_video_capture), patch(
            "app.media.test_source.time.sleep", return_value=None
        ):
            self.assertTrue(source.connect_source())
            backend_label = "CAP_MSMF" if secondary_backend == getattr(cv2, "CAP_MSMF", None) else "CAP_ANY"
            self.assertIn(backend_label, source.get_display_name())
            frame = source.read_frame()

        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.image.shape, (36, 64, 3))
        self.assertTrue(captures[cv2.CAP_DSHOW].released)
        self.assertFalse(captures[secondary_backend].released)

    def test_connect_source_falls_back_to_synthetic_when_all_backends_fail(self) -> None:
        black_frame = np.zeros((32, 48, 3), dtype=np.uint8)

        def fake_video_capture(camera_index: int, backend_id: int) -> _FakeCapture:
            self.assertEqual(camera_index, 2)
            del backend_id
            return _FakeCapture([black_frame] * 12)

        source = TestSource(
            source_name="Test Source",
            camera_index=2,
            frame_width=64,
            frame_height=36,
            target_fps=15.0,
        )

        with patch("app.media.test_source.cv2.VideoCapture", side_effect=fake_video_capture), patch(
            "app.media.test_source.time.sleep", return_value=None
        ):
            self.assertTrue(source.connect_source())
            self.assertIn("Synthetic Fallback", source.get_display_name())
            self.assertEqual(
                source.get_status_message(),
                "Camera opened but returned black frames; using synthetic fallback.",
            )
            frame = source.read_frame()

        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertGreater(int(frame.image.max()), 0)


if __name__ == "__main__":
    unittest.main()
