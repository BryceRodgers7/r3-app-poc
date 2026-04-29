"""Touch-friendly playback controls."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget


class ControlsWidget(QWidget):
    """Large buttons for pause, rewind, and jump-to-live actions."""

    pause_requested = Signal()
    rewind_requested = Signal()
    live_requested = Signal()
    half_speed_requested = Signal()
    quarter_speed_requested = Signal()
    long_recording_toggle_requested = Signal()
    short_segment_advance_requested = Signal()

    def __init__(self, button_height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pause_button = QPushButton("Pause", self)
        self.rewind_button = QPushButton("Rewind 10s", self)
        self.half_speed_button = QPushButton("Slow 1/2x", self)
        self.quarter_speed_button = QPushButton("Slow 1/4x", self)
        self.live_button = QPushButton("Jump to Live", self)
        self.long_recording_button = QPushButton("Start game recording", self)
        self.next_clip_button = QPushButton("Next clip", self)

        for button in (
            self.pause_button,
            self.rewind_button,
            self.half_speed_button,
            self.quarter_speed_button,
            self.live_button,
            self.long_recording_button,
            self.next_clip_button,
        ):
            button.setMinimumHeight(button_height)
            button.setStyleSheet("font-size: 20px; font-weight: 600; padding: 12px 18px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.rewind_button)
        layout.addWidget(self.half_speed_button)
        layout.addWidget(self.quarter_speed_button)
        layout.addWidget(self.live_button)
        layout.addWidget(self.long_recording_button)
        layout.addWidget(self.next_clip_button)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.rewind_button.clicked.connect(self.rewind_requested.emit)
        self.half_speed_button.clicked.connect(self.half_speed_requested.emit)
        self.quarter_speed_button.clicked.connect(self.quarter_speed_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)
        self.long_recording_button.clicked.connect(self.long_recording_toggle_requested.emit)
        self.next_clip_button.clicked.connect(self.short_segment_advance_requested.emit)

        self.next_clip_button.setEnabled(False)
