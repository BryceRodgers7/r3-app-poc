"""Yes/No confirmation shared by the operator-facing dialogs.

Dialogs take an injectable `confirm_fn` so tests do not drive real modal
boxes (the same pattern as `OperatorControlsWidget`'s End Game prompt).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QMessageBox, QWidget

ConfirmFn = Callable[[QWidget, str, str], bool]


def default_confirm(parent: QWidget, title: str, message: str) -> bool:
    """Ask a Yes/No question; True only when the operator picks Yes.

    No is the default button, so a stray Enter never confirms.
    """
    answer = QMessageBox.question(
        parent,
        title,
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes
