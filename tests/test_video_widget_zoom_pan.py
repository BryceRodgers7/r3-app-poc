"""Ctrl+wheel zoom and drag pan on a tile, and how long the view lasts."""

from __future__ import annotations

import unittest

import numpy as np
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.models import FeedDefinition, MediaFrame
from app.ui.feed_view import ZOOM_PER_WHEEL_NOTCH, FeedView
from app.ui.multi_feed_video_panel import MultiFeedVideoPanel
from app.ui.video_widget import VideoWidget


_WIDE = 16 / 9
_LEFT = Qt.MouseButton.LeftButton


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


def _wheel(
    widget: VideoWidget,
    *,
    delta: int = 120,
    control: bool = True,
    position: tuple[int, int] = (400, 225),
) -> QWheelEvent:
    point = QPointF(*position)
    event = QWheelEvent(
        point,
        QPointF(widget.mapToGlobal(QPoint(*position))),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        (
            Qt.KeyboardModifier.ControlModifier
            if control
            else Qt.KeyboardModifier.NoModifier
        ),
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    return event


def _tile(*, native: bool = False) -> VideoWidget:
    tile = VideoWidget()
    tile.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    tile.setMinimumHeight(90)
    tile.resize(800, 450)
    tile.enable_zoom_pan(_WIDE)
    if native:
        tile.set_render_mode("native")
    tile.show()
    QApplication.processEvents()
    return tile


def _drag(tile: VideoWidget, start: tuple[int, int], end: tuple[int, int]) -> None:
    QTest.mousePress(tile, _LEFT, pos=QPoint(*start))
    QTest.mouseMove(tile, QPoint(*end))
    QTest.mouseRelease(tile, _LEFT, pos=QPoint(*end))


def _frame(width: int = 1920, height: int = 1080) -> MediaFrame:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = (0, 0, 255)  # left half red (BGR)
    image[:, width // 2 :] = (255, 0, 0)  # right half blue
    return MediaFrame(
        frame_id=1,
        timestamp=0.0,
        image=image,
        source_name="test",
        feed_id="feed_a",
    )


class ZoomInputTests(_QtTestCase):
    def test_ctrl_wheel_zooms(self) -> None:
        tile = _tile()
        _wheel(tile)
        self.assertAlmostEqual(tile.view().zoom, ZOOM_PER_WHEEL_NOTCH)

    def test_wheel_down_zooms_back_out(self) -> None:
        tile = _tile()
        _wheel(tile)
        _wheel(tile, delta=-120)
        self.assertEqual(tile.view(), FeedView())

    def test_plain_wheel_is_left_alone(self) -> None:
        tile = _tile()
        event = _wheel(tile, control=False)
        self.assertEqual(tile.view(), FeedView())
        self.assertFalse(event.isAccepted())

    def test_ctrl_wheel_does_nothing_unless_enabled(self) -> None:
        # The operator window's tiles never call enable_zoom_pan.
        tile = VideoWidget()
        tile.resize(800, 450)
        _wheel(tile)
        self.assertEqual(tile.view(), FeedView())

    def test_zoom_keeps_the_point_under_the_cursor(self) -> None:
        tile = _tile()
        _wheel(tile, position=(600, 225))
        self.assertGreater(tile.view().center_x, 0.5)


class PanInputTests(_QtTestCase):
    def test_drag_pans_when_zoomed(self) -> None:
        tile = _tile()
        for _ in range(4):
            _wheel(tile)
        before = tile.view()
        _drag(tile, (400, 225), (300, 225))
        self.assertGreater(tile.view().center_x, before.center_x)

    def test_drag_does_nothing_at_1x(self) -> None:
        tile = _tile()
        _drag(tile, (400, 225), (300, 225))
        self.assertEqual(tile.view(), FeedView())

    def test_click_without_moving_keeps_the_view(self) -> None:
        tile = _tile()
        _wheel(tile)
        before = tile.view()
        QTest.mouseClick(tile, _LEFT, pos=QPoint(400, 225))
        self.assertEqual(tile.view(), before)

    def test_cursor_shows_the_picture_can_be_grabbed(self) -> None:
        tile = _tile()
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.ArrowCursor)
        _wheel(tile)
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.OpenHandCursor)
        QTest.mousePress(tile, _LEFT, pos=QPoint(400, 225))
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.ClosedHandCursor)
        QTest.mouseRelease(tile, _LEFT, pos=QPoint(400, 225))
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.OpenHandCursor)
        _wheel(tile, delta=-120)
        self.assertEqual(tile.cursor().shape(), Qt.CursorShape.ArrowCursor)


class NativeRenderRectTests(_QtTestCase):
    """Live video: the view reaches the sink as a render rectangle."""

    def _capture(self, tile: VideoWidget) -> list[tuple[int, int, int, int]]:
        rects: list[tuple[int, int, int, int]] = []
        tile.native_render_rect_changed.connect(rects.append)
        return rects

    def test_never_zoomed_tile_leaves_the_sink_alone(self) -> None:
        # An explicit rectangle stops the sink tracking its window.
        tile = _tile(native=True)
        rects = self._capture(tile)
        tile.resize(640, 360)
        QApplication.processEvents()
        self.assertEqual(rects, [])

    def test_zoom_sends_the_oversized_picture(self) -> None:
        tile = _tile(native=True)
        rects = self._capture(tile)
        _wheel(tile)
        self.assertEqual(rects[-1][2:], (1000, 562))
        self.assertLess(rects[-1][0], 0)

    def test_pan_moves_the_rectangle_without_resizing_it(self) -> None:
        tile = _tile(native=True)
        rects = self._capture(tile)
        for _ in range(3):
            _wheel(tile)
        before = rects[-1]
        _drag(tile, (400, 225), (380, 225))
        self.assertEqual(rects[-1][0], before[0] - 20)
        self.assertEqual(rects[-1][2:], before[2:])

    def test_resize_after_zoom_sends_a_new_rectangle(self) -> None:
        # The sink doesn't follow its window once it has a rectangle.
        tile = _tile(native=True)
        rects = self._capture(tile)
        _wheel(tile)
        tile.resize(640, 360)
        QApplication.processEvents()
        self.assertEqual(rects[-1][2:], (800, 450))

    def test_zoomed_back_out_tile_keeps_tracking_resizes(self) -> None:
        # Back at 1x it still can't return to auto-sizing: the sink's
        # (-1, -1, -1, -1) reset blanks the picture.
        tile = _tile(native=True)
        rects = self._capture(tile)
        _wheel(tile)
        _wheel(tile, delta=-120)
        self.assertEqual(rects[-1], (0, 0, 800, 450))
        tile.resize(640, 360)
        QApplication.processEvents()
        self.assertEqual(rects[-1], (0, 0, 640, 360))

    def test_qimage_tiles_send_no_rectangles(self) -> None:
        tile = _tile()
        rects = self._capture(tile)
        _wheel(tile)
        self.assertEqual(rects, [])


class ReplayFrameTests(_QtTestCase):
    """Replay frames arrive at full resolution, so the crop shows real detail."""

    def _pixel(self, tile: VideoWidget, x: int, y: int) -> str:
        return tile._frame_label.pixmap().toImage().pixelColor(x, y).name()

    def _replay_tile(self) -> VideoWidget:
        tile = _tile()
        tile.display_frame(_frame())
        QApplication.processEvents()
        return tile

    def test_unzoomed_frame_shows_both_halves(self) -> None:
        tile = self._replay_tile()
        self.assertEqual(self._pixel(tile, 10, 100), "#ff0000")
        self.assertEqual(self._pixel(tile, 790, 100), "#0000ff")

    def test_zoom_crops_the_frame(self) -> None:
        tile = self._replay_tile()
        for _ in range(4):
            _wheel(tile, position=(100, 225))  # deep into the red half
        pixmap = tile._frame_label.pixmap()
        self.assertEqual((pixmap.width(), pixmap.height()), (800, 450))
        self.assertEqual(self._pixel(tile, 790, 100), "#ff0000")

    def test_view_applies_to_the_next_frame(self) -> None:
        tile = self._replay_tile()
        for _ in range(4):
            _wheel(tile, position=(100, 225))
        tile.display_frame(_frame())
        self.assertEqual(self._pixel(tile, 790, 100), "#ff0000")


class PersistenceTests(_QtTestCase):
    def _panel(self) -> MultiFeedVideoPanel:
        panel = MultiFeedVideoPanel(
            [
                FeedDefinition(feed_id="feed_a", display_name="A", source_kind="synthetic"),
                FeedDefinition(feed_id="feed_b", display_name="B", source_kind="synthetic"),
            ]
        )
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        panel.resize(1000, 800)
        panel.enable_zoom_pan(_WIDE)
        panel.show()
        QApplication.processEvents()
        return panel

    def test_each_feed_keeps_its_own_view(self) -> None:
        panel = self._panel()
        tiles = panel.widgets_by_feed_id
        _wheel(tiles["feed_a"])
        self.assertNotEqual(tiles["feed_a"].view(), FeedView())
        self.assertEqual(tiles["feed_b"].view(), FeedView())

    def test_view_survives_hiding_and_showing_the_feed(self) -> None:
        panel = self._panel()
        tile = panel.widgets_by_feed_id["feed_a"]
        for _ in range(3):
            _wheel(tile)
        _drag(tile, (100, 100), (70, 80))
        before = tile.view()
        panel.set_tile_visible("feed_a", False)
        QApplication.processEvents()
        panel.set_tile_visible("feed_a", True)
        QApplication.processEvents()
        self.assertEqual(tile.view().zoom, before.zoom)
        self.assertAlmostEqual(tile.view().center_x, before.center_x, places=6)
        self.assertAlmostEqual(tile.view().center_y, before.center_y, places=6)

    def test_view_survives_other_feeds_being_hidden(self) -> None:
        # Hiding feed_b hands feed_a a much taller tile, where the same
        # zoom can't pan vertically. That can't cost feed_a its pan.
        panel = self._panel()
        panel.resize(1600, 450)
        QApplication.processEvents()
        tile = panel.widgets_by_feed_id["feed_a"]
        for _ in range(3):
            _wheel(tile, position=(100, 40))
        before = tile.view()
        self.assertNotEqual(before.center_y, 0.5)
        panel.resize(500, 900)
        panel.set_tile_visible("feed_b", False)
        QApplication.processEvents()
        panel.set_tile_visible("feed_b", True)
        panel.resize(1600, 450)
        QApplication.processEvents()
        self.assertEqual(tile.view(), before)

    def test_tile_too_small_for_the_view_draws_it_fitted(self) -> None:
        tile = _tile(native=True)
        rects: list[tuple[int, int, int, int]] = []
        tile.native_render_rect_changed.connect(rects.append)
        for _ in range(3):
            _wheel(tile, position=(10, 10))
        tile.resize(300, 450)  # at 1.95x the picture no longer fills this height
        QApplication.processEvents()
        x, y, width, height = rects[-1]
        self.assertEqual(y, round((450 - height) / 2))

    def test_panning_starts_from_what_is_on_screen(self) -> None:
        # A stored view the tile can't show mustn't become a dead zone.
        tile = _tile()
        for _ in range(3):
            _wheel(tile, position=(10, 10))
        tile.resize(300, 450)
        QApplication.processEvents()
        _drag(tile, (150, 225), (150, 200))
        self.assertEqual(tile.view().center_y, 0.5)

    def test_zero_size_tile_does_not_crash_a_zoomed_replay(self) -> None:
        tile = _tile()
        tile.display_frame(_frame())
        _wheel(tile)
        tile.setMinimumHeight(0)
        tile.resize(0, 0)
        QApplication.processEvents()
        tile.display_frame(_frame())

    def test_view_survives_live_to_replay_and_back(self) -> None:
        tile = _tile(native=True)
        _wheel(tile)
        before = tile.view()
        tile.set_video_surface_visible(True, live=False)
        tile.set_video_surface_visible(True, live=True)
        self.assertEqual(tile.view(), before)

    def test_zoom_shows_in_the_title_not_on_the_video(self) -> None:
        panel = self._panel()
        tile = panel.widgets_by_feed_id["feed_a"]
        title = panel._titles["feed_a"]
        self.assertEqual(title.text(), "A")
        _wheel(tile)
        self.assertEqual(title.text(), "A  1.2×")
        _wheel(tile, delta=-120)
        self.assertEqual(title.text(), "A")


if __name__ == "__main__":
    unittest.main()
