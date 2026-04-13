"""Main application window."""

from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QStatusBar, QVBoxLayout, QWidget

from app.config.settings import AppSettings
from app.core.app_state import AppState
from app.core.models import FeedDefinition, PlaybackMode
from app.core.playback_controller import PlaybackController
from app.media.output_renderer import MultiFeedOutputRenderer
from app.ui.controls_widget import ControlsWidget
from app.ui.multi_feed_video_panel import MultiFeedVideoPanel
from app.ui.status_bar_widget import StatusBarWidget


class MainWindow(QMainWindow):
    """Top-level window for the sports replay proof of concept."""

    def __init__(
        self,
        settings: AppSettings,
        controller: PlaybackController,
        output_renderer: MultiFeedOutputRenderer,
        feeds: list[FeedDefinition],
        *,
        window_title: str | None = None,
        show_controls: bool = True,
        program_live_only: bool = False,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._controller = controller
        self._output_renderer = output_renderer
        self._show_controls = show_controls
        self._program_live_only = program_live_only

        self.setWindowTitle(window_title or settings.window_title)
        self.resize(1280, 860)

        enabled_feeds = [f for f in feeds if f.enabled]
        self.video_panel = MultiFeedVideoPanel(enabled_feeds, self)
        for feed_id, widget in self.video_panel.widgets_by_feed_id.items():
            self._output_renderer.bind_feed_widget(feed_id, widget)

        self.controls_widget = (
            ControlsWidget(button_height=settings.touch_button_height, parent=self)
            if show_controls
            else None
        )
        self.status_widget = StatusBarWidget(self)
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        central_widget = QWidget(self)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)
        layout.addWidget(self.video_panel, stretch=1)
        if self.controls_widget is not None:
            layout.addWidget(self.controls_widget)
        layout.addWidget(self.status_widget)
        self.setCentralWidget(central_widget)

        self._wire_events()
        self._render_state(self._controller.get_state())

    def _wire_events(self) -> None:
        if self.controls_widget is not None:
            self.controls_widget.pause_requested.connect(self._controller.pause_playback)
            self.controls_widget.rewind_requested.connect(self._controller.rewind_10_seconds)
            self.controls_widget.half_speed_requested.connect(lambda: self._controller.set_playback_rate(0.5))
            self.controls_widget.quarter_speed_requested.connect(
                lambda: self._controller.set_playback_rate(0.25)
            )
            self.controls_widget.live_requested.connect(self._controller.jump_to_live)
        self._controller.signals.state_changed.connect(self._render_state)
        self._controller.signals.status_message.connect(self._status_bar.showMessage)

    def _render_state(self, state: AppState) -> None:
        self.status_widget.update_state(state)
        show_embedded_video = state.current_playback_mode in {
            PlaybackMode.LIVE,
            PlaybackMode.PAUSED,
            PlaybackMode.REPLAY,
        }
        self.video_panel.set_playback_overlay(state.playback_overlay)
        self.video_panel.apply_tile_visibility(
            state.current_playback_mode,
            program_live_only=self._program_live_only,
        )
        if not show_embedded_video:
            placeholder_text = state.playback_overlay.status_text or "Waiting for the selected source"
            self.video_panel.set_global_placeholder(placeholder_text)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Release widget bindings when the window closes."""
        self._output_renderer.detach_all()
        super().closeEvent(event)
