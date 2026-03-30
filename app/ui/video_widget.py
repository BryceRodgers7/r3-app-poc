"""Qt widget for the temporary live/replay video surface."""

from __future__ import annotations

import cv2
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.core.models import MediaFrame


class VideoWidget(QWidget):
    """Displays the selected frame and an informational overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame_label = QLabel("Awaiting video...", self)
        self._frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_label.setStyleSheet("background-color: #101010; color: #f3f3f3;")

        self._overlay_label = QLabel("Initializing source...", self)
        self._overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay_label.setWordWrap(True)
        self._current_image: QImage | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addWidget(self._frame_label, stretch=1)
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
