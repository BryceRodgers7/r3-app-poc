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
from app.ui.main_window import MainWindow


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


def main() -> int:
    """Launch the desktop application."""
    app, _coordinator, windows = build_application()
    for window in windows:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
