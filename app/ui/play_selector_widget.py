"""Play selector for the referee window (during a challenge).

When the operator presses Challenge the referee is pinned to the play that
just ended. But the operator may have pressed Next Play *before* the
challenge was called, in which case the play to review is an earlier one.
This widget lets the referee step back (and forward again) through the
game's finished plays; each step re-pins the referee to that play.

It is gated on the challenge state, like the rest of the referee transport:
disabled outside a challenge. It holds no truth of its own. A press only
emits `play_selected`; the coordinator re-pins the replay and answers with
`challenge_review_changed`, and `set_plays` redraws from that. So the
selection shown is always the one actually under review.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui import theme
from app.ui.control_button import ButtonTone, ControlButton

# The selector is a secondary control the referee reaches for only when the
# call is in an earlier play, so it is deliberately quiet: short, thin-bordered
# buttons in a muted tone, well below the transport buttons' weight.
_BUTTON_FONT_PX = 13
_BUTTON_HEIGHT = 30
_SUBDUED_TONE = ButtonTone(
    background=theme.SURFACE,
    hover=theme.SURFACE_RAISED,
    border=theme.BORDER,
    text=theme.TEXT_MUTED,
)
# Thinner than the shared 2px button border, and a lighter weight.
_SUBDUED_EXTRA_QSS = " QPushButton { border-width: 1px; font-weight: 600; padding: 2px 6px; }"
_SEPARATOR_COLOR = "#3c4654"
_IDLE_TEXT = "—"


class PlaySelectorWidget(QWidget):
    """Earlier / Later buttons around a "Play N of M" readout.

    A hairline separator closes the widget, setting it apart from the
    transport buttons below it.
    """

    play_selected = Signal(int)

    def __init__(self, button_height: int = _BUTTON_HEIGHT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._plays: list[int] = []
        self._selected: int | None = None

        self.title_label = QLabel("REVIEW PLAY", self)
        self.title_label.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_MUTED}; font-size: 12px; font-weight: 600;"
            " letter-spacing: 1.5px; }"
        )
        self.status_label = QLabel(_IDLE_TEXT, self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.earlier_button = ControlButton(
            "◀ Earlier", self, tone=_SUBDUED_TONE, font_px=_BUTTON_FONT_PX
        )
        self.later_button = ControlButton(
            "Later ▶", self, tone=_SUBDUED_TONE, font_px=_BUTTON_FONT_PX
        )
        for button in (self.earlier_button, self.later_button):
            button.setMinimumHeight(button_height)
            button.setStyleSheet(button.styleSheet() + _SUBDUED_EXTRA_QSS)
        self.earlier_button.setToolTip("Review the previous play")
        self.later_button.setToolTip("Review the next play")

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.earlier_button, 1)
        buttons.addWidget(self.later_button, 1)

        self.separator = QFrame(self)
        self.separator.setFixedHeight(1)
        self.separator.setStyleSheet(f"QFrame {{ background-color: {_SEPARATOR_COLOR}; border: 0; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)
        layout.addSpacing(6)
        layout.addWidget(self.separator)

        self.earlier_button.clicked.connect(lambda: self._step(-1))
        self.later_button.clicked.connect(lambda: self._step(+1))
        self._refresh()

    # ------------------------------------------------------------------ API

    def set_challenge_active(self, active: bool) -> None:
        """Gate the selector on the challenge state (like the transport)."""
        self._active = active
        if not active:
            self._plays = []
            self._selected = None
        self._refresh()

    def set_plays(self, selected: int, plays: Sequence[int]) -> None:
        """Show `selected` as the play under review among `plays`."""
        self._selected = int(selected)
        self._plays = sorted(int(p) for p in plays)
        self._refresh()

    def selected_play(self) -> int | None:
        return self._selected

    def earlier_play(self) -> int | None:
        """The play one step back from the selection, or None at the start."""
        if self._selected is None:
            return None
        earlier = [p for p in self._plays if p < self._selected]
        return max(earlier) if earlier else None

    def later_play(self) -> int | None:
        """The play one step forward from the selection, or None at the end."""
        if self._selected is None:
            return None
        later = [p for p in self._plays if p > self._selected]
        return min(later) if later else None

    # ------------------------------------------------------------ internals

    def _step(self, direction: int) -> None:
        target = self.earlier_play() if direction < 0 else self.later_play()
        if target is not None and self._active:
            self.play_selected.emit(target)

    def _refresh(self) -> None:
        if self._active and self._selected is not None and self._plays:
            self.status_label.setText(f"Play {self._selected} of {self._plays[-1]}")
        else:
            self.status_label.setText(_IDLE_TEXT)
        self.status_label.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT if self._active else theme.TEXT_DISABLED};"
            " font-size: 18px; font-weight: 700; }"
        )
        self.earlier_button.setEnabled(self._active and self.earlier_play() is not None)
        self.later_button.setEnabled(self._active and self.later_play() is not None)
