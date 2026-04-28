"""Application entrypoint for the sports replay proof of concept."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.application_coordinator import (
    ApplicationCoordinator,
    build_default_application_coordinator,
)
from app.media.output_renderer import MultiFeedOutputRenderer
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = AppSettings.load()

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(settings.app_name)

    file_manager = FileManager(settings)
    metadata_db = MetadataDb(settings.metadata_db_path)
    session_manager = SessionManager(file_manager, metadata_db)

    # Slice 4.E + §11.4: scan for unfinished prior sessions and let the
    # operator resolve each one before any new session is created. Runs
    # before coordinator.initialize() so the new-session creation path
    # never has to think about dirty manifests on disk.
    resume_session_id = _run_startup_recovery_flow(settings, metadata_db)

    operator_output = MultiFeedOutputRenderer()
    program_output = MultiFeedOutputRenderer()
    coordinator = build_default_application_coordinator(
        settings,
        session_manager,
        operator_renderer=operator_output,
        program_renderer=program_output,
    )
    enabled_feeds = coordinator.feed_registry.get_enabled_feeds()

    operator_window = MainWindow(
        settings=settings,
        controller=coordinator.operator_controller,
        output_renderer=operator_output,
        feeds=enabled_feeds,
        window_title=settings.operator_window_title,
        show_controls=True,
        program_live_only=False,
        application_coordinator=coordinator,
    )
    program_window = MainWindow(
        settings=settings,
        controller=coordinator.program_controller,
        output_renderer=program_output,
        feeds=enabled_feeds,
        window_title=settings.program_window_title,
        show_controls=False,
        program_live_only=True,
    )
    coordinator.initialize(resume_session_id=resume_session_id)
    qt_app.aboutToQuit.connect(coordinator.shutdown)
    return qt_app, coordinator, [operator_window, program_window]


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
