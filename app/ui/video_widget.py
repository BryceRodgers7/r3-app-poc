"""Placeholder widget for the future live/replay video surface."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class VideoWidget(QWidget):
    """Large placeholder surface that will later host video output."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._overlay_label = QLabel("Video preview placeholder", self)
        self._overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
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
                font-size: 24px;
                font-weight: 600;
            }
            """
        )

    def set_overlay_text(self, text: str) -> None:
        """Update the placeholder text shown inside the video surface."""
        self._overlay_label.setText(text)
