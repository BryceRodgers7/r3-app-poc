"""Ctrl+wheel zoom, drag pan and touch pinch/pan on a tile, and how long the view lasts."""

from __future__ import annotations

import unittest

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.models import FeedDefinition, MediaFrame
from app.ui.feed_view import (
    MAX_SURFACE_PIXELS,
    ZOOM_PER_WHEEL_NOTCH,
    FeedView,
    Tile,
    max_zoom,
    picture_rect,
)
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


def _mouse_event(
    widget: VideoWidget, event_type: QEvent.Type, x: int, *, synthesized: bool
) -> QMouseEvent:
    position = QPointF(x, 225)
    released = event_type == QEvent.Type.MouseButtonRelease
    return QMouseEvent(
        event_type,
        position,
        position,
        QPointF(widget.mapToGlobal(position.toPoint())),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton if released else Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        (
            Qt.MouseEventSource.MouseEventSynthesizedBySystem
            if synthesized
            else Qt.MouseEventSource.MouseEventNotSynthesized
        ),
    )


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


class _Fingers:
    """Drives a tile with `QTouchEvent`s, the way a touchscreen does.

    One `QTouchEventSequence` serves the whole gesture: it is what
    remembers where each finger is between events.
    """

    def __init__(self, tile: VideoWidget) -> None:
        self._tile = tile
        self._sequence = QTest.touchEvent(tile, QTest.createTouchDevice())

    def _commit(self) -> None:
        self._sequence.commit()
        QApplication.processEvents()

    def press(self, finger: int, x: int, y: int) -> None:
        self._sequence.press(finger, QPoint(x, y), self._tile)
        self._commit()

    def move(self, **fingers: tuple[int, int]) -> None:
        """Move fingers at once: `move(f0=(x, y), f1=(x, y))`."""
        for name, position in fingers.items():
            self._sequence.move(int(name[1:]), QPoint(*position), self._tile)
        self._commit()

    def release(self, finger: int, x: int = 0, y: int = 0) -> None:
        self._sequence.release(finger, QPoint(x, y), self._tile)
        self._commit()


class TouchInputTests(_QtTestCase):
    def test_a_zooming_tile_asks_for_touch_events(self) -> None:
        self.assertTrue(
            _tile().testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        )

    def test_the_operator_windows_tiles_do_not(self) -> None:
        # They never call enable_zoom_pan, so touch stays a plain click there.
        self.assertFalse(
            VideoWidget().testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        )

    def test_spreading_two_fingers_zooms_in(self) -> None:
        tile = _tile()
        fingers = _Fingers(tile)
        fingers.press(0, 350, 225)
        fingers.press(1, 450, 225)
        fingers.move(f0=(250, 225), f1=(550, 225))  # 100 -> 300 apart
        self.assertAlmostEqual(tile.view().zoom, 3.0, places=2)

    def test_pinching_closed_zooms_back_out(self) -> None:
        tile = _tile()
        fingers = _Fingers(tile)
        fingers.press(0, 350, 225)
        fingers.press(1, 450, 225)
        fingers.move(f0=(250, 225), f1=(550, 225))
        fingers.move(f0=(350, 225), f1=(450, 225))
        self.assertEqual(tile.view(), FeedView())

    def test_zoom_goes_to_eight_times_on_a_small_tile(self) -> None:
        tile = _tile()  # 800x450
        fingers = _Fingers(tile)
        fingers.press(0, 395, 225)
        fingers.press(1, 405, 225)
        for spread in (20, 40, 80, 160, 320, 640, 780):
            fingers.move(f0=(400 - spread // 2, 225), f1=(400 + spread // 2, 225))
        self.assertEqual(tile.view().zoom, 8.0)

    def test_two_fingers_moving_together_pan(self) -> None:
        tile = _tile()
        for _ in range(4):
            _wheel(tile)
        before = tile.view()
        fingers = _Fingers(tile)
        fingers.press(0, 350, 225)
        fingers.press(1, 450, 225)
        fingers.move(f0=(300, 225), f1=(400, 225))
        self.assertGreater(tile.view().center_x, before.center_x)
        self.assertAlmostEqual(tile.view().zoom, before.zoom)

    def test_the_picture_stays_under_the_fingers(self) -> None:
        tile = _tile()
        fingers = _Fingers(tile)
        fingers.press(0, 600, 225)
        fingers.press(1, 700, 225)
        fingers.move(f0=(500, 225), f1=(700, 225))  # spread 2x about (650, 225)
        left, _, width, _ = picture_rect(tile.view(), Tile(800, 450, _WIDE))
        # The source point that was under the new midpoint is the one that
        # was under the old.
        self.assertAlmostEqual((600 - left) / width * 2, 600 / 800 * 2 - 0.0, delta=0.5)
        self.assertAlmostEqual(tile.view().zoom, 2.0)

    def test_one_finger_pans_a_zoomed_tile(self) -> None:
        tile = _tile()
        for _ in range(4):
            _wheel(tile)
        before = tile.view()
        fingers = _Fingers(tile)
        fingers.press(0, 400, 225)
        fingers.move(f0=(300, 225))
        self.assertGreater(tile.view().center_x, before.center_x)

    def test_one_finger_does_nothing_at_1x(self) -> None:
        tile = _tile()
        fingers = _Fingers(tile)
        fingers.press(0, 400, 225)
        fingers.move(f0=(300, 225))
        self.assertEqual(tile.view(), FeedView())

    def test_a_second_finger_landing_does_not_jump_the_view(self) -> None:
        tile = _tile()
        for _ in range(4):
            _wheel(tile)
        fingers = _Fingers(tile)
        fingers.press(0, 300, 225)
        before = tile.view()
        fingers.press(1, 500, 225)
        self.assertEqual(tile.view(), before)

    # Lifting one of two fingers is checked against real Win32 touch input,
    # not here: QTest drops the remaining finger's moves after a release.

    def test_a_mouse_event_made_from_a_touch_is_ignored(self) -> None:
        # Windows sends a press and drag with every touch. The touch
        # handler has already panned for it, so acting on it pans twice.
        tile = _tile()
        for _ in range(4):
            _wheel(tile)
        before = tile.view()
        for event_type, x in (
            (QEvent.Type.MouseButtonPress, 400),
            (QEvent.Type.MouseMove, 300),
            (QEvent.Type.MouseButtonRelease, 300),
        ):
            QApplication.sendEvent(
                tile, _mouse_event(tile, event_type, x, synthesized=True)
            )
        self.assertEqual(tile.view(), before)

    def test_a_real_mouse_event_still_pans(self) -> None:
        tile = _tile()
        for _ in range(4):
            _wheel(tile)
        before = tile.view()
        for event_type, x in (
            (QEvent.Type.MouseButtonPress, 400),
            (QEvent.Type.MouseMove, 300),
            (QEvent.Type.MouseButtonRelease, 300),
        ):
            QApplication.sendEvent(
                tile, _mouse_event(tile, event_type, x, synthesized=False)
            )
        self.assertGreater(tile.view().center_x, before.center_x)

    def test_zoom_by_touch_reaches_the_native_sink(self) -> None:
        tile = _tile(native=True)
        rects: list[tuple[int, int, int, int]] = []
        tile.native_render_rect_changed.connect(rects.append)
        fingers = _Fingers(tile)
        fingers.press(0, 350, 225)
        fingers.press(1, 450, 225)
        fingers.move(f0=(300, 225), f1=(500, 225))
        self.assertEqual(rects[-1][2:], (1600, 900))

    def test_touch_zoom_shows_in_the_title(self) -> None:
        panel = PersistenceTests()._panel()
        tile = panel.widgets_by_feed_id["feed_a"]
        fingers = _Fingers(tile)
        fingers.press(0, 100, 100)
        fingers.press(1, 150, 100)
        fingers.move(f0=(80, 100), f1=(170, 100))
        self.assertIn("×", panel._titles["feed_a"].text())

    def test_the_mouse_still_works_after_touch(self) -> None:
        tile = _tile()
        fingers = _Fingers(tile)
        fingers.press(0, 350, 225)
        fingers.press(1, 450, 225)
        fingers.move(f0=(250, 225), f1=(550, 225))
        fingers.release(1)
        fingers.release(0)
        before = tile.view()
        _drag(tile, (400, 225), (300, 225))
        self.assertGreater(tile.view().center_x, before.center_x)


class ZoomLabelTests(_QtTestCase):
    """The title shows the zoom the tile draws, which a bigger tile may cap."""

    def test_zoom_set_on_a_small_tile_is_capped_when_the_tile_grows(self) -> None:
        tile = _tile()
        shown: list[float] = []
        tile.view_changed.connect(lambda view: shown.append(view.zoom))
        fingers = _Fingers(tile)
        fingers.press(0, 395, 225)
        fingers.press(1, 405, 225)
        for spread in (20, 40, 80, 160, 320, 640, 780):
            fingers.move(f0=(400 - spread // 2, 225), f1=(400 + spread // 2, 225))
        self.assertEqual(shown[-1], 8.0)
        tile.setMinimumHeight(0)
        tile.resize(2322, 1306)
        QApplication.processEvents()
        # This tile has no frame size, so it can't crop: the surface is its limit.
        self.assertAlmostEqual(
            shown[-1], max_zoom(Tile(2322, 1306, _WIDE, crop_levels=(1,)))
        )
        self.assertEqual(tile.view().zoom, 8.0)  # what the referee set is kept
        tile.resize(800, 450)
        QApplication.processEvents()
        self.assertEqual(shown[-1], 8.0)

    def test_resizing_without_a_zoom_change_says_nothing(self) -> None:
        tile = _tile()
        shown: list[float] = []
        tile.view_changed.connect(lambda view: shown.append(view.zoom))
        tile.resize(640, 360)
        QApplication.processEvents()
        self.assertEqual(shown, [])


class NativeCropTests(_QtTestCase):
    """Zoom past what one sink surface can draw is done by cropping the frame."""

    _SOURCE = (1920, 1080)

    def _big_tile(self, *, source=True) -> tuple[VideoWidget, list, list]:
        """A native tile as big as a maximized single tile, with what it sends."""
        tile = VideoWidget()
        tile.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        tile.setMinimumHeight(90)
        tile.resize(2322, 1306)
        tile.enable_zoom_pan(_WIDE, self._SOURCE if source else None)
        tile.set_render_mode("native")
        tile.show()
        QApplication.processEvents()
        sent: list = []
        tile.native_crop_changed.connect(lambda crop: sent.append(("crop", crop)))
        tile.native_render_rect_changed.connect(lambda rect: sent.append(("rect", rect)))
        return tile, sent, [sent]

    def _zoom_to(self, tile: VideoWidget, zoom: float) -> None:
        position = (tile.width() // 2, tile.height() // 2)
        while tile.view().zoom < zoom - 1e-9:
            _wheel(tile, position=position)

    def test_a_big_tile_zooms_to_8x_by_cropping(self) -> None:
        tile, sent, _ = self._big_tile()
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        self.assertEqual(tile.view().zoom, 8.0)
        crops = [value for kind, value in sent if kind == "crop"]
        self.assertTrue(crops)
        self.assertNotEqual(crops[-1], (0, 0, 0, 0))

    def test_the_surface_it_asks_for_stays_in_budget(self) -> None:
        tile, sent, _ = self._big_tile()
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
            rects = [value for kind, value in sent if kind == "rect"]
            self.assertLessEqual(rects[-1][2] * rects[-1][3], MAX_SURFACE_PIXELS * 1.01)

    def test_low_zoom_sends_no_crop(self) -> None:
        tile, sent, _ = self._big_tile()
        for _ in range(4):
            _wheel(tile, position=(1161, 653))
        self.assertEqual([k for k, _ in sent if k == "crop"], [])

    def test_the_crop_is_sent_before_the_rectangle_that_goes_with_it(self) -> None:
        tile, sent, _ = self._big_tile()
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        first_crop = next(i for i, (kind, _) in enumerate(sent) if kind == "crop")
        self.assertEqual(sent[first_crop + 1][0], "rect")

    def test_zooming_back_out_removes_the_crop(self) -> None:
        # Otherwise the sink would be left showing a part of the frame.
        tile, sent, _ = self._big_tile()
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        for _ in range(14):
            _wheel(tile, delta=-120, position=(1161, 653))
        self.assertEqual(tile.view(), FeedView())
        crops = [value for kind, value in sent if kind == "crop"]
        self.assertEqual(crops[-1], (0, 0, 0, 0))

    def test_the_crop_is_only_sent_when_it_changes(self) -> None:
        tile, sent, _ = self._big_tile()
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        sent.clear()
        # Small pans stay inside the crop already in use.
        for step in range(4):
            _drag(tile, (1000, 650), (1000 - 6, 650))
        self.assertEqual([k for k, _ in sent if k == "crop"], [])
        self.assertTrue([k for k, _ in sent if k == "rect"])

    def test_panning_far_moves_the_crop_with_it(self) -> None:
        tile, sent, _ = self._big_tile()
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        first = [v for k, v in sent if k == "crop"][-1]
        for _ in range(30):
            _drag(tile, (1500, 650), (700, 650))
        last = [v for k, v in sent if k == "crop"][-1]
        self.assertNotEqual(first, last)
        self.assertGreater(last[0], first[0])  # now showing more of the right

    def test_a_tile_without_a_frame_size_never_crops(self) -> None:
        tile, sent, _ = self._big_tile(source=False)
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        self.assertEqual([k for k, _ in sent if k == "crop"], [])
        self.assertAlmostEqual(tile.view().zoom, 4.0, places=1)

    def test_a_small_tile_never_needs_one(self) -> None:
        tile = _tile(native=True)
        tile.enable_zoom_pan(_WIDE, self._SOURCE)
        crops: list = []
        tile.native_crop_changed.connect(crops.append)
        for _ in range(12):
            _wheel(tile)
        self.assertEqual(tile.view().zoom, 8.0)
        self.assertEqual(crops, [])

    def test_qimage_tiles_send_no_crop(self) -> None:
        tile = _tile()
        tile.enable_zoom_pan(_WIDE, self._SOURCE)
        crops: list = []
        tile.native_crop_changed.connect(crops.append)
        for _ in range(12):
            _wheel(tile)
        self.assertEqual(crops, [])

    def test_the_title_shows_the_full_zoom_on_a_big_tile(self) -> None:
        tile, _, _ = self._big_tile()
        shown: list[float] = []
        tile.view_changed.connect(lambda view: shown.append(view.zoom))
        for _ in range(12):
            _wheel(tile, position=(1161, 653))
        self.assertEqual(shown[-1], 8.0)


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
