"""How `VideoWidget.display_frame` draws a replay frame.

Replay calls it once per feed per tick on the UI thread, so it scales with cv2
straight to the tile size and only builds a full-resolution QImage for the zoom
path. These tests pin the observable behaviour of that fast path.
"""

from __future__ import annotations

import unittest

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.core.models import MediaFrame
from app.ui.video_widget import VideoWidget


def _frame(width: int = 1280, height: int = 720) -> MediaFrame:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = (0, 0, 255)  # left half red (BGR)
    image[:, width // 2 :] = (255, 0, 0)  # right half blue
    return MediaFrame(
        frame_id=1, timestamp=0.0, image=image, source_name="t", feed_id="f"
    )


def _tile(width: int = 640, height: int = 360) -> VideoWidget:
    tile = VideoWidget()
    tile.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    tile.setMinimumHeight(10)
    tile.resize(width, height)
    tile.show()
    QApplication.processEvents()
    return tile


class ReplayDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _pixmap(self, tile: VideoWidget):
        return tile._frame_label.pixmap()

    def test_frame_is_fitted_to_the_tile_keeping_aspect_ratio(self) -> None:
        tile = _tile(640, 480)  # 4:3 tile, 16:9 frame -> letterboxed
        tile.display_frame(_frame(1280, 720))
        pixmap = self._pixmap(tile)
        self.assertEqual((pixmap.width(), pixmap.height()), (640, 360))

    def test_colours_survive_the_downscale(self) -> None:
        tile = _tile(640, 360)
        tile.display_frame(_frame())
        image = self._pixmap(tile).toImage()
        self.assertEqual(image.pixelColor(10, 100).name(), "#ff0000")
        self.assertEqual(image.pixelColor(630, 100).name(), "#0000ff")

    def test_a_frame_smaller_than_the_tile_is_scaled_up(self) -> None:
        tile = _tile(800, 450)
        tile.display_frame(_frame(320, 180))
        pixmap = self._pixmap(tile)
        self.assertEqual((pixmap.width(), pixmap.height()), (800, 450))

    def test_unzoomed_frames_never_build_a_full_resolution_qimage(self) -> None:
        tile = _tile()
        tile.display_frame(_frame())
        self.assertIsNone(tile._current_image)
        self.assertIsNotNone(self._pixmap(tile))

    def test_resizing_the_tile_refits_the_current_frame(self) -> None:
        tile = _tile(640, 360)
        tile.display_frame(_frame())
        tile.resize(320, 180)
        QApplication.processEvents()
        pixmap = self._pixmap(tile)
        self.assertEqual((pixmap.width(), pixmap.height()), (320, 180))

    def test_a_new_frame_replaces_the_picture(self) -> None:
        tile = _tile(640, 360)
        tile.display_frame(_frame())
        green = _frame()
        green.image[:] = (0, 255, 0)
        tile.display_frame(green)
        self.assertEqual(
            self._pixmap(tile).toImage().pixelColor(100, 100).name(), "#00ff00"
        )

    def test_a_zero_size_tile_draws_nothing_and_does_not_crash(self) -> None:
        tile = _tile()
        tile.setMinimumHeight(0)
        tile.resize(0, 0)
        QApplication.processEvents()
        tile.display_frame(_frame())  # must not raise

    def test_the_source_frame_is_not_modified(self) -> None:
        tile = _tile()
        frame = _frame()
        before = frame.image.copy()
        tile.display_frame(frame)
        np.testing.assert_array_equal(frame.image, before)


if __name__ == "__main__":
    unittest.main()
