"""Operator-window recording controls (Phase 13.A).

Hosts the recording transport that the persistent operator at the
live-only window operates: Start/Stop game recording and Next Play.
The referee's replay/review transport lives on `RefereeControlsWidget`
in the referee window — see `r3_app_architecture.md` §Phase 13.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget


class OperatorControlsWidget(QWidget):
    """Large buttons for the operator's recording transport."""

    long_recording_toggle_requested = Signal()
    next_play_requested = Signal()

    def __init__(self, button_height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.long_recording_button = QPushButton("Start game recording", self)
        # Phase 7.H.2: rebound from "Next clip" → "Next Play".
        self.next_play_button = QPushButton("Next Play", self)

        for button in (self.long_recording_button, self.next_play_button):
            button.setMinimumHeight(button_height)
            button.setStyleSheet("font-size: 20px; font-weight: 600; padding: 12px 18px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.long_recording_button)
        layout.addWidget(self.next_play_button)

        self.long_recording_button.clicked.connect(self.long_recording_toggle_requested.emit)
        self.next_play_button.clicked.connect(self.next_play_requested.emit)

        # Next Play is only meaningful while a game is being recorded —
        # there's no current play to advance otherwise. Start/Stop is
        # always enabled because pressing it IS the toggle.
        self.next_play_button.setEnabled(False)

    def set_recording_state(self, is_recording: bool) -> None:
        """Enable/disable Next Play to track recording state.

        Phase 7.H.2: marking a play boundary only makes sense in
        RECORDING state. Start/Stop stays enabled — pressing it IS
        the recording-state toggle.
        """
        self.next_play_button.setEnabled(is_recording)

    def set_recording_label(self, is_recording: bool) -> None:
        """Flip the Start/Stop button label between recording states."""
        self.long_recording_button.setText(
            "Stop game recording" if is_recording else "Start game recording"
        )
