"""How `VideoWidget.display_frame` draws a replay frame.

Replay calls it once per feed per tick on the UI thread, so it scales with cv2
straight to the tile size and only builds a full-resolution QImage for the zoom
path. These tests pin the observable behaviour of that fast path.
"""

from __future__ import annotations

import threading
import unittest
import unittest.mock

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.core.models import MediaFrame
from app.media.output_renderer import MultiFeedOutputRenderer
from app.ui.feed_view import zoomed
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


class PreparedFrameTests(unittest.TestCase):
    """`prepare_for_display` scales a frame off the UI thread; `display_frame` uses it.

    The point is that the UI thread does no resize of its own for a prepared
    frame, and still draws exactly what the unprepared path draws.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _sized_tile(self, width: int = 640, height: int = 360) -> VideoWidget:
        tile = _tile(width, height)
        tile.display_frame(_frame())  # the first frame tells the tile its box
        return tile

    def test_a_prepared_frame_is_scaled_to_the_tile(self) -> None:
        tile = self._sized_tile(640, 480)
        prepared = tile.prepare_for_display(_frame(1280, 720))
        assert prepared.fitted is not None
        self.assertEqual((prepared.fitted.width, prepared.fitted.height), (640, 360))
        self.assertEqual(prepared.fitted.bgr.shape, (360, 640, 3))

    def test_a_tile_that_has_not_been_sized_leaves_the_frame_alone(self) -> None:
        tile = VideoWidget()
        self.assertIsNone(tile.prepare_for_display(_frame()).fitted)

    def test_a_prepared_frame_is_shown_without_scaling_on_the_ui_thread(self) -> None:
        tile = self._sized_tile()
        prepared = tile.prepare_for_display(_frame())
        with unittest.mock.patch(
            "app.ui.video_widget.cv2.resize", side_effect=AssertionError("resized on the UI thread")
        ):
            tile.display_frame(prepared)
        pixmap = tile._frame_label.pixmap()
        self.assertEqual((pixmap.width(), pixmap.height()), (640, 360))

    def test_prepared_and_unprepared_frames_draw_the_same_picture(self) -> None:
        tile = self._sized_tile()
        tile.display_frame(_frame())
        plain = tile._frame_label.pixmap().toImage()
        tile.display_frame(tile.prepare_for_display(_frame()))
        prepared = tile._frame_label.pixmap().toImage()
        self.assertEqual(plain, prepared)

    def test_colours_are_right_through_the_prepared_path(self) -> None:
        tile = self._sized_tile()
        tile.display_frame(tile.prepare_for_display(_frame()))
        image = tile._frame_label.pixmap().toImage()
        self.assertEqual(image.pixelColor(10, 100).name(), "#ff0000")
        self.assertEqual(image.pixelColor(630, 100).name(), "#0000ff")

    def test_a_frame_prepared_for_an_old_tile_size_is_scaled_again(self) -> None:
        tile = self._sized_tile(640, 360)
        stale = tile.prepare_for_display(_frame())  # fitted for 640x360
        tile.resize(320, 180)
        QApplication.processEvents()
        tile.display_frame(stale)
        pixmap = tile._frame_label.pixmap()
        self.assertEqual((pixmap.width(), pixmap.height()), (320, 180))

    def test_a_zoomed_tile_is_not_prepared_and_still_draws(self) -> None:
        tile = self._sized_tile(640, 360)
        tile.enable_zoom_pan(16 / 9)
        tile._view = zoomed(tile._view, 3, 320, 180, tile._tile())
        self.assertTrue(tile._view.is_zoomed)
        frame = tile.prepare_for_display(_frame())
        self.assertIsNone(frame.fitted)
        tile.display_frame(frame)  # the UI thread's zoom path draws it
        self.assertFalse(tile._frame_label.pixmap().isNull())

    def test_the_frame_is_not_modified_by_preparing_it(self) -> None:
        tile = self._sized_tile()
        frame = _frame()
        before = frame.image.copy()
        tile.prepare_for_display(frame)
        np.testing.assert_array_equal(frame.image, before)

    def test_preparing_works_from_another_thread(self) -> None:
        tile = self._sized_tile()
        result: list[MediaFrame] = []
        worker = threading.Thread(target=lambda: result.append(tile.prepare_for_display(_frame())))
        worker.start()
        worker.join(5)
        self.assertTrue(result and result[0].fitted is not None)

    def test_the_renderer_routes_prepare_to_the_feeds_tile(self) -> None:
        tile = self._sized_tile()
        renderer = MultiFeedOutputRenderer()
        renderer.bind_feed_widget("f", tile)
        self.assertIsNotNone(renderer.prepare_frame(_frame()).fitted)
        other = _frame()
        other.feed_id = "unbound"
        self.assertIsNone(renderer.prepare_frame(other).fitted)  # no tile: untouched


if __name__ == "__main__":
    unittest.main()
