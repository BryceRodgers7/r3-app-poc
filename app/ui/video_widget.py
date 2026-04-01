"""Qt widget for the transitional live/replay video surface."""

from __future__ import annotations

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel, QStackedLayout, QVBoxLayout, QWidget

from app.core.models import MediaFrame


class VideoWidget(QWidget):
    """Displays the selected frame and an informational overlay."""

    live_surface_resized = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._live_surface = QWidget(self)
        self._live_surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._live_surface.setStyleSheet("background-color: #101010;")
        # Force Qt to create the native child window up front so GStreamer can bind to it.
        self._live_surface.winId()

        self._frame_label = QLabel("Awaiting video...", self)
        self._frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_label.setStyleSheet("background-color: #101010; color: #f3f3f3;")

        self._overlay_label = QLabel("Initializing source...", self)
        self._overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay_label.setWordWrap(True)
        self._current_image: QImage | None = None
        self._showing_live_sink = False

        surface_stack_host = QWidget(self)
        self._surface_stack = QStackedLayout(surface_stack_host)
        self._surface_stack.setContentsMargins(0, 0, 0, 0)
        self._surface_stack.addWidget(self._live_surface)
        self._surface_stack.addWidget(self._frame_label)
        self._surface_stack.setCurrentWidget(self._frame_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addWidget(surface_stack_host, stretch=1)
        layout.addWidget(self._overlay_label)

        self.setMinimumHeight(420)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #1f1f1f;
                border: 2px solid #505050;
                border-radius: 10px;
            }
            QLabel {
                color: #f3f3f3;
                font-size: 20px;
                font-weight: 600;
            }
            """
        )

    def set_overlay_text(self, text: str) -> None:
        """Update the status text shown with the video surface."""
        self._overlay_label.setText(text)

    def get_live_video_handle(self) -> int:
        """Return the native child-window handle used by the embedded preview sink."""
        return int(self._live_surface.winId())

    def set_live_mode(self, enabled: bool) -> None:
        """Switch between the embedded live sink and manual frame rendering."""
        if enabled == self._showing_live_sink:
            return
        self._showing_live_sink = enabled
        current_widget = self._live_surface if enabled else self._frame_label
        self._surface_stack.setCurrentWidget(current_widget)
        if enabled:
            self.live_surface_resized.emit()

    def display_frame(self, frame: MediaFrame) -> None:
        """Render a new frame inside the preview area."""
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
        if self._showing_live_sink:
            self.live_surface_resized.emit()

    def _refresh_pixmap(self) -> None:
        if self._current_image is None:
            return

        pixmap = QPixmap.fromImage(self._current_image)
        scaled_pixmap = pixmap.scaled(
            self._frame_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._frame_label.setText("")
        self._frame_label.setPixmap(scaled_pixmap)
