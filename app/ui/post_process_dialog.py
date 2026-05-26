"""Phase 14.E: in-app modal that runs the post-session processor.

Hosted by the operator window's "Post-process & Exit" link. Shows a
progress bar + status text while ffmpeg encodes each
`(game_subdir, feed_id)` MP4, then either:

- Success → the button label flips to "Close" and clicking it
  triggers `QApplication.quit()` (which closes every window and
  drives `coordinator.shutdown` via `aboutToQuit`).
- Failure → the progress bar is hidden, the status label shows the
  first error message, and the button label flips to "OK". Clicking
  it also calls `QApplication.quit()` — the operator can re-run
  from the CLI per the existing manual workflow.

The dialog runs the encode on a `PostProcessWorker` (QThread) so the
main thread stays responsive (progress bar can repaint, dialog can
be moved).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.post_process_worker import PostProcessWorker


_STATUS_BEGIN = "Preparing to process…"
_BUTTON_LABEL_CLOSE = "Close"
_BUTTON_LABEL_OK = "OK"


class PostProcessDialog(QDialog):
    """Modal progress dialog for the in-app post-session processor."""

    def __init__(
        self,
        session_path: Path,
        *,
        metadata_db_path: Path | None = None,
        ffmpeg_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_path = session_path
        self._metadata_db_path = metadata_db_path
        self._ffmpeg_path = ffmpeg_path
        self._worker: PostProcessWorker | None = None
        self._quit_fn = QApplication.quit
        self._finished_seen = False

        self.setWindowTitle("Post-process & Exit")
        # Block the close-via-X button — the only way out is the final
        # button (which is disabled until the worker reports done).
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.status_label = QLabel(_STATUS_BEGIN, self)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(420)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.button_box = QDialogButtonBox(self)
        self._action_button = QPushButton(_BUTTON_LABEL_CLOSE, self)
        self._action_button.setEnabled(False)
        self._action_button.clicked.connect(self._on_action_clicked)
        self.button_box.addButton(
            self._action_button, QDialogButtonBox.ButtonRole.AcceptRole
        )
        layout.addWidget(self.button_box)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_quit_fn(self, quit_fn) -> None:
        """Test seam — override `QApplication.quit()` for headless runs."""
        self._quit_fn = quit_fn

    def start(self) -> None:
        """Launch the worker thread. Call after `exec()`-ing the dialog
        is set up but before showing it.

        Split out from `__init__` so tests can build the dialog, wire
        the quit-fn, and drive `update_progress` / `on_finished`
        directly without spawning a thread.
        """
        worker = PostProcessWorker(
            self._session_path,
            metadata_db_path=self._metadata_db_path,
            ffmpeg_path=self._ffmpeg_path,
            parent=self,
        )
        worker.progress.connect(self.update_progress)
        worker.finished_run.connect(self.on_finished)
        self._worker = worker
        worker.start()

    def update_progress(self, processed: int, total: int, current: str) -> None:
        """Slot for `PostProcessWorker.progress`. Safe to call from the
        main thread (Qt routes the signal across the thread boundary)."""
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(processed)
        self.status_label.setText(f"Processing {current}…")

    def on_finished(
        self,
        success: bool,
        error_message: object,
        succeeded: int,
        failed: int,
        skipped: int,
    ) -> None:
        """Slot for `PostProcessWorker.finished_run`. Renders the final
        state and re-enables the dismiss button.

        `error_message` is typed `object` so PySide6 can pass `None`
        through the signal (`Signal(str)` would coerce None to '')."""
        self._finished_seen = True
        if success:
            self._action_button.setText(_BUTTON_LABEL_CLOSE)
            if (succeeded + failed + skipped) == 0:
                self.status_label.setText(
                    "Nothing to process — no recorded games found."
                )
            else:
                self.status_label.setText(
                    f"Done — {succeeded} encoded"
                    + (f", {skipped} skipped" if skipped else "")
                    + "."
                )
            # Fill the bar so the operator sees a "complete" indicator
            # even on the empty-plan path.
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
        else:
            self._action_button.setText(_BUTTON_LABEL_OK)
            # Hide the bar — its half-filled state is misleading when
            # a failure aborts the run partway through.
            self.progress_bar.hide()
            msg = str(error_message) if error_message else "Post-processing failed."
            self.status_label.setText(f"Failed: {msg}")
        self._action_button.setEnabled(True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_action_clicked(self) -> None:
        """Close + quit the application.

        Wired to both the "Close" (success) and "OK" (failure)
        button labels — both paths exit the app per the 14.E spec.
        `QApplication.quit` triggers `aboutToQuit` → `coordinator.shutdown`,
        which finalizes the session manifest cleanly.
        """
        self.accept()
        self._quit_fn()

    def reject(self) -> None:  # noqa: D401 — Qt override
        """Block the Escape key from dismissing the dialog mid-run.

        The post-processor can be slow; the operator pressing Escape
        and walking away mid-encode would leave the app in a weird
        half-shut-down state. They have to wait for the button.
        """
        if not self._finished_seen:
            return
        super().reject()
