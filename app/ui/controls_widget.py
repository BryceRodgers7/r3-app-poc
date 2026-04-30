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
    # Phase 7.H.2: was `short_segment_advance_requested` (a slice 4.D
    # no-op stub). Now wired to `coordinator.mark_next_play`.
    next_play_requested = Signal()
    # Phase 7.H.4: wired to `controller.replay_current_play` — seeks
    # to the currently-open play's start.
    replay_current_play_requested = Signal()

    def __init__(self, button_height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pause_button = QPushButton("Pause", self)
        self.rewind_button = QPushButton("Rewind 10s", self)
        # Phase 7.H.4: seeks to the start of the currently-open play.
        self.replay_play_button = QPushButton("Replay Play", self)
        self.half_speed_button = QPushButton("Slow 1/2x", self)
        self.quarter_speed_button = QPushButton("Slow 1/4x", self)
        self.live_button = QPushButton("Jump to Live", self)
        self.long_recording_button = QPushButton("Start game recording", self)
        # Phase 7.H.2: rebound from "Next clip" → "Next Play".
        self.next_play_button = QPushButton("Next Play", self)

        for button in (
            self.pause_button,
            self.rewind_button,
            self.replay_play_button,
            self.half_speed_button,
            self.quarter_speed_button,
            self.live_button,
            self.long_recording_button,
            self.next_play_button,
        ):
            button.setMinimumHeight(button_height)
            button.setStyleSheet("font-size: 20px; font-weight: 600; padding: 12px 18px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.rewind_button)
        layout.addWidget(self.replay_play_button)
        layout.addWidget(self.half_speed_button)
        layout.addWidget(self.quarter_speed_button)
        layout.addWidget(self.live_button)
        layout.addWidget(self.long_recording_button)
        layout.addWidget(self.next_play_button)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.rewind_button.clicked.connect(self.rewind_requested.emit)
        self.replay_play_button.clicked.connect(self.replay_current_play_requested.emit)
        self.half_speed_button.clicked.connect(self.half_speed_requested.emit)
        self.quarter_speed_button.clicked.connect(self.quarter_speed_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)
        self.long_recording_button.clicked.connect(self.long_recording_toggle_requested.emit)
        self.next_play_button.clicked.connect(self.next_play_requested.emit)

        self.next_play_button.setEnabled(False)
        # Phase 7.H.4: Replay Play also only makes sense while
        # recording (replay isn't available outside RECORDING per
        # §10.4 / §15.2). `set_recording_state` toggles both.
        self.replay_play_button.setEnabled(False)

    def set_recording_state(self, is_recording: bool) -> None:
        """Enable/disable the play-related buttons to track recording.

        Phase 7.H.2: Next Play is only meaningful while recording, so
        marking a play boundary only makes sense in RECORDING state.
        Phase 7.H.4: Replay Play is similarly gated — replay isn't
        available outside RECORDING (§10.4 / §15.2) and there's no
        current play to seek to anyway.
        """
        self.next_play_button.setEnabled(is_recording)
        self.replay_play_button.setEnabled(is_recording)
