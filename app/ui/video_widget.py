"""Qt widget for the transitional live/replay video surface."""

from __future__ import annotations

from typing import Any

import cv2
from PySide6.QtCore import QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPixmap, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app.core.models import MediaFrame
from app.ui.feed_view import (
    FeedView,
    Tile,
    clamped,
    panned,
    picture_rect,
    visible_source_rect,
    zoomed,
)


_ANGLE_DELTA_PER_NOTCH = 120


class VideoWidget(QWidget):
    """Displays the selected live/replay frame.

    Nothing is drawn on top of the video surface — the tile shows the
    feed (or a full-surface placeholder when there's no video). All
    status chrome (play/clip counters, transport state) lives outside
    the video panel.

    With `enable_zoom_pan`, Ctrl+wheel zooms and a left-button drag pans.
    The view belongs to the widget, so it outlasts hiding the tile and
    switching between live and replay.
    """

    video_surface_resized = Signal()
    view_changed = Signal(object)  # FeedView
    # (x, y, width, height) the native sink should draw the picture into.
    native_render_rect_changed = Signal(object)

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
        self._showing_video_surface = False
        # `qimage` (legacy / python_push sources) renders frames into the
        # QLabel via QImage. `native` (3.A.3+ native sources) lets a
        # GStreamer video sink render directly into `_live_surface`.
        self._render_mode: str = "qimage"
        # Aspect ratio of the picture; None until `enable_zoom_pan`.
        self._zoom_pan_aspect_ratio: float | None = None
        self._view = FeedView()
        self._drag_position: QPointF | None = None
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

    def enable_zoom_pan(self, aspect_ratio: float) -> None:
        """Let this tile be zoomed and panned, for a picture of `aspect_ratio`."""
        self._zoom_pan_aspect_ratio = float(aspect_ratio)

    def view(self) -> FeedView:
        """Return the tile's current zoom and pan."""
        return self._view

    def _tile(self) -> Tile:
        assert self._zoom_pan_aspect_ratio is not None
        return Tile(self.width(), self.height(), self._zoom_pan_aspect_ratio)

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
        self.view_changed.emit(view)

    def _push_native_render_rect(self) -> None:
        """Tell the native sink where to draw, once it has been zoomed.

        A tile that has never been zoomed sends nothing, so the sink
        keeps tracking its window by itself.
        """
        if self._render_mode != "native" or self._zoom_pan_aspect_ratio is None:
            return
        if not (self._view.is_zoomed or self._native_rect_engaged):
            return
        if not self._has_area():
            return
        self._native_rect_engaged = True
        self.native_render_rect_changed.emit(
            picture_rect(self._shown_view(), self._tile())
        )

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

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._view.is_zoomed:
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
        # Replay runs this once per feed per tick on the UI thread, so it
        # is the hot path: no full-resolution RGB conversion, QImage copy or
        # Qt smooth-scale here (see `_fit_pixmap`).
        self._current_bgr = frame.image_bgr
        self._current_image = None
        self._refresh_pixmap()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the rendered frame scaled to the current widget size."""
        super().resizeEvent(event)
        self._refresh_pixmap()
        self._push_native_render_rect()
        if self._showing_video_surface:
            self.video_surface_resized.emit()

    def _full_image(self) -> QImage | None:
        """The current frame as a full-resolution QImage (zoom path only)."""
        if self._current_image is None and self._current_bgr is not None:
            rgb_image = cv2.cvtColor(self._current_bgr, cv2.COLOR_BGR2RGB)
            height, width, channels = rgb_image.shape
            self._current_image = QImage(
                rgb_image.data,
                width,
                height,
                channels * width,
                QImage.Format.Format_RGB888,
            ).copy()
        return self._current_image

    def _fit_pixmap(self) -> QPixmap:
        """The current frame scaled to fit the tile, aspect ratio kept.

        Scaled with cv2 before it becomes a QImage/QPixmap, so the colour
        conversion and pixmap upload work on tile-sized data. INTER_AREA is
        the quality choice for shrinking, which is almost always the case
        (720p footage in a tile smaller than the window).
        """
        bgr = self._current_bgr
        source_height, source_width = bgr.shape[:2]
        fitted = QSize(source_width, source_height).scaled(
            self._frame_label.size(), Qt.AspectRatioMode.KeepAspectRatio
        )
        width, height = fitted.width(), fitted.height()
        if width <= 0 or height <= 0:
            return QPixmap()  # nothing to draw into (a zero-size tile)
        if (width, height) != (source_width, source_height):
            shrinking = width < source_width or height < source_height
            bgr = cv2.resize(
                bgr,
                (width, height),
                interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR,
            )
        rgb_image = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return QPixmap.fromImage(
            QImage(rgb_image.data, width, height, 3 * width, QImage.Format.Format_RGB888)
        )

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
