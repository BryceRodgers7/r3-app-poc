"""Application entrypoint for the sports replay proof of concept."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.application_coordinator import (
    ApplicationCoordinator,
    build_default_application_coordinator,
)
from app.media.output_renderer import MultiFeedOutputRenderer
from app.media.pipeline_manager import purge_unrouted_segments
from app.storage.file_manager import FileManager
from app.storage.metadata_db import MetadataDb
from app.storage.session_manager import SessionManager
from app.storage.session_recovery import (
    DirtySessionInfo,
    RecoveryAction,
    find_dirty_sessions,
    resolve_dirty_session,
    run_startup_scan,
)
from app.ui.main_window import MainWindow
from app.ui.recovery_dialog import RecoveryDialog


def build_application() -> tuple[QApplication, ApplicationCoordinator, list[MainWindow]]:
    """Create the Qt application and wire the coordinator and windows together."""
    settings = AppSettings.load()
    _configure_logging(settings)

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(settings.app_name)

    file_manager = FileManager(settings)
    metadata_db = MetadataDb(settings.metadata_db_path)
    session_manager = SessionManager(file_manager, metadata_db)

    purge_unrouted_segments()

    # Slice 4.E + §11.4: scan for unfinished prior sessions and let the
    # operator resolve each one before any new session is created. Runs
    # before coordinator.initialize() so the new-session creation path
    # never has to think about dirty manifests on disk.
    resume_session_id = _run_startup_recovery_flow(settings, metadata_db)

    referee_output = MultiFeedOutputRenderer()
    operator_output = MultiFeedOutputRenderer()
    coordinator = build_default_application_coordinator(
        settings,
        session_manager,
        referee_renderer=referee_output,
        operator_renderer=operator_output,
    )
    enabled_feeds = coordinator.feed_registry.get_enabled_feeds()

    referee_window = MainWindow(
        settings=settings,
        controller=coordinator.referee_controller,
        output_renderer=referee_output,
        feeds=enabled_feeds,
        window_title=settings.referee_window_title,
        show_controls=True,
        live_only_window=False,
        application_coordinator=coordinator,
    )
    operator_window = MainWindow(
        settings=settings,
        controller=coordinator.operator_controller,
        output_renderer=operator_output,
        feeds=enabled_feeds,
        window_title=settings.operator_window_title,
        show_controls=False,
        live_only_window=True,
        # Slice 3.A.3 retry: the operator window also needs the coordinator
        # so its MainWindow.__init__ runs the native-preview bind path.
        # Without this, only the referee sink got a window handle and the
        # operator sink fell back to creating its own d3d11 top-level window.
        application_coordinator=coordinator,
    )
    coordinator.initialize(resume_session_id=resume_session_id)
    qt_app.aboutToQuit.connect(coordinator.shutdown)
    return qt_app, coordinator, [referee_window, operator_window]


def _configure_logging(settings: AppSettings) -> None:
    """Send INFO+ logs to stderr and to a rotating file.

    File lives at `<base_data_dir>/logs/app.log` and rotates at 10 MB
    keeping 5 backups (~50 MB cap per machine). `logs/health_events.jsonl`
    stays per-session under the session folder; this file is the
    cross-session free-form Python log so the user can still see what
    happened on a previous run after the app exited.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Wipe handlers if `_configure_logging` somehow runs twice (e.g. in
    # tests that import main); avoids duplicated lines on stderr.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    log_dir = settings.base_data_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        # Fall back to stderr-only if the data dir isn't writable. Don't
        # crash startup over logging.
        logging.getLogger(__name__).exception(
            "could not open file log under %s; continuing with stderr only",
            log_dir,
        )


def _run_startup_recovery_flow(
    settings: AppSettings, db: MetadataDb
) -> str | None:
    """Run the §11.4 startup scan + recovery prompt before MainWindow opens.

    1. `run_startup_scan` marks unfinished sessions DIRTY and quarantines
       any corrupt segments.
    2. `find_dirty_sessions` lists everything still in DIRTY.
    3. For each, show a modal `RecoveryDialog`. The most recent dirty
       session gets Resume enabled; older ones don't (resuming two
       crashes at once isn't meaningful and complicates the UX).
    4. If the operator picks Resume, the chosen `session_id` is
       returned so the bootstrap can pass it to
       `coordinator.initialize(resume_session_id=...)`. Only the
       *first* Resume is honored; subsequent Resumes (which the dialog
       wouldn't normally offer anyway) are downgraded to FINALIZE for
       safety.

    Per §11.4 the prompt blocks all media UI; nothing has been shown
    yet at this point, so simply running modal dialogs serially before
    we build the windows satisfies that rule.
    """
    sessions_root = settings.sessions_root
    run_startup_scan(sessions_root, db)
    dirty = find_dirty_sessions(sessions_root)
    if not dirty:
        return None
    # Resume is offered only on the most recent dirty session
    # (`find_dirty_sessions` returns directory-sorted; the last entry
    # is the highest session_id and therefore the most recent).
    most_recent_id = dirty[-1].session_id
    resume_session_id: str | None = None
    for info in dirty:
        is_resume_eligible = (
            info.session_id == most_recent_id and resume_session_id is None
        )
        action = _prompt_for_recovery(info, is_resume_eligible=is_resume_eligible)
        if action is RecoveryAction.RESUME and resume_session_id is None:
            resume_session_id = info.session_id
        resolve_dirty_session(sessions_root, info.session_id, action)
    return resume_session_id


def _prompt_for_recovery(
    info: DirtySessionInfo, *, is_resume_eligible: bool
) -> RecoveryAction:
    """Run the modal recovery dialog and return the operator's choice."""
    dialog = RecoveryDialog(info, is_resume_eligible=is_resume_eligible)
    dialog.exec()
    chosen = dialog.chosen_action()
    # `RecoveryDialog` overrides `reject()` to no-op until a button is
    # pressed, so `chosen` is guaranteed to be set by the time exec()
    # returns. Still defend against future refactors.
    if chosen is None:
        return RecoveryAction.FINALIZE
    return chosen


def main() -> int:
    """Launch the desktop application."""
    app, _coordinator, windows = build_application()
    for window in windows:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
