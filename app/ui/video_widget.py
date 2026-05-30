"""Qt widget for the transitional live/replay video surface."""

from __future__ import annotations

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel, QStackedLayout, QVBoxLayout, QWidget

from app.core.models import MediaFrame


class VideoWidget(QWidget):
    """Displays the selected live/replay frame.

    Nothing is drawn on top of the video surface — the tile shows the
    feed (or a full-surface placeholder when there's no video). All
    status chrome (play/clip counters, transport state) lives outside
    the video panel.
    """

    video_surface_resized = Signal()

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
        self._current_image: QImage | None = None
        self._showing_video_surface = False
        # `qimage` (legacy / python_push sources) renders frames into the
        # QLabel via QImage. `native` (3.A.3+ native sources) lets a
        # GStreamer video sink render directly into `_live_surface`.
        self._render_mode: str = "qimage"

        self._surface_stack_host = QWidget(self)
        self._surface_stack_host.setObjectName("videoSurfaceHost")
        self._surface_stack = QStackedLayout(self._surface_stack_host)
        self._surface_stack.setContentsMargins(0, 0, 0, 0)
        self._surface_stack.addWidget(self._live_surface)
        self._surface_stack.addWidget(self._frame_label)
        self._surface_stack.setCurrentWidget(self._frame_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addWidget(self._surface_stack_host, stretch=1)

        self.setMinimumHeight(420)
        self.setStyleSheet(
            """
            QWidget#videoWidgetRoot {
                background-color: #1f1f1f;
                border: 2px solid #505050;
                border-radius: 10px;
            }
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
        rgb_image = cv2.cvtColor(frame.image_bgr, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb_image.shape
        bytes_per_line = channels * width
        self._current_image = QImage(
            rgb_image.data,
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        ).copy()
        self._refresh_pixmap()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the rendered frame scaled to the current widget size."""
        super().resizeEvent(event)
        self._refresh_pixmap()
        if self._showing_video_surface:
            self.video_surface_resized.emit()

    def _scaled_display_pixmap(self) -> QPixmap | None:
        if self._current_image is None:
            return None
        pixmap = QPixmap.fromImage(self._current_image)
        return pixmap.scaled(
            self._frame_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _refresh_pixmap(self) -> None:
        scaled_pixmap = self._scaled_display_pixmap()
        if scaled_pixmap is None:
            return

        self._frame_label.setText("")
        self._frame_label.setPixmap(scaled_pixmap)
