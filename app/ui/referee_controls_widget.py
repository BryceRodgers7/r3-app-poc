"""Referee-window playback controls (Phase 13.A).

Hosts the replay / review transport that an occasional-use referee
operates: Pause, Rewind 10s, Replay Play, Slow 1/2x, Slow 1/4x,
Step ◀, Step ▶, Jump to Live. The persistent operator's recording
transport (Start/Stop game, Next Play) lives on `OperatorControlsWidget`
in the operator (live-only) window — see `r3_app_architecture.md`
§Phase 13.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget


class RefereeControlsWidget(QWidget):
    """Large buttons for the referee's replay-review transport."""

    pause_requested = Signal()
    rewind_requested = Signal()
    live_requested = Signal()
    half_speed_requested = Signal()
    quarter_speed_requested = Signal()
    # Phase 7.H.4: wired to `controller.replay_current_play` — seeks
    # to the currently-open play's start.
    replay_current_play_requested = Signal()
    # Phase 12.B: frame-step replay. Wired to
    # `controller.step_frames(±settings.replay_frame_step_count)`.
    step_back_requested = Signal()
    step_forward_requested = Signal()

    def __init__(self, button_height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pause_button = QPushButton("Pause", self)
        self.rewind_button = QPushButton("Rewind 10s", self)
        # Phase 7.H.4: seeks to the start of the currently-open play.
        self.replay_play_button = QPushButton("Replay Play", self)
        self.half_speed_button = QPushButton("Slow 1/2x", self)
        self.quarter_speed_button = QPushButton("Slow 1/4x", self)
        # Phase 12.B: frame-by-frame jog buttons. Layout sits between
        # Slow 1/4x (slowest continuous rate) and Jump to Live so the
        # transport row reads left-to-right from "freshest content"
        # toward "live edge".
        self.step_back_button = QPushButton("Step ◀", self)
        self.step_forward_button = QPushButton("Step ▶", self)
        self.live_button = QPushButton("Jump to Live", self)

        for button in (
            self.pause_button,
            self.rewind_button,
            self.replay_play_button,
            self.half_speed_button,
            self.quarter_speed_button,
            self.step_back_button,
            self.step_forward_button,
            self.live_button,
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
        layout.addWidget(self.step_back_button)
        layout.addWidget(self.step_forward_button)
        layout.addWidget(self.live_button)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.rewind_button.clicked.connect(self.rewind_requested.emit)
        self.replay_play_button.clicked.connect(self.replay_current_play_requested.emit)
        self.half_speed_button.clicked.connect(self.half_speed_requested.emit)
        self.quarter_speed_button.clicked.connect(self.quarter_speed_requested.emit)
        self.step_back_button.clicked.connect(self.step_back_requested.emit)
        self.step_forward_button.clicked.connect(self.step_forward_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)

        # Phase 7.H.4 / 12.B: replay-related buttons are gated on
        # recording state because replay isn't available outside
        # RECORDING (§10.4 / §15.2). `set_recording_state` toggles them
        # when MainWindow's `_render_state` fires.
        self.replay_play_button.setEnabled(False)
        self.step_back_button.setEnabled(False)
        self.step_forward_button.setEnabled(False)

    def set_recording_state(self, is_recording: bool) -> None:
        """Enable/disable the replay-only buttons to track recording.

        Replay (and its frame-step / replay-play variants) isn't available
        outside RECORDING per §10.4 / §15.2. The continuous transport
        controls (Pause, Rewind, Slow, Jump to Live) are always enabled
        — they'll surface a status message if the operator presses them
        while replay is unavailable.
        """
        self.replay_play_button.setEnabled(is_recording)
        self.step_back_button.setEnabled(is_recording)
        self.step_forward_button.setEnabled(is_recording)
