"""Main application window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QStatusBar, QVBoxLayout, QWidget

from app.config.settings import AppSettings

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.application_coordinator import ApplicationCoordinator
from app.core.app_state import UiState
from app.core.models import FeedDefinition, PlaybackMode
from app.core.playback_controller import PlaybackController
from app.media.output_renderer import MultiFeedOutputRenderer
from app.ui.controls_widget import ControlsWidget
from app.ui.diagnostics_widget import DiagnosticsWidget
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
        application_coordinator: ApplicationCoordinator | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._controller = controller
        self._output_renderer = output_renderer
        self._show_controls = show_controls
        self._program_live_only = program_live_only
        self._application_coordinator = application_coordinator

        self.setWindowTitle(window_title or settings.window_title)
        self.resize(1280, 860)

        enabled_feeds = [f for f in feeds if f.enabled]
        self.video_panel = MultiFeedVideoPanel(enabled_feeds, self)
        # Slice 3.A.3 (retry): per-feed native preview wiring. For each
        # feed whose source is in NATIVE pipeline mode, flip the
        # widget's render_mode to "native" and hand the d3d11videosink
        # the widget's child-window handle BEFORE the pipeline goes to
        # PLAYING (handles passed in here are stored on PipelineManager
        # and re-applied during `_build_pipeline`). Sources in
        # `python_push` mode (synthetic test source) stay in the qimage
        # path. The `force_python_push_preview` setting overrides this
        # to keep everyone on the qimage path.
        role = "program" if program_live_only else "operator"
        for feed_id, widget in self.video_panel.widgets_by_feed_id.items():
            self._output_renderer.bind_feed_widget(feed_id, widget)
            if (
                application_coordinator is not None
                and application_coordinator.is_native_preview_active(feed_id)
            ):
                widget.set_render_mode("native")
                handle = widget.get_video_surface_handle()
                LOGGER.info(
                    "native-preview MainWindow bind: role=%s feed=%s handle=0x%x",
                    role,
                    feed_id,
                    int(handle),
                )
                application_coordinator.bind_native_preview_window_handle(
                    role, feed_id, handle
                )

        self.controls_widget = (
            ControlsWidget(button_height=settings.touch_button_height, parent=self)
            if show_controls
            else None
        )
        self.status_widget = StatusBarWidget(self)
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        self.diagnostics_widget: DiagnosticsWidget | None = None
        if (
            show_controls
            and application_coordinator is not None
            and application_coordinator.telemetry_hub is not None
        ):
            self.diagnostics_widget = DiagnosticsWidget(
                application_coordinator.telemetry_hub,
                self,
                coordinator=application_coordinator,
                settings=settings,
            )

        central_widget = QWidget(self)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)
        layout.addWidget(self.video_panel, stretch=1)
        if self.controls_widget is not None:
            layout.addWidget(self.controls_widget)
        layout.addWidget(self.status_widget)
        if self.diagnostics_widget is not None:
            layout.addWidget(self.diagnostics_widget)
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
            if self._application_coordinator is not None:
                self.controls_widget.long_recording_toggle_requested.connect(
                    self._application_coordinator.toggle_long_session_recording
                )
                self.controls_widget.short_segment_advance_requested.connect(
                    self._application_coordinator.advance_short_segments
                )
        self._controller.signals.state_changed.connect(self._render_state)
        self._controller.signals.status_message.connect(self._status_bar.showMessage)

    def _render_state(self, state: UiState) -> None:
        self.status_widget.update_state(state)
        if self._application_coordinator is not None:
            self.status_widget.set_app_state_summary(
                self._application_coordinator.get_app_state().value,
                self._application_coordinator._recording_manager.recording_state.state.value,
            )
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
        self.video_panel.apply_freeze_indicators(state.feeds_in_freeze_frame)
        if not show_embedded_video:
            placeholder_text = state.playback_overlay.status_text or "Waiting for the selected source"
            self.video_panel.set_global_placeholder(placeholder_text)

        if self.controls_widget is not None:
            self.controls_widget.next_clip_button.setEnabled(state.is_recording)
            self.controls_widget.long_recording_button.setText(
                "Stop game recording" if state.is_recording else "Start game recording"
            )

    def closeEvent(self, event: QCloseEvent) -> None:
        """Release widget bindings when the window closes."""
        self._output_renderer.detach_all()
        super().closeEvent(event)
