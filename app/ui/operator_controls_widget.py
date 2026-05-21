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

End Game pops a confirmation modal before emitting the toggle. The
confirmation callable is injected (`confirm_fn`) so tests can stub
it without driving real Qt dialogs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.models import CLIP_TYPE_CHALLENGE


# Color-coded styling. Reused as the "challenge mode" red on the
# referee window's play-number badge (Phase 14.C / 14.D) so the two
# windows agree on what "active recording / dangerous action" red
# looks like.
_BEGIN_GAME_STYLE = (
    "QPushButton { background-color: #2c8a3a; color: white; "
    "font-size: 20px; font-weight: 700; padding: 12px 18px; "
    "border-radius: 6px; }"
    "QPushButton:hover { background-color: #34a045; }"
    "QPushButton:pressed { background-color: #226e2e; }"
)
_END_GAME_STYLE = (
    "QPushButton { background-color: #c33b3b; color: white; "
    "font-size: 20px; font-weight: 700; padding: 12px 18px; "
    "border-radius: 6px; }"
    "QPushButton:hover { background-color: #d24747; }"
    "QPushButton:pressed { background-color: #a02e2e; }"
)
_TRANSPORT_BUTTON_STYLE = (
    "QPushButton { font-size: 18px; font-weight: 600; padding: 10px 16px; }"
)


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

        self.next_play_button = QPushButton("Next Play", self)
        self.timeout_button = QPushButton("Time-out", self)
        self.challenge_button = QPushButton("Challenge", self)
        self.mark_play_button = QPushButton("Mark Play", self)
        self.long_recording_button = QPushButton("Begin Game", self)

        for button in (
            self.next_play_button,
            self.timeout_button,
            self.challenge_button,
            self.mark_play_button,
        ):
            button.setMinimumHeight(button_height)
            button.setStyleSheet(_TRANSPORT_BUTTON_STYLE)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )

        self.long_recording_button.setMinimumHeight(button_height)
        self.long_recording_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._apply_recording_button_style(is_recording=False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.next_play_button)
        layout.addWidget(self.timeout_button)
        layout.addWidget(self.challenge_button)
        layout.addSpacing(16)
        layout.addWidget(self.mark_play_button)
        layout.addStretch(1)
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
    ) -> None:
        """Re-evaluate per-clip button enables.

        Driven from `MainWindow._render_state` on every UiState tick.
        `has_play_started` is `state.current_play_number is not None`;
        `current_clip_type` is `state.current_clip_type`.
        """
        self._is_recording = is_recording
        self.next_play_button.setEnabled(is_recording)
        play_dependent = is_recording and has_play_started
        self.timeout_button.setEnabled(play_dependent)
        in_challenge = current_clip_type == CLIP_TYPE_CHALLENGE
        self.challenge_button.setEnabled(play_dependent and not in_challenge)
        self.mark_play_button.setEnabled(is_recording)

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
            self.long_recording_toggle_requested.emit()

    def _apply_recording_button_style(self, *, is_recording: bool) -> None:
        style = _END_GAME_STYLE if is_recording else _BEGIN_GAME_STYLE
        self.long_recording_button.setStyleSheet(style)
