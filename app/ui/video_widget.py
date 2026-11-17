"""Qt widget for the transitional live/replay video surface."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QEventPoint,
    QImage,
    QMouseEvent,
    QPixmap,
    QResizeEvent,
    QTouchEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app.core.models import FittedImage, MediaFrame
from app.ui.feed_view import (
    CROP_LEVELS,
    FeedView,
    NativeView,
    Tile,
    clamped,
    native_view,
    panned,
    picture_rect,
    pinched,
    visible_source_rect,
    zoomed,
)


_ANGLE_DELTA_PER_NOTCH = 120
_TOUCH_EVENT_TYPES = frozenset(
    {
        QEvent.Type.TouchBegin,
        QEvent.Type.TouchUpdate,
        QEvent.Type.TouchEnd,
        QEvent.Type.TouchCancel,
    }
)


def fit_size(
    source_width: int, source_height: int, box_width: int, box_height: int
) -> tuple[int, int]:
    """The largest size with the source's aspect ratio that fits the box."""
    fitted = QSize(source_width, source_height).scaled(
        QSize(box_width, box_height), Qt.AspectRatioMode.KeepAspectRatio
    )
    return fitted.width(), fitted.height()


class VideoWidget(QWidget):
    """Displays the selected live/replay frame.

    Nothing is drawn on top of the video surface — the tile shows the
    feed (or a full-surface placeholder when there's no video). All
    status chrome (play/clip counters, transport state) lives outside
    the video panel.

    With `enable_zoom_pan`, Ctrl+wheel zooms and a left-button drag pans.
    On a touchscreen, two fingers pinch to zoom and pan together, and one
    finger pans a zoomed tile. The view belongs to the widget, so it
    outlasts hiding the tile and switching between live and replay.
    """

    video_surface_resized = Signal()
    view_changed = Signal(object)  # FeedView
    # (x, y, width, height) the native sink should draw the picture into.
    native_render_rect_changed = Signal(object)
    # (left, top, right, bottom) source pixels to cut off before the sink.
    # Sent ahead of the rectangle it goes with, and only when it changes.
    native_crop_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoWidgetRoot")
        self._live_surface = QWidget(self)
        # `WA_NativeWindow` forces Qt to allocate a native child window so
        # GStreamer's video sink can render directly into it.
        # `WA_DontCreateNativeAncestors` is the partner flag — without it
        # Qt may walk up the parent chain creating native windows on
        # demand, which on Windows can leave the child winId() returning
        # a not-yet-realized HWND when MainWindow hasn't been shown yet.
        # That race is the most likely cause of the slice 3.A.3 "third
        # unintended window" symptom (one d3d11videosink ends up with
        # an unusable handle and falls back to creating its own window).
        self._live_surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._live_surface.setAttribute(
            Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True
        )
        self._live_surface.setStyleSheet("background-color: #101010;")
        # Force Qt to create the native child window up front so GStreamer can bind to it.
        self._live_surface.winId()

        self._frame_label = QLabel("Awaiting video...", self)
        self._frame_label.setObjectName("framePlaceholderLabel")
        self._frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_label.setStyleSheet("background-color: #101010; color: #f3f3f3;")
        # A QLabel's minimum size follows its content. The pixmap here
        # is scaled to the label, so without this a tile could never
        # shrink again, and a grid reflow would push the window wider
        # than the screen.
        self._frame_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        # The frame on screen, kept as the BGR array it arrived as. The
        # normal (unzoomed) path scales it with cv2 straight to the tile, so
        # no full-resolution QImage is needed per frame; `_current_image`
        # is that QImage, built on demand for the zoom path and cleared on
        # every new frame.
        self._current_bgr: Any = None
        self._current_image: QImage | None = None
        # Size of the label the picture is fitted into, as last seen on the
        # UI thread. A worker thread reads it to scale replay frames ahead of
        # time (`prepare_for_display`); the UI thread re-checks it against the
        # real label size before using the result, so a stale value only
        # costs a re-scale, never a wrongly sized picture.
        self._fit_box: tuple[int, int] | None = None
        self._showing_video_surface = False
        # `qimage` (legacy / python_push sources) renders frames into the
        # QLabel via QImage. `native` (3.A.3+ native sources) lets a
        # GStreamer video sink render directly into `_live_surface`.
        self._render_mode: str = "qimage"
        # Aspect ratio of the picture; None until `enable_zoom_pan`.
        self._zoom_pan_aspect_ratio: float | None = None
        self._view = FeedView()
        self._drag_position: QPointF | None = None
        # The zoom last reported through `view_changed`: the title shows it,
        # so it has to follow what the tile can actually draw.
        self._announced_zoom = self._view.zoom
        # Frame size in pixels, when known: what lets live zoom crop the
        # frame upstream of the sink instead of growing its surface.
        self._source_size: tuple[int, int] | None = None
        self._native_view: NativeView | None = None
        self._sent_crop = (0, 0, 0, 0)
        # True once the native sink has been given a render rectangle.
        # An explicit rectangle stops the sink tracking its window, and
        # the sink can't be handed back: its (-1, -1, -1, -1) reset
        # blanks the picture. So it stays on rectangles from then on.
        self._native_rect_engaged = False

        self._surface_stack_host = QWidget(self)
        self._surface_stack_host.setObjectName("videoSurfaceHost")
        self._surface_stack = QStackedLayout(self._surface_stack_host)
        self._surface_stack.setContentsMargins(0, 0, 0, 0)
        self._surface_stack.addWidget(self._live_surface)
        self._surface_stack.addWidget(self._frame_label)
        self._surface_stack.setCurrentWidget(self._frame_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._surface_stack_host, stretch=1)

        self.setMinimumHeight(420)
        self.setStyleSheet(
            """
            QWidget#videoSurfaceHost {
                background-color: #101010;
                border: none;
            }
            QLabel#framePlaceholderLabel {
                background-color: #101010;
                border: none;
                color: #f3f3f3;
                font-size: 20px;
                font-weight: 600;
            }
            """
        )

    def enable_zoom_pan(
        self, aspect_ratio: float, source_size: tuple[int, int] | None = None
    ) -> None:
        """Let this tile be zoomed and panned, for a picture of `aspect_ratio`.

        `source_size` is the live frame's size in pixels. With it, zoom past
        what one sink surface can draw is done by cropping the frame before
        the sink; without it, the surface limit is the zoom limit.
        """
        self._zoom_pan_aspect_ratio = float(aspect_ratio)
        self._source_size = (
            (int(source_size[0]), int(source_size[1])) if source_size else None
        )
        # Without this Qt sends no touch events here, and turns a touch
        # into a mouse click, which cannot pinch. Only a tile that zooms
        # asks, so the operator window's tiles behave as before.
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

    def view(self) -> FeedView:
        """Return the tile's current zoom and pan."""
        return self._view

    def _tile(self) -> Tile:
        assert self._zoom_pan_aspect_ratio is not None
        return Tile(
            self.width(),
            self.height(),
            self._zoom_pan_aspect_ratio,
            CROP_LEVELS if self._source_size is not None else (1,),
        )

    def _has_area(self) -> bool:
        return self.width() > 0 and self.height() > 0

    def _shown_view(self) -> FeedView:
        """The view fitted to the tile as it is now.

        Hiding other feeds can leave a tile the wrong shape to show a
        view. Only drawing uses this fitted copy, so the referee's view
        is back intact when the tile is.
        """
        return clamped(self._view, self._tile())

    def _set_view(self, view: FeedView) -> None:
        if view == self._view:
            return
        self._view = view
        self._refresh_pixmap()
        self._push_native_render_rect()
        self._update_cursor()
        self._announce_zoom()

    def _announce_zoom(self) -> None:
        """Report the zoom the tile is drawing, if it has changed.

        A tile can draw less than the referee asked for (see `max_zoom`):
        the view is kept as set, but the title must show what is on screen.
        """
        shown = self._shown_view()
        if shown.zoom != self._announced_zoom:
            self._announced_zoom = shown.zoom
            self.view_changed.emit(shown)

    def _push_native_render_rect(self) -> None:
        """Tell the native sink how to show the view, once it has been zoomed.

        That is a render rectangle, and past what one surface can draw, a
        crop of the frame ahead of it. A tile that has never been zoomed
        sends nothing, so the sink keeps tracking its window by itself.
        """
        if self._render_mode != "native" or self._zoom_pan_aspect_ratio is None:
            return
        if not (self._view.is_zoomed or self._native_rect_engaged):
            return
        if not self._has_area():
            return
        self._native_rect_engaged = True
        tile = self._tile()
        view = self._shown_view()
        if self._source_size is None:
            crop, rect = (0, 0, 0, 0), picture_rect(view, tile)
        else:
            self._native_view = native_view(
                view, tile, self._source_size, self._native_view
            )
            crop, rect = self._native_view.crop, self._native_view.rect
        if crop != self._sent_crop:
            self._sent_crop = crop
            self.native_crop_changed.emit(crop)
        self.native_render_rect_changed.emit(rect)

    def _update_cursor(self) -> None:
        if self._drag_position is not None:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._view.is_zoomed:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Ctrl+wheel zooms about the cursor; a plain wheel does nothing here."""
        control_held = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if self._zoom_pan_aspect_ratio is None or not control_held:
            super().wheelEvent(event)
            return
        position = event.position()
        self._set_view(
            zoomed(
                self._shown_view(),
                event.angleDelta().y() / _ANGLE_DELTA_PER_NOTCH,
                position.x(),
                position.y(),
                self._tile(),
            )
        )
        event.accept()

    @staticmethod
    def _is_touch_echo(event: QMouseEvent) -> bool:
        """True for a mouse event the system made up from a touch.

        Windows sends a press and drag alongside every touch. The touch
        handler already panned for it, so acting on the echo would pan
        twice.
        """
        return event.source() != Qt.MouseEventSource.MouseEventNotSynthesized

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() != Qt.MouseButton.LeftButton
            or not self._view.is_zoomed
            or self._is_touch_echo(event)
        ):
            super().mousePressEvent(event)
            return
        self._drag_position = event.position()
        self._update_cursor()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_position is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self._drag_position
        self._drag_position = event.position()
        self._set_view(panned(self._shown_view(), delta.x(), delta.y(), self._tile()))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._drag_position is None:
            super().mouseReleaseEvent(event)
            return
        self._drag_position = None
        self._update_cursor()
        event.accept()

    def event(self, event: QEvent) -> bool:
        """Route touch events here; QWidget has no handler of its own for them."""
        # Type first: Qt sends events while `__init__` is still running,
        # before this widget's own attributes exist.
        if event.type() in _TOUCH_EVENT_TYPES and self._zoom_pan_aspect_ratio is not None:
            self._handle_touch(event)
            return True
        return super().event(event)

    def _handle_touch(self, event: QTouchEvent) -> None:
        """Two fingers pinch-zoom and pan together; one finger pans a zoomed tile.

        Each event is handled from where its fingers just were
        (`lastPosition`), so no gesture state is kept between events.
        """
        # Accepting the first event is what keeps the rest coming, and
        # what stops Qt faking a mouse press from the touch.
        event.accept()
        if not self._has_area():
            return
        down = sorted(
            (
                point
                for point in event.points()
                if point.state() != QEventPoint.State.Released
            ),
            key=lambda point: point.id(),
        )
        if len(down) >= 2:
            first, second = down[0], down[1]
            if QEventPoint.State.Pressed in (first.state(), second.state()):
                return  # a finger just landed: it has no earlier position
            self._set_view(
                pinched(
                    self._shown_view(),
                    (
                        (first.lastPosition().x(), first.lastPosition().y()),
                        (second.lastPosition().x(), second.lastPosition().y()),
                    ),
                    (
                        (first.position().x(), first.position().y()),
                        (second.position().x(), second.position().y()),
                    ),
                    self._tile(),
                )
            )
        elif len(down) == 1 and down[0].state() == QEventPoint.State.Updated:
            delta = down[0].position() - down[0].lastPosition()
            self._set_view(
                panned(self._shown_view(), delta.x(), delta.y(), self._tile())
            )

    def set_placeholder_text(self, text: str) -> None:
        """Update the placeholder message shown when no embedded video is active."""
        self._frame_label.setPixmap(QPixmap())
        self._frame_label.setText(text)

    def get_video_surface_handle(self) -> int:
        """Return the native child-window handle used by embedded video output."""
        return int(self._live_surface.winId())

    def set_render_mode(self, mode: str) -> None:
        """Set whether 'show video' should reveal the native surface or QImage label."""
        if mode not in {"qimage", "native"}:
            raise ValueError(f"Unsupported render mode: {mode!r}")
        self._render_mode = mode
        # If video is already meant to be visible, re-apply with the new mode.
        if self._showing_video_surface:
            self.set_video_surface_visible(True)

    def set_video_surface_visible(self, enabled: bool, *, live: bool = True) -> None:
        """Switch between active-video mode and placeholder mode.

        When `enabled` is True, the surface picked depends on render
        mode and the `live` flag:

        - `qimage` mode: always show the QLabel pixmap layer (legacy
          path; replay and live both render into the same QLabel via
          `display_frame`).
        - `native` mode + `live=True`: show `_live_surface` so the
          GStreamer d3d11videosink renders directly. No Python frame
          hop on the live path.
        - `native` mode + `live=False` (replay/pause): show the
          QLabel layer because replay frames arrive via `display_frame`
          from the segment decoder. `restore_live_surface` flips back
          when the operator returns to live.

        When `enabled` is False, always show the QLabel placeholder
        (e.g. SOURCE_LOST).
        """
        self._showing_video_surface = enabled
        if not enabled:
            self._surface_stack.setCurrentWidget(self._frame_label)
        elif self._render_mode == "native" and live:
            self._surface_stack.setCurrentWidget(self._live_surface)
        else:
            self._surface_stack.setCurrentWidget(self._frame_label)
        self.video_surface_resized.emit()

    def display_frame(self, frame: MediaFrame) -> None:
        """Render a new frame inside the preview area.

        In native render mode this is the replay path — receiving a
        frame here means the playback controller is showing a non-live
        timestamp, so the QStackedLayout flips to the QLabel layer and
        leaves the d3d11 native surface running invisibly behind it.
        `restore_live_surface()` flips back when the operator returns
        to LIVE.
        """
        if self._render_mode == "native" and self._showing_video_surface:
            self._surface_stack.setCurrentWidget(self._frame_label)
        # Replay runs this once per feed per frame on the UI thread, so it
        # is the hot path: no full-resolution QImage copy or Qt smooth-scale
        # here. A frame that a worker thread already scaled to this tile
        # (`prepare_for_display`) only needs handing to Qt; anything else is
        # scaled here (see `_fit_pixmap`).
        self._current_bgr = frame.image_bgr
        self._current_image = None
        self._fit_box = self._label_box()
        if frame.fitted is not None and self._show_fitted(frame.fitted):
            return
        self._refresh_pixmap()

    def prepare_for_display(self, frame: MediaFrame) -> MediaFrame:
        """Scale `frame` to this tile ahead of `display_frame`; safe off the UI thread.

        Sets `frame.fitted` when the tile is showing the whole picture. A
        zoomed tile, or one that has not been sized yet, is left for
        `display_frame` to handle on the UI thread.
        """
        box = self._fit_box
        if box is None or box[0] <= 0 or box[1] <= 0 or self._view.is_zoomed:
            return frame
        bgr = frame.image_bgr
        source_height, source_width = bgr.shape[:2]
        width, height = fit_size(source_width, source_height, box[0], box[1])
        if width <= 0 or height <= 0:
            return frame
        if (width, height) != (source_width, source_height):
            shrinking = width < source_width or height < source_height
            bgr = cv2.resize(
                bgr,
                (width, height),
                interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR,
            )
        frame.fitted = FittedImage(width, height, np.ascontiguousarray(bgr))
        return frame

    def _label_box(self) -> tuple[int, int]:
        size = self._frame_label.size()
        return size.width(), size.height()

    def _show_fitted(self, fitted: FittedImage) -> bool:
        """Show a frame a worker already scaled, if it still fits this tile."""
        if self._view.is_zoomed:
            return False
        box_width, box_height = self._label_box()
        source_height, source_width = self._current_bgr.shape[:2]
        if fit_size(source_width, source_height, box_width, box_height) != (
            fitted.width,
            fitted.height,
        ):
            return False
        self._frame_label.setText("")
        self._frame_label.setPixmap(self._pixmap_from_bgr(fitted.bgr, fitted.width, fitted.height))
        return True

    @staticmethod
    def _pixmap_from_bgr(bgr: Any, width: int, height: int) -> QPixmap:
        """A pixmap from BGR pixels, with no colour conversion step.

        Qt can read BGR directly, so there is no cv2.cvtColor pass over the
        frame. The pixmap copies the pixels, so `bgr` is free afterwards.
        """
        return QPixmap.fromImage(
            QImage(bgr.data, width, height, 3 * width, QImage.Format.Format_BGR888)
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the rendered frame scaled to the current widget size."""
        super().resizeEvent(event)
        self._fit_box = self._label_box()
        self._refresh_pixmap()
        self._push_native_render_rect()
        if self._zoom_pan_aspect_ratio is not None and self._has_area():
            self._announce_zoom()
        if self._showing_video_surface:
            self.video_surface_resized.emit()

    def _full_image(self) -> QImage | None:
        """The current frame as a full-resolution QImage (zoom path only)."""
        if self._current_image is None and self._current_bgr is not None:
            bgr = np.ascontiguousarray(self._current_bgr)
            height, width, channels = bgr.shape
            self._current_image = QImage(
                bgr.data,
                width,
                height,
                channels * width,
                QImage.Format.Format_BGR888,
            ).copy()
        return self._current_image

    def _fit_pixmap(self) -> QPixmap:
        """The current frame scaled to fit the tile, aspect ratio kept.

        Scaled with cv2 before it becomes a QImage/QPixmap, so the pixmap
        upload works on tile-sized data. INTER_AREA is the quality choice
        for shrinking, which is almost always the case (720p footage in a
        tile smaller than the window). Qt reads the BGR pixels as they are.
        """
        bgr = self._current_bgr
        source_height, source_width = bgr.shape[:2]
        box_width, box_height = self._label_box()
        width, height = fit_size(source_width, source_height, box_width, box_height)
        if width <= 0 or height <= 0:
            return QPixmap()  # nothing to draw into (a zero-size tile)
        if (width, height) != (source_width, source_height):
            shrinking = width < source_width or height < source_height
            bgr = cv2.resize(
                bgr,
                (width, height),
                interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR,
            )
        return self._pixmap_from_bgr(np.ascontiguousarray(bgr), width, height)

    def _scaled_display_pixmap(self) -> QPixmap | None:
        if self._current_bgr is None:
            return None
        if self._view.is_zoomed and self._has_area():
            image = self._full_image()
            source, target = visible_source_rect(
                self._shown_view(),
                self._tile(),
                image.width(),
                image.height(),
            )
            return QPixmap.fromImage(
                image.copy(QRect(*source)).scaled(
                    QSize(*target),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        return self._fit_pixmap()

    def _refresh_pixmap(self) -> None:
        scaled_pixmap = self._scaled_display_pixmap()
        if scaled_pixmap is None:
            return

        self._frame_label.setText("")
        self._frame_label.setPixmap(scaled_pixmap)
