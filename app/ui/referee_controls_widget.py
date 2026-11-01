"""Referee-window playback controls (Phase 14.C rebuild).

Spec source: `docs/window-requirements.md`. The replay/review
transport an occasional-use referee operates:

    Play/Pause, 2x, 1/2x, 1/4x, 1/8x, Rewind Ns, Step ◀, Step ▶

Phase 13.A's `Replay Play` and `Jump to Live` buttons are gone —
the operator's Challenge button (14.B) drives the jump-to-play
behavior now, and there's no explicit live-return UI on the
referee window (the referee just resumes playback from wherever
they are after a lockout clear).

The Rewind button's duration is config-driven
(`settings.replay_rewind_seconds`, default 5s). Label is built from
the setting at construction time so future tweaks are settings-only.

The pause button's label flips between `⏸` (rate > 0) and `▶`
(rate == 0) via `set_playback_rate` — same Qt-signal-driven path
the recording-state gating uses.

The buttons sit in a two-column grid so the widget fits the window's
right-hand control rail. The speed button matching the current rate
stays lit (`set_active_rate`) because rate is otherwise only visible
in the video itself.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QWidget

from app.ui.control_button import ControlButton


_PAUSE_LABEL = "⏸ Pause"
_PLAY_LABEL = "▶ Play"
_BUTTON_FONT_PX = 16


class RefereeControlsWidget(QWidget):
    """Large buttons for the referee's replay-review transport."""

    pause_requested = Signal()
    rewind_requested = Signal()
    speed_2x_requested = Signal()
    half_speed_requested = Signal()
    quarter_speed_requested = Signal()
    eighth_speed_requested = Signal()
    # Phase 12.B: frame-step replay. Wired to
    # `controller.step_frames(±settings.replay_frame_step_count)`.
    step_back_requested = Signal()
    step_forward_requested = Signal()

    def __init__(
        self,
        button_height: int,
        *,
        rewind_seconds: int = 5,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._rewind_seconds = max(1, int(rewind_seconds))

        self.pause_button = ControlButton(_PAUSE_LABEL, self, font_px=_BUTTON_FONT_PX)
        self.speed_2x_button = ControlButton("2x", self, font_px=_BUTTON_FONT_PX)
        self.half_speed_button = ControlButton("1/2x", self, font_px=_BUTTON_FONT_PX)
        self.quarter_speed_button = ControlButton("1/4x", self, font_px=_BUTTON_FONT_PX)
        self.eighth_speed_button = ControlButton("1/8x", self, font_px=_BUTTON_FONT_PX)
        self.rewind_button = ControlButton(
            f"Rewind {self._rewind_seconds}s", self, font_px=_BUTTON_FONT_PX
        )
        self.step_back_button = ControlButton("Step ◀", self, font_px=_BUTTON_FONT_PX)
        self.step_forward_button = ControlButton("Step ▶", self, font_px=_BUTTON_FONT_PX)
        self._rate_buttons = (
            (self.speed_2x_button, 2.0),
            (self.half_speed_button, 0.5),
            (self.quarter_speed_button, 0.25),
            (self.eighth_speed_button, 0.125),
        )

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        for row, buttons in enumerate(
            (
                (self.pause_button, self.rewind_button),
                (self.speed_2x_button, self.half_speed_button),
                (self.quarter_speed_button, self.eighth_speed_button),
                (self.step_back_button, self.step_forward_button),
            )
        ):
            for column, button in enumerate(buttons):
                button.setMinimumHeight(button_height)
                layout.addWidget(button, row, column)

        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.speed_2x_button.clicked.connect(self.speed_2x_requested.emit)
        self.half_speed_button.clicked.connect(self.half_speed_requested.emit)
        self.quarter_speed_button.clicked.connect(self.quarter_speed_requested.emit)
        self.eighth_speed_button.clicked.connect(self.eighth_speed_requested.emit)
        self.rewind_button.clicked.connect(self.rewind_requested.emit)
        self.step_back_button.clicked.connect(self.step_back_requested.emit)
        self.step_forward_button.clicked.connect(self.step_forward_requested.emit)

        # Phase 14.F: the entire transport is gated on
        # `challenge_state_changed` (driven by ApplicationCoordinator).
        # All buttons start disabled — no challenge is active at startup.
        # `set_transport_enabled(True)` is called when a challenge begins;
        # `set_transport_enabled(False)` is called when it ends.
        # This subsumes the Phase 12.B recording-state gate (challenge
        # implies recording).
        self._set_all_enabled(False)

    def _set_all_enabled(self, enabled: bool) -> None:
        for button in (
            self.pause_button,
            self.speed_2x_button,
            self.half_speed_button,
            self.quarter_speed_button,
            self.eighth_speed_button,
            self.rewind_button,
            self.step_back_button,
            self.step_forward_button,
        ):
            button.setEnabled(enabled)

    def set_transport_enabled(self, active: bool) -> None:
        """Phase 14.F: gate every transport button on challenge state.

        Wired to `ApplicationCoordinator.challenge_state_changed` in
        MainWindow. Owning every button's enable in one method (rather
        than spreading the concern across multiple setters) avoids the
        flicker race documented in the Phase 14.B field-test notes (3).
        """
        self._set_all_enabled(active)

    def set_pause_label_for_rate(self, playback_rate: float) -> None:
        """Flip Pause/Play label by playback rate.

        Rate 0.0 means the replay clock is stopped — the button now
        acts as "Play" (resume at 1.0×); any non-zero rate means
        playback is advancing, so the button is "Pause." The handler
        wired in MainWindow inspects this when the click fires.
        """
        if playback_rate <= 0.0:
            self.pause_button.setText(_PLAY_LABEL)
        else:
            self.pause_button.setText(_PAUSE_LABEL)

    def set_active_rate(self, playback_rate: float) -> None:
        """Light the speed button matching the current playback rate.

        Nothing is lit at 1x (no button for it) or while paused.
        """
        for button, rate in self._rate_buttons:
            button.set_active(math.isclose(playback_rate, rate))
