"""Phase 14.E: background QThread that runs the post-session processor.

The post-session processor shells out to ffmpeg per `(game, feed)`
item, and each call can take minutes for a real-length game. Running
it on the Qt main thread would freeze the dialog (and the rest of
the UI). This worker delegates the run to a Qt background thread so
the progress bar can repaint and the close button can stay
responsive.

Signal contract:

- `progress(processed: int, total: int, current: str)` — fires once
  per plan item *after* the encode attempt. `current` is
  `"<game_subdir>/<feed_id>"`. Routed onto the dialog's progress bar.
- `finished(success: bool, error_message: str | None,
   succeeded: int, failed: int, skipped: int)` — fires exactly once
  at the end of the run. The dialog uses `success` to choose between
  the "Close" and "OK" button labels and `error_message` (failure
  only) for the status label.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.tools.post_session_processor import (
    PostProcessRunResult,
    run_post_process,
)


class PostProcessWorker(QThread):
    """Background runner for `post_session_processor.run_post_process`."""

    progress = Signal(int, int, str)
    # 5-arg signal: success, error_message, succeeded, failed, skipped.
    # `object` for the optional str so None passes through cleanly
    # (PySide6's `str` signal type rejects None).
    finished_run = Signal(bool, object, int, int, int)

    def __init__(
        self,
        session_path: Path,
        *,
        metadata_db_path: Path | None = None,
        ffmpeg_path: Path | None = None,
        force: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._session_path = session_path
        self._metadata_db_path = metadata_db_path
        self._ffmpeg_path = ffmpeg_path
        self._force = force

    def run(self) -> None:  # noqa: D401 — QThread API
        """QThread.run() override — the body executes on the worker thread."""
        result: PostProcessRunResult = run_post_process(
            self._session_path,
            metadata_db_path=self._metadata_db_path,
            ffmpeg_path=self._ffmpeg_path,
            force=self._force,
            progress_callback=self._emit_progress,
        )
        self.finished_run.emit(
            result.success,
            result.error_message,
            result.succeeded,
            result.failed,
            result.skipped,
        )

    def _emit_progress(self, processed: int, total: int, current: str) -> None:
        """Per-item callback invoked from `export_all` on the worker thread.

        Qt queues the signal across the thread boundary, so the dialog's
        slot runs on the main thread — safe to touch QWidgets there.
        """
        self.progress.emit(processed, total, current)
