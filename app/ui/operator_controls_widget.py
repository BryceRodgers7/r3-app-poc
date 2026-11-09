"""Operator-window recording controls (Phase 14.B).

Hosts the recording transport the persistent operator drives at the
live-only window: Next Play, Time-out, Challenge, Mark Play, and
Begin/End Game. The referee's replay/review transport lives on
`RefereeControlsWidget` in the referee window — see
`docs/r3_app_architecture.md` §Phase 14.

Layout (matches `docs/window-layouts.pdf` page 1, right-edge column):

    [ Next Play  ]
    [ Time-out   ]
    [ Challenge  ]
       (spacer)
    [ Mark Play  ]
       (spacer)
    [ Begin Game ]   ← green when stopped, "End Game" red when recording

Gating (see `set_clip_state`):

  - Next Play: enabled iff recording.
  - Time-out: enabled iff recording AND `has_play_started` (i.e. the
    first Next Play of the game has been pressed).
  - Challenge: same as Time-out, plus disabled when the current clip
    is already a challenge (no back-to-back challenges per spec).
  - Mark Play: enabled iff recording.
  - Begin/End Game: always enabled — pressing it IS the toggle.

Latched highlights (see `set_clip_state`): Time-out and Challenge stay
lit while that clip type is open, and Mark Play stays lit (and reads
"Marked") while the open clip is marked. Mark Play has no other
on-screen effect, so without this a press looks like it did nothing.

End Game pops a confirmation modal before emitting the toggle. The
confirmation callable is injected (`confirm_fn`) so tests can stub
it without driving real Qt dialogs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.models import CLIP_TYPE_CHALLENGE, CLIP_TYPE_TIMEOUT
from app.ui import theme
from app.ui.control_button import GO_TONE, STOP_TONE, ControlButton


_MARK_PLAY_LABEL = "Mark Play"
_MARKED_LABEL = "✓ Marked"


def format_elapsed(seconds: float | None) -> str:
    """Render game time as `Elapsed: h:mm` (whole minutes, seconds dropped).

    `None` (no game has begun yet) reads as `Elapsed: 0:00`.
    """
    total = max(0, int(seconds)) if seconds is not None else 0
    hours, rest = divmod(total, 3600)
    return f"Elapsed: {hours}:{rest // 60:02d}"


def _elapsed_style(color: str) -> str:
    return f"QLabel {{ color: {color}; font-size: 22px; font-weight: 600; }}"


ConfirmFn = Callable[[QWidget, str, str], bool]


def _default_end_game_confirm(parent: QWidget, title: str, message: str) -> bool:
    """Default confirm-modal implementation: a Qt question modal.

    Returned True iff the user picks Yes. Tests inject a stub that
    returns True / False without driving real Qt dialogs.
    """
    result = QMessageBox.question(
        parent,
        title,
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes


class OperatorControlsWidget(QWidget):
    """Operator-window recording transport (Phase 14.B)."""

    long_recording_toggle_requested = Signal()
    next_play_requested = Signal()
    timeout_requested = Signal()
    challenge_requested = Signal()
    mark_play_toggle_requested = Signal()

    def __init__(
        self,
        button_height: int,
        parent: QWidget | None = None,
        *,
        confirm_fn: Optional[ConfirmFn] = None,
    ) -> None:
        super().__init__(parent)
        self._confirm_fn: ConfirmFn = confirm_fn or _default_end_game_confirm
        self._is_recording = False
        # True while the challenge details screen is open: Challenge stays
        # off until that challenge is finished or discarded.
        self._challenge_record_open = False

        self.next_play_button = ControlButton("Next Play", self)
        self.timeout_button = ControlButton("Time-out", self)
        self.challenge_button = ControlButton("Challenge", self)
        self.mark_play_button = ControlButton(_MARK_PLAY_LABEL, self)
        self.long_recording_button = ControlButton(
            "Begin Game", self, tone=GO_TONE, font_px=20
        )

        for button in (
            self.next_play_button,
            self.timeout_button,
            self.challenge_button,
            self.mark_play_button,
            self.long_recording_button,
        ):
            button.setMinimumHeight(button_height)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.next_play_button)
        layout.addWidget(self.timeout_button)
        layout.addWidget(self.challenge_button)
        layout.addSpacing(16)
        layout.addWidget(self.mark_play_button)
        layout.addStretch(1)
        # Time since Begin Game was pressed; sits above the button that
        # starts it. Muted while no game is recording.
        self.elapsed_label = QLabel(format_elapsed(None), self)
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed_label.setStyleSheet(_elapsed_style(theme.TEXT_MUTED))
        self._elapsed_running = False
        layout.addWidget(self.elapsed_label)
        layout.addWidget(self.long_recording_button)

        self.next_play_button.clicked.connect(self.next_play_requested.emit)
        self.timeout_button.clicked.connect(self.timeout_requested.emit)
        self.challenge_button.clicked.connect(self.challenge_requested.emit)
        self.mark_play_button.clicked.connect(self.mark_play_toggle_requested.emit)
        self.long_recording_button.clicked.connect(self._on_long_recording_clicked)

        # Defaults: nothing is recording at construction. Gating
        # disables the play-boundary buttons until the coordinator
        # signals RECORDING + the first Next Play press.
        self.set_clip_state(
            is_recording=False,
            has_play_started=False,
            current_clip_type=None,
        )

    # ------------------------------------------------------------------
    # Public API used by MainWindow._render_state
    # ------------------------------------------------------------------

    def set_recording_state(self, is_recording: bool) -> None:
        """Track recording state and gate the recording-only buttons.

        Owns gating for buttons whose enable depends ONLY on whether
        a game is being recorded (Next Play, Mark Play). Does NOT
        touch Time-out / Challenge — those depend on per-clip context
        and are owned exclusively by `set_clip_state`.

        Why the split: `MainWindow._render_state` calls this
        immediately before `set_clip_state` on every ~30Hz UiState
        tick. If `set_recording_state` reset Time-out / Challenge
        to disabled here, every tick would briefly flicker them off
        before `set_clip_state` re-enabled them — and a click event
        queued during that micro-window would be silently dropped
        by Qt (disabled QPushButtons swallow mouse events). Phase
        14.B field test caught this: Time-out/Challenge clicks were
        being dropped while Next Play/Mark Play (no flicker) worked.
        """
        self._is_recording = is_recording
        self.next_play_button.setEnabled(is_recording)
        self.mark_play_button.setEnabled(is_recording)
        if not is_recording:
            # Recording stopped → no clip is open, so Time-out /
            # Challenge are not meaningful either. Disable here so
            # callers that only invoke `set_recording_state(False)`
            # (e.g. older Phase 13 tests) still see a fully-disabled
            # widget.
            self.timeout_button.setEnabled(False)
            self.challenge_button.setEnabled(False)

    def set_recording_label(self, is_recording: bool) -> None:
        """Flip the Begin/End Game button label + color."""
        self.long_recording_button.setText(
            "End Game" if is_recording else "Begin Game"
        )
        self._apply_recording_button_style(is_recording=is_recording)

    def set_clip_state(
        self,
        *,
        is_recording: bool,
        has_play_started: bool,
        current_clip_type: str | None,
        is_marked: bool = False,
    ) -> None:
        """Re-evaluate per-clip button enables and latched highlights.

        Driven from `MainWindow._render_state` on every UiState tick.
        `has_play_started` is `state.current_play_number is not None`;
        `current_clip_type` is `state.current_clip_type`;
        `is_marked` is `state.current_clip_marked`.
        """
        self._is_recording = is_recording
        self.next_play_button.setEnabled(is_recording)
        play_dependent = is_recording and has_play_started
        self.timeout_button.setEnabled(play_dependent)
        in_challenge = current_clip_type == CLIP_TYPE_CHALLENGE
        self.challenge_button.setEnabled(
            play_dependent and not in_challenge and not self._challenge_record_open
        )
        self.mark_play_button.setEnabled(is_recording)
        self.timeout_button.set_active(current_clip_type == CLIP_TYPE_TIMEOUT)
        self.challenge_button.set_active(in_challenge)
        self.mark_play_button.set_active(is_marked)
        self.mark_play_button.setText(
            _MARKED_LABEL if is_marked else _MARK_PLAY_LABEL
        )

    def set_challenge_record_open(self, is_open: bool) -> None:
        """Hold Challenge off while the challenge details screen is open.

        `set_clip_state` (every UiState tick) applies the flag; this also
        applies it right away so the button cannot be pressed twice before
        the next tick.
        """
        self._challenge_record_open = is_open
        if is_open:
            self.challenge_button.setEnabled(False)

    def set_elapsed(self, seconds: float | None, *, running: bool) -> None:
        """Show `Elapsed: h:mm`; `running` False mutes it (no game recording)."""
        text = format_elapsed(seconds)
        if text != self.elapsed_label.text():
            self.elapsed_label.setText(text)
        if running != self._elapsed_running:
            self._elapsed_running = running
            self.elapsed_label.setStyleSheet(
                _elapsed_style(theme.TEXT if running else theme.TEXT_MUTED)
            )

    # ------------------------------------------------------------------
    # Internal click handler — End Game requires confirmation
    # ------------------------------------------------------------------

    def _on_long_recording_clicked(self) -> None:
        if not self._is_recording:
            self.long_recording_toggle_requested.emit()
            return
        confirmed = self._confirm_fn(
            self,
            "End Game",
            "Are you sure you want to end this game?",
        )
        if confirmed:
            # Stopping blocks the UI thread for over a second, and the
            # press flash expired behind the modal.
            self.long_recording_button.flash()
            self.long_recording_toggle_requested.emit()

    def _apply_recording_button_style(self, *, is_recording: bool) -> None:
        self.long_recording_button.set_tone(
            STOP_TONE if is_recording else GO_TONE
        )
