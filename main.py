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
    _run_startup_recovery_flow(settings, metadata_db)

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
    coordinator.initialize()
    qt_app.aboutToQuit.connect(coordinator.shutdown)
    return qt_app, coordinator, [operator_window, program_window]


def _run_startup_recovery_flow(settings: AppSettings, db: MetadataDb) -> None:
    """Run the §11.4 startup scan + recovery prompt before MainWindow opens.

    1. `run_startup_scan` marks unfinished sessions DIRTY and quarantines
       any corrupt segments.
    2. `find_dirty_sessions` lists everything still in DIRTY (newly marked
       *plus* anything left over from a prior unresolved run).
    3. For each, show a modal `RecoveryDialog`. The operator's choice is
       written back to the manifest via `resolve_dirty_session`.

    Per §11.4 the prompt blocks all media UI; nothing has been shown
    yet at this point, so simply running modal dialogs serially before
    we build the windows satisfies that rule.
    """
    sessions_root = settings.sessions_root
    run_startup_scan(sessions_root, db)
    dirty = find_dirty_sessions(sessions_root)
    if not dirty:
        return
    for info in dirty:
        action = _prompt_for_recovery(info)
        try:
            resolve_dirty_session(sessions_root, info.session_id, action)
        except NotImplementedError:
            # Defensive: the dialog disables Resume, so we should never
            # land here. Fall back to FINALIZE so the app can keep going
            # rather than crashing on launch.
            logging.getLogger(__name__).warning(
                "recovery: Resume requested for %s but unsupported; "
                "falling back to FINALIZE.",
                info.session_id,
            )
            resolve_dirty_session(
                sessions_root, info.session_id, RecoveryAction.FINALIZE
            )


def _prompt_for_recovery(info: DirtySessionInfo) -> RecoveryAction:
    """Run the modal recovery dialog and return the operator's choice."""
    dialog = RecoveryDialog(info)
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
