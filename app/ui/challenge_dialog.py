"""Challenge details screen for the operator.

Opens when the operator presses **Challenge** and stays up until the
challenge is finished. It is *not* modal: the referee reviews the play in
the other window while this one is open, and an application-modal dialog
would block that window too.

The operator picks:

- **Team who challenged**: two buttons, labelled with the Team A / Team B
  names from the pre-match dialog.
- **Category**: one of the configured categories (`[challenge] categories`).
- **Outcome**: one of the configured outcomes (`[challenge] outcomes`).

A timer shows how long the challenge has run. **Challenge Done** stops it
and is enabled only once team, category and outcome are all chosen (none of
them may be empty). **Discard** drops a mis-pressed challenge after a
confirmation.

The dialog stores nothing itself. It emits `finish_requested` /
`discard_requested`; the owner calls the coordinator and then
`close_challenge()`. It cannot be dismissed any other way (Esc and the
window's close button are ignored), so an open challenge is never lost by
accident.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.challenge_manager import TEAM_A, TEAM_B, format_duration
from app.ui import theme
from app.ui.confirm import ConfirmFn, default_confirm
from app.ui.control_button import GO_TONE, NEUTRAL_TONE, control_button_style

_TIMER_TICK_MS = 250

_DIALOG_QSS = f"""
QDialog {{ background-color: {theme.WINDOW_BACKGROUND}; }}
QLabel[role="eyebrow"] {{
    color: {theme.TEXT_MUTED}; font-size: 12px; font-weight: 600;
    letter-spacing: 1.5px;
}}
QLabel[role="title"] {{ font-size: 24px; font-weight: 700; }}
QLabel[role="muted"] {{ color: {theme.TEXT_MUTED}; }}
QLabel[role="timer"] {{ font-size: 34px; font-weight: 700; }}
QFrame[role="rule"] {{ background-color: {theme.BORDER}; max-height: 1px; }}
QPushButton[role="choice"] {{
    min-height: 48px; padding: 6px 14px; font-size: 17px; font-weight: 600;
    background-color: {theme.CONTROL}; color: {theme.TEXT};
    border: 2px solid {theme.BORDER_STRONG}; border-radius: 6px;
}}
QPushButton[role="choice"]:hover {{ background-color: #3c4654; }}
QPushButton[role="choice"]:checked {{
    background-color: {theme.ACCENT}; color: #ffffff; border-color: #9cc9ff;
}}
"""


def _section(title: str) -> QWidget:
    """A small-caps heading with a hairline rule running out to the right."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    label = QLabel(title)
    label.setProperty("role", "eyebrow")
    rule = QFrame()
    rule.setProperty("role", "rule")
    rule.setFixedHeight(1)
    layout.addWidget(label)
    layout.addWidget(rule, 1)
    return row


class ChallengeDialog(QDialog):
    """Collect the details of one challenge while its timer runs."""

    finish_requested = Signal(str, str, str)  # team side, category, outcome
    discard_requested = Signal()

    def __init__(
        self,
        team_names: Mapping[str, str],
        categories: Sequence[str],
        outcomes: Sequence[str],
        elapsed_fn: Callable[[], float | None],
        *,
        parent: QWidget | None = None,
        confirm_fn: ConfirmFn | None = None,
    ) -> None:
        super().__init__(parent)
        self._elapsed_fn = elapsed_fn
        self._confirm: ConfirmFn = confirm_fn or default_confirm
        self._closing = False

        self.setWindowTitle("Challenge")
        # Not modal (the referee window must stay usable). Title bar only:
        # no close button, so it can't be dismissed by accident.
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setStyleSheet(_DIALOG_QSS)
        self.setFixedWidth(560)

        self.team_buttons: dict[str, QPushButton] = {}
        self.category_buttons: list[QPushButton] = []
        self.outcome_buttons: list[QPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)
        root.addLayout(self._build_header())

        root.addWidget(_section("TEAM WHO CHALLENGED"))
        team_row = QHBoxLayout()
        team_row.setSpacing(12)
        team_group = self._exclusive_group()
        for side in (TEAM_A, TEAM_B):
            button = self._choice_button(team_names.get(side, f"Team {side}"), team_group)
            self.team_buttons[side] = button
            team_row.addWidget(button, 1)
        root.addLayout(team_row)

        root.addWidget(_section("CATEGORY"))
        category_group = self._exclusive_group()
        for category in categories:
            button = self._choice_button(category, category_group)
            self.category_buttons.append(button)
            root.addWidget(button)

        root.addWidget(_section("OUTCOME"))
        outcome_row = QHBoxLayout()
        outcome_row.setSpacing(12)
        outcome_group = self._exclusive_group()
        for outcome in outcomes:
            button = self._choice_button(outcome, outcome_group)
            self.outcome_buttons.append(button)
            outcome_row.addWidget(button, 1)
        root.addLayout(outcome_row)

        root.addSpacing(6)
        root.addLayout(self._build_footer())

        self._timer = QTimer(self)
        self._timer.setInterval(_TIMER_TICK_MS)
        self._timer.timeout.connect(self._refresh_timer)
        self._timer.start()
        self._refresh_timer()
        self._refresh_state()

    # ------------------------------------------------------------ results

    def selected_team(self) -> str | None:
        """The side ("A" / "B") of the team that challenged, or None."""
        for side, button in self.team_buttons.items():
            if button.isChecked():
                return side
        return None

    def selected_category(self) -> str | None:
        return _checked_text(self.category_buttons)

    def selected_outcome(self) -> str | None:
        return _checked_text(self.outcome_buttons)

    def missing_details(self) -> list[str]:
        """Names of the details still to choose, in screen order."""
        missing = []
        if self.selected_team() is None:
            missing.append("team")
        if self.selected_category() is None:
            missing.append("category")
        if self.selected_outcome() is None:
            missing.append("outcome")
        return missing

    def timer_text(self) -> str:
        return self.timer_label.text()

    def close_challenge(self) -> None:
        """Close the dialog. Called by the owner once the challenge has ended."""
        self._closing = True
        self._timer.stop()
        self.accept()

    # ------------------------------------------------------------- actions

    def _on_done_clicked(self) -> None:
        team = self.selected_team()
        category = self.selected_category()
        outcome = self.selected_outcome()
        if team is None or category is None or outcome is None:
            return
        self._refresh_timer()  # show the elapsed time at the moment of the press
        self.finish_requested.emit(team, category, outcome)

    def _on_discard_clicked(self) -> None:
        if self._confirm(
            self,
            "Discard challenge?",
            "Discard this challenge? Nothing will be recorded. Use this only "
            "if Challenge was pressed by mistake.",
        ):
            self.discard_requested.emit()

    def reject(self) -> None:  # type: ignore[override]
        # Esc and the close button land here. The challenge only ends on
        # Challenge Done or Discard.
        if self._closing:
            super().reject()

    # --------------------------------------------------------------- build

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(2)
        eyebrow = QLabel("CHALLENGE")
        eyebrow.setProperty("role", "eyebrow")
        title = QLabel("Challenge details")
        title.setProperty("role", "title")
        left.addWidget(eyebrow)
        left.addWidget(title)
        row.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(0)
        caption = QLabel("Challenge time")
        caption.setProperty("role", "muted")
        caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.timer_label = QLabel("0:00")
        self.timer_label.setProperty("role", "timer")
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(caption)
        right.addWidget(self.timer_label)
        row.addLayout(right)
        return row

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(16)

        self.discard_button = QPushButton("Discard")
        self.discard_button.setStyleSheet(control_button_style(NEUTRAL_TONE, font_px=16))
        self.discard_button.setMinimumHeight(52)
        self.discard_button.setMinimumWidth(110)
        self.discard_button.setAutoDefault(False)
        self.discard_button.clicked.connect(self._on_discard_clicked)
        row.addWidget(self.discard_button)

        self.hint_label = QLabel()
        self.hint_label.setProperty("role", "muted")
        self.hint_label.setWordWrap(True)
        row.addWidget(self.hint_label, 1)

        self.done_button = QPushButton("Challenge Done")
        self.done_button.setStyleSheet(control_button_style(GO_TONE, font_px=19))
        self.done_button.setMinimumHeight(56)
        self.done_button.setMinimumWidth(210)
        self.done_button.setAutoDefault(False)
        self.done_button.clicked.connect(self._on_done_clicked)
        row.addWidget(self.done_button)
        return row

    def _exclusive_group(self) -> QButtonGroup:
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.buttonToggled.connect(lambda *_: self._refresh_state())
        return group

    def _choice_button(self, text: str, group: QButtonGroup) -> QPushButton:
        # Qt reads a lone "&" in button text as a keyboard-mnemonic marker
        # and hides it ("Faults & Interference" would lose its "&"). Show
        # "&&" instead, and keep the real text as the value to store.
        button = QPushButton(text.replace("&", "&&"))
        button.setProperty("value", text)
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.setProperty("role", "choice")
        group.addButton(button)
        return button

    # --------------------------------------------------------------- state

    def _refresh_state(self) -> None:
        missing = self.missing_details()
        self.done_button.setEnabled(not missing)
        if missing:
            self.hint_label.setText("Still to choose: " + ", ".join(missing) + ".")
        else:
            self.hint_label.setText("Press Challenge Done to stop the timer and save.")

    def _refresh_timer(self) -> None:
        elapsed = self._elapsed_fn()
        self.timer_label.setText(format_duration(elapsed if elapsed is not None else 0.0))


def _checked_text(buttons: Sequence[QPushButton]) -> str | None:
    for button in buttons:
        if button.isChecked():
            return str(button.property("value"))
    return None
