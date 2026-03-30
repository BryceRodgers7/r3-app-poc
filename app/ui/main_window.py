"""Main application window."""

from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QStatusBar, QVBoxLayout, QWidget

from app.config.settings import AppSettings
from app.core.app_state import AppState
from app.core.playback_controller import PlaybackController
from app.media.preview_output import PreviewOutput
from app.ui.controls_widget import ControlsWidget
from app.ui.status_bar_widget import StatusBarWidget
from app.ui.video_widget import VideoWidget


class MainWindow(QMainWindow):
    """Top-level window for the sports replay proof of concept."""

    def __init__(
        self,
        settings: AppSettings,
        controller: PlaybackController,
        preview_output: PreviewOutput,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._controller = controller
        self._preview_output = preview_output

        self.setWindowTitle(settings.window_title)
        self.resize(1280, 860)

        self.video_widget = VideoWidget(self)
        self.controls_widget = ControlsWidget(button_height=settings.touch_button_height, parent=self)
        self.status_widget = StatusBarWidget(self)
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        central_widget = QWidget(self)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)
        layout.addWidget(self.video_widget, stretch=1)
        layout.addWidget(self.controls_widget)
        layout.addWidget(self.status_widget)
        self.setCentralWidget(central_widget)

        self._preview_output.bind_widget(self.video_widget)
        self._wire_events()
        self._render_state(self._controller.get_state())

    def _wire_events(self) -> None:
        self.controls_widget.pause_requested.connect(self._controller.pause_playback)
        self.controls_widget.rewind_requested.connect(self._controller.rewind_10_seconds)
        self.controls_widget.live_requested.connect(self._controller.jump_to_live)
        self._controller.signals.state_changed.connect(self._render_state)
        self._controller.signals.status_message.connect(self._status_bar.showMessage)

    def _render_state(self, state: AppState) -> None:
        self.status_widget.update_state(state)

        if state.current_playback_mode.value == "LIVE":
            overlay = "LIVE VIEW"
        elif state.current_playback_mode.value == "PAUSED":
            overlay = "PAUSED\nCapture, recording, and replay buffering continue"
        elif state.current_playback_mode.value == "REPLAY":
            overlay = f"REPLAY\nViewing approximately {state.seconds_behind_live:.0f}s behind live"
        else:
            overlay = "SOURCE LOST\nWaiting for the selected source"

        self.video_widget.set_overlay_text(overlay)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Shut down placeholder services when the window closes."""
        self._preview_output.detach_widget()
        self._controller.shutdown()
        super().closeEvent(event)
