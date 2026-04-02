"""Application entrypoint for the sports replay proof of concept."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.playback_controller import PlaybackController
from app.media.pipeline_manager import PipelineManager
from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer
from app.media.source_factory import build_default_source
from app.storage.file_manager import FileManager
from app.storage.metadata_db import MetadataDb
from app.storage.session_manager import SessionManager
from app.ui.main_window import MainWindow


def build_application() -> tuple[QApplication, MainWindow]:
    """Create the Qt application and wire placeholder services together."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = AppSettings()

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(settings.app_name)

    file_manager = FileManager(settings)
    metadata_db = MetadataDb(settings.metadata_db_path)
    session_manager = SessionManager(file_manager, metadata_db)
    source = build_default_source(settings)
    preview_output = PreviewOutput()
    recorder = Recorder(settings=settings)
    replay_buffer = ReplayBuffer(
        buffer_duration_seconds=settings.replay_buffer_seconds,
        jpeg_quality=settings.replay_buffer_jpeg_quality,
    )
    pipeline_manager = PipelineManager(
        source=source,
        preview_output=preview_output,
        recorder=recorder,
        replay_buffer=replay_buffer,
    )
    controller = PlaybackController(
        session_manager=session_manager,
        pipeline_manager=pipeline_manager,
        preview_output=preview_output,
        recorder=recorder,
        replay_buffer=replay_buffer,
        default_source_name=settings.default_source_name,
    )
    window = MainWindow(settings=settings, controller=controller, preview_output=preview_output)
    controller.initialize()
    return qt_app, window


def main() -> int:
    """Launch the desktop application."""
    app, window = build_application()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
