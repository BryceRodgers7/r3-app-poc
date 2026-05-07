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
from app.ui.alert_banner import AlertBanner
from app.ui.diagnostics_widget import DiagnosticsWidget
from app.ui.multi_feed_video_panel import MultiFeedVideoPanel
from app.ui.operator_controls_widget import OperatorControlsWidget
from app.ui.referee_controls_widget import RefereeControlsWidget
from app.ui.status_bar_widget import StatusBarWidget

_VALID_CONTROLS_ROLES = frozenset({"referee", "operator", "none"})


class MainWindow(QMainWindow):
    """Top-level window for the sports replay proof of concept.

    `controls_role` (Phase 13.B) selects which transport widget the
    window builds:

    - `"referee"` — `RefereeControlsWidget` with the replay/review
      transport (Pause, Rewind, Replay Play, Slow 1/2x, Slow 1/4x,
      Step ◀/▶, Jump to Live). Default to keep older test callers
      that don't pass the kwarg working as before.
    - `"operator"` — `OperatorControlsWidget` with the recording
      transport (Start/Stop game, Next Play). Combined with
      `live_only_window=True` it's the persistent operator's pane.
    - `"none"` — no controls (reserved for a future "spectator"
      window).
    """

    def __init__(
        self,
        settings: AppSettings,
        controller: PlaybackController,
        output_renderer: MultiFeedOutputRenderer,
        feeds: list[FeedDefinition],
        *,
        window_title: str | None = None,
        controls_role: str = "referee",
        live_only_window: bool = False,
        application_coordinator: ApplicationCoordinator | None = None,
    ) -> None:
        super().__init__()
        if controls_role not in _VALID_CONTROLS_ROLES:
            raise ValueError(
                f"controls_role must be one of {sorted(_VALID_CONTROLS_ROLES)}; "
                f"got {controls_role!r}"
            )
        self._settings = settings
        self._controller = controller
        self._output_renderer = output_renderer
        self._controls_role = controls_role
        self._live_only_window = live_only_window
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
        role = "operator" if live_only_window else "referee"
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

        # Phase 13.A/13.B: at most one of these is non-None per window.
        # The `controls_role` selector picks which widget is built.
        self.referee_controls: RefereeControlsWidget | None = (
            RefereeControlsWidget(
                button_height=settings.touch_button_height, parent=self
            )
            if controls_role == "referee"
            else None
        )
        self.operator_controls: OperatorControlsWidget | None = (
            OperatorControlsWidget(
                button_height=settings.touch_button_height, parent=self
            )
            if controls_role == "operator"
            else None
        )

        self.status_widget = StatusBarWidget(self)
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        # DiagnosticsWidget today is gated on the referee window. The
        # eventual home for most diagnostics is the operator window
        # (deferred to a later slice — see §Phase 13 open questions).
        self.diagnostics_widget: DiagnosticsWidget | None = None
        if (
            controls_role == "referee"
            and application_coordinator is not None
            and application_coordinator.telemetry_hub is not None
        ):
            self.diagnostics_widget = DiagnosticsWidget(
                application_coordinator.telemetry_hub,
                self,
                coordinator=application_coordinator,
                settings=settings,
            )

        # Phase 10.A + 13.C: foreground §11.3 health warnings on the
        # operator (live-only) window — that's where the recording
        # transport (Start/Stop game, Next Play) lives, so the
        # persistent operator who can act on a `recording_error` /
        # `disk_low` / `feed_lost` event is the one who sees the
        # banner. Pre-13.C the banner was on the referee window, but
        # the referee is occasional-use and could miss the alert.
        self.alert_banner: AlertBanner | None = (
            AlertBanner(parent=self) if controls_role == "operator" else None
        )

        central_widget = QWidget(self)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)
        if self.alert_banner is not None:
            layout.addWidget(self.alert_banner)
        layout.addWidget(self.video_panel, stretch=1)
        if self.referee_controls is not None:
            layout.addWidget(self.referee_controls)
        if self.operator_controls is not None:
            layout.addWidget(self.operator_controls)
        layout.addWidget(self.status_widget)
        if self.diagnostics_widget is not None:
            layout.addWidget(self.diagnostics_widget)
        self.setCentralWidget(central_widget)

        self._wire_events()
        self._render_state(self._controller.get_state())

    def _wire_events(self) -> None:
        if self.referee_controls is not None:
            self.referee_controls.pause_requested.connect(self._controller.pause_playback)
            self.referee_controls.rewind_requested.connect(self._controller.rewind_10_seconds)
            self.referee_controls.half_speed_requested.connect(lambda: self._controller.set_playback_rate(0.5))
            self.referee_controls.quarter_speed_requested.connect(
                lambda: self._controller.set_playback_rate(0.25)
            )
            self.referee_controls.live_requested.connect(self._controller.jump_to_live)
            self.referee_controls.replay_current_play_requested.connect(
                self._controller.replay_current_play
            )
            # Phase 12.B: frame-step buttons — read the configured count
            # at click time so a future runtime knob (12.C) can update
            # `settings.replay_frame_step_count` without re-wiring.
            self.referee_controls.step_back_requested.connect(
                lambda: self._controller.step_frames(
                    -self._settings.replay_frame_step_count
                )
            )
            self.referee_controls.step_forward_requested.connect(
                lambda: self._controller.step_frames(
                    +self._settings.replay_frame_step_count
                )
            )
        if self.operator_controls is not None and self._application_coordinator is not None:
            self.operator_controls.long_recording_toggle_requested.connect(
                self._application_coordinator.toggle_long_session_recording
            )
            self.operator_controls.next_play_requested.connect(
                self._application_coordinator.mark_next_play
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
        # Phase 13.B: each widget self-handles its recording-state
        # gating. RefereeControlsWidget toggles replay-related buttons
        # (Replay Play, Step ◀/▶) since replay isn't available outside
        # RECORDING. OperatorControlsWidget toggles Next Play and flips
        # the Start/Stop button label.
        if self.referee_controls is not None:
            self.referee_controls.set_recording_state(state.is_recording)
        if self.operator_controls is not None:
            self.operator_controls.set_recording_state(state.is_recording)
            self.operator_controls.set_recording_label(state.is_recording)
        show_embedded_video = state.current_playback_mode in {
            PlaybackMode.LIVE,
            PlaybackMode.PAUSED,
            PlaybackMode.REPLAY,
        }
        self.video_panel.set_playback_overlay(state.playback_overlay)
        self.video_panel.apply_tile_visibility(
            state.current_playback_mode,
            live_only_window=self._live_only_window,
        )
        self.video_panel.apply_freeze_indicators(state.feeds_in_freeze_frame)
        if not show_embedded_video:
            placeholder_text = state.playback_overlay.status_text or "Waiting for the selected source"
            self.video_panel.set_global_placeholder(placeholder_text)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Release widget bindings when the window closes."""
        self._output_renderer.detach_all()
        super().closeEvent(event)
