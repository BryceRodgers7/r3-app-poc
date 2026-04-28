"""§11.4 dirty-session recovery prompt (operator-facing modal)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.storage.session_recovery import DirtySessionInfo, RecoveryAction


class RecoveryDialog(QDialog):
    """Modal prompt asking the operator how to handle one dirty session.

    Per §11.4, the dialog blocks the media UI until the operator
    chooses. It cannot be dismissed without a decision: there is no
    Cancel button, the close button is removed, and Escape is ignored.

    Three actions are exposed:

    - **Resume** — disabled in this slice. Continuing the same
      `session_id` with new segment numbering needs a
      `SessionManager.adopt_session(...)` API plus segment-counter
      seeding; that lives in a follow-up. The button is shown so the
      operator can see it's a deliberate omission, with a tooltip
      explaining the workaround.
    - **End and finalize** — closes the session into `FINALIZED`. The
      surviving segments stay on disk for the post-session processor
      (Phase 8) to handle.
    - **Discard** — transitions the session to `CREATED` (empty shell).
      Segments stay on disk for §6.8 retention rules.
    """

    def __init__(
        self,
        info: DirtySessionInfo,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._chosen_action: RecoveryAction | None = None
        self.setWindowTitle("Unfinished session detected")
        # Remove the close button so the operator must use one of the
        # action buttons. Without this, clicking [X] would let them
        # bypass the prompt and the app would proceed with an
        # unresolved DIRTY session.
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(
            f"Session <b>{self._info.session_id}</b> did not shut down cleanly."
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title)

        if self._info.created_at:
            created = QLabel(f"Started: {self._info.created_at}")
            layout.addWidget(created)

        body = QLabel(
            "Pick what to do before the application can continue. The recorded "
            "segments are still on disk; corrupt files have already been moved "
            "to the session's quarantine folder."
        )
        body.setWordWrap(True)
        layout.addWidget(body)

        buttons = QDialogButtonBox(self)
        # Resume: deliberately disabled in this slice.
        self._resume_button = QPushButton("Resume", self)
        self._resume_button.setEnabled(False)
        self._resume_button.setToolTip(
            "Resume is not yet implemented. Pick End and finalize "
            "(keep the recording) or Discard (drop the session)."
        )
        buttons.addButton(self._resume_button, QDialogButtonBox.ButtonRole.AcceptRole)

        finalize_button = QPushButton("End and finalize", self)
        finalize_button.setDefault(True)
        finalize_button.clicked.connect(self._on_finalize)
        buttons.addButton(finalize_button, QDialogButtonBox.ButtonRole.AcceptRole)

        discard_button = QPushButton("Discard", self)
        discard_button.clicked.connect(self._on_discard)
        buttons.addButton(discard_button, QDialogButtonBox.ButtonRole.DestructiveRole)

        layout.addWidget(buttons)

    def chosen_action(self) -> RecoveryAction | None:
        """Return the action the operator selected, or `None` if not yet chosen."""
        return self._chosen_action

    def _on_finalize(self) -> None:
        self._chosen_action = RecoveryAction.FINALIZE
        self.accept()

    def _on_discard(self) -> None:
        self._chosen_action = RecoveryAction.DISCARD
        self.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        # Escape would otherwise call reject(); per §11.4 the prompt
        # must not be dismissable without a decision.
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:  # type: ignore[override]
        # Block any path (close button, system menu) that would close
        # the dialog without a chosen action.
        if self._chosen_action is None:
            return
        super().reject()
