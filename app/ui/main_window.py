"""Main application window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import AppSettings

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.application_coordinator import ApplicationCoordinator
from app.core.app_state import UiState
from app.core.models import FeedDefinition, PlaybackMode
from app.core.playback_controller import PlaybackController
from app.media.output_renderer import MultiFeedOutputRenderer
from app.ui.alert_banner import AlertBanner
from app.ui.camera_visibility_ribbon import CameraVisibilityRibbon
from app.ui.diagnostics_widget import DiagnosticsWidget
from app.ui.jog_wheel import JogWheel
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
      transport (Phase 14.C: Play/Pause, 2x, 1/2x, 1/4x, 1/8x,
      Rewind Ns, Step ◀/▶, plus a jog wheel + play-number badge per
      Phase 14.F). Default to keep older test callers that don't pass
      the kwarg working as before.
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
        show_diagnostics: bool | None = None,
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
        # Phase 14.F: gate the legacy diagnostic chrome
        # (StatusBarWidget + DiagnosticsWidget). When None, inherit
        # from `settings.ui_show_diagnostics`; explicit True/False
        # overrides for tests.
        self._show_diagnostics = (
            settings.ui_show_diagnostics if show_diagnostics is None else show_diagnostics
        )
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
                button_height=settings.touch_button_height,
                rewind_seconds=int(settings.replay_rewind_seconds),
                parent=self,
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
        # Phase 14.B/14.C/14.F: per-window chrome. Both windows get
        # their own CameraVisibilityRibbon — per-spec, hiding a feed on
        # one window does NOT hide it on the other. The operator window
        # adds a Post-process & Exit link + a free-floating Play/Clip
        # counter overlay; the referee window adds a jog wheel
        # (Phase 14.F, replaces the Phase 14.C slider) and a
        # play-number badge.
        self.camera_ribbon: CameraVisibilityRibbon | None = None
        self.post_process_link: QLabel | None = None
        self.jog_wheel: JogWheel | None = None
        if controls_role == "operator":
            self.camera_ribbon = CameraVisibilityRibbon(enabled_feeds, self)
            self.post_process_link = self._build_post_process_link()
        elif controls_role == "referee":
            self.camera_ribbon = CameraVisibilityRibbon(enabled_feeds, self)
            self.jog_wheel = JogWheel(self)

        # Phase 14.F: StatusBarWidget is the legacy diagnostic strip
        # (recording state / source name / behind-live counter). The
        # production windows hide it; developers can flip
        # `[ui] show_diagnostics = true` to bring it back.
        self.status_widget: StatusBarWidget | None = (
            StatusBarWidget(self) if self._show_diagnostics else None
        )
        # The Qt QStatusBar is always present — it's the only sink for
        # transient transport messages (rewind status, post-process
        # placeholder, "Held at end of play (challenge)", etc.).
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        # DiagnosticsWidget is referee-only (telemetry / health-event
        # panel). Also gated by Phase 14.F's diagnostics-chrome flag.
        self.diagnostics_widget: DiagnosticsWidget | None = None
        if (
            self._show_diagnostics
            and controls_role == "referee"
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
        if controls_role == "operator":
            # Phase 14.B operator layout:
            #   [ video panel | OperatorControlsWidget (right column) ]
            #   [ CameraVisibilityRibbon | stretch | Post-process link ]
            middle_row = QHBoxLayout()
            middle_row.setContentsMargins(0, 0, 0, 0)
            middle_row.setSpacing(16)
            middle_row.addWidget(self.video_panel, stretch=1)
            assert self.operator_controls is not None  # set above
            self.operator_controls.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
            )
            self.operator_controls.setMinimumWidth(220)
            middle_row.addWidget(self.operator_controls)
            layout.addLayout(middle_row, stretch=1)

            bottom_row = QHBoxLayout()
            bottom_row.setContentsMargins(0, 0, 0, 0)
            bottom_row.setSpacing(16)
            if self.camera_ribbon is not None:
                bottom_row.addWidget(self.camera_ribbon)
            bottom_row.addStretch(1)
            if self.post_process_link is not None:
                bottom_row.addWidget(self.post_process_link)
            layout.addLayout(bottom_row)
        else:
            # Phase 14.C / 14.F referee layout:
            #   [ video panel                                   ]
            #   [ RefereeControlsWidget transport row | JogWheel ]
            #   [ CameraVisibilityRibbon                        ]
            # The jog wheel sits beside the transport buttons because
            # both controls are part of the same gated transport
            # surface (Phase 14.F).
            layout.addWidget(self.video_panel, stretch=1)
            transport_row = QHBoxLayout()
            transport_row.setContentsMargins(0, 0, 0, 0)
            transport_row.setSpacing(16)
            if self.referee_controls is not None:
                transport_row.addWidget(self.referee_controls, stretch=1)
            if self.jog_wheel is not None:
                transport_row.addWidget(self.jog_wheel)
            layout.addLayout(transport_row)
            if self.camera_ribbon is not None:
                layout.addWidget(self.camera_ribbon)
        if self.status_widget is not None:
            layout.addWidget(self.status_widget)
        if self.diagnostics_widget is not None:
            layout.addWidget(self.diagnostics_widget)
        self.setCentralWidget(central_widget)

        self._wire_events()
        self._render_state(self._controller.get_state())

    def _wire_events(self) -> None:
        if self.referee_controls is not None:
            # Phase 14.C: Pause button is a toggle — from a paused/live
            # position press = "Play" (resume at 1×); from active
            # replay press = "Pause" at current position. Branch on
            # current `current_playback_mode` (set by `_render_state`).
            self.referee_controls.pause_requested.connect(self._on_pause_button_pressed)
            self.referee_controls.rewind_requested.connect(
                self._controller.rewind_configured_seconds
            )
            self.referee_controls.speed_2x_requested.connect(
                lambda: self._controller.set_playback_rate(2.0)
            )
            self.referee_controls.half_speed_requested.connect(
                lambda: self._controller.set_playback_rate(0.5)
            )
            self.referee_controls.quarter_speed_requested.connect(
                lambda: self._controller.set_playback_rate(0.25)
            )
            self.referee_controls.eighth_speed_requested.connect(
                lambda: self._controller.set_playback_rate(0.125)
            )
            # Phase 12.B: frame-step buttons — read the configured count
            # at click time so a future runtime knob (12.C) can update
            # `settings.replay_frame_step_count` without re-wiring.
            # `step_frames_button` applies the step-button auto-resume
            # policy (default: stay paused).
            self.referee_controls.step_back_requested.connect(
                lambda: self._controller.step_frames_button(
                    -self._settings.replay_frame_step_count
                )
            )
            self.referee_controls.step_forward_requested.connect(
                lambda: self._controller.step_frames_button(
                    +self._settings.replay_frame_step_count
                )
            )
        if self.jog_wheel is not None:
            # Phase 14.F: each degree of rotation seeks by `frames_per_degree`
            # frames (default 1). step_frames already clamps to clip-bounds
            # (Phase 14.D) and to the available replay range, so the wheel
            # is a pure seek source — no clamping logic at the widget.
            self.jog_wheel.seek_by_frames_requested.connect(
                self._controller.step_frames
            )
            # Auto-resume policy: capture the pre-jog rate on touch and
            # (per config) resume it once the wheel settles.
            self.jog_wheel.jog_engaged.connect(self._controller.begin_jog)
            self.jog_wheel.jog_released.connect(self._controller.end_jog)
        # Phase 14.F: the challenge-state signal gates the referee
        # transport (every transport button + the jog wheel) — disabled
        # outside a challenge, enabled while one is active. The
        # coordinator owns the state (one challenge across both
        # windows), so the signal lives on the coordinator's bus, not
        # on the per-controller AppSignals.
        if (
            self._application_coordinator is not None
            and self._controls_role == "referee"
        ):
            self._application_coordinator.signals.challenge_state_changed.connect(
                self._set_referee_transport_enabled
            )
        if self.operator_controls is not None and self._application_coordinator is not None:
            self.operator_controls.long_recording_toggle_requested.connect(
                self._application_coordinator.toggle_long_session_recording
            )
            self.operator_controls.next_play_requested.connect(
                self._application_coordinator.mark_next_play
            )
            # Phase 14.B: Time-out / Challenge / Mark Play route into
            # the 14.A coordinator pass-throughs. Gating is enforced
            # both client-side (button enable/disable via
            # `set_clip_state`) and server-side (ClipManager rejects
            # back-to-back challenges + pre-play timeouts).
            self.operator_controls.timeout_requested.connect(
                self._application_coordinator.mark_timeout
            )
            self.operator_controls.challenge_requested.connect(
                self._application_coordinator.mark_challenge
            )
            self.operator_controls.mark_play_toggle_requested.connect(
                self._application_coordinator.toggle_clip_mark
            )
        if self.camera_ribbon is not None:
            self.camera_ribbon.feed_visibility_toggled.connect(
                self.video_panel.set_tile_visible
            )
        self._controller.signals.state_changed.connect(self._render_state)
        self._controller.signals.status_message.connect(self._status_bar.showMessage)

    def _render_state(self, state: UiState) -> None:
        if self.status_widget is not None:
            self.status_widget.update_state(state)
            if self._application_coordinator is not None:
                self.status_widget.set_app_state_summary(
                    self._application_coordinator.get_app_state().value,
                    self._application_coordinator._recording_manager.recording_state.state.value,
                )
        # Phase 13.B: each widget self-handles its recording-state
        # gating. OperatorControlsWidget toggles Next Play and flips the
        # Start/Stop button label. The referee transport is gated by
        # Phase 14.F's challenge-state hook (`_set_referee_transport_enabled`),
        # not by recording state — challenge implies recording, so the
        # narrower gate is enough.
        if self.referee_controls is not None:
            self.referee_controls.set_pause_label_for_rate(
                state.playback_overlay.playback_rate
            )
        if self.operator_controls is not None:
            self.operator_controls.set_recording_state(state.is_recording)
            self.operator_controls.set_recording_label(state.is_recording)
            # Phase 14.B: gate Time-out / Challenge / Mark Play on
            # whether a play has started and on the current clip type.
            self.operator_controls.set_clip_state(
                is_recording=state.is_recording,
                has_play_started=state.current_play_number is not None,
                current_clip_type=state.current_clip_type,
            )
        if self.camera_ribbon is not None:
            self.camera_ribbon.set_selector_label(
                clip_type=state.current_clip_type,
                clip_number=state.current_clip_number,
                play_number=state.current_play_number,
            )
        show_embedded_video = state.current_playback_mode in {
            PlaybackMode.LIVE,
            PlaybackMode.PAUSED,
            PlaybackMode.REPLAY,
        }
        self.video_panel.apply_tile_visibility(
            state.current_playback_mode,
            live_only_window=self._live_only_window,
        )
        if not show_embedded_video:
            placeholder_text = state.playback_overlay.status_text or "Waiting for the selected source"
            self.video_panel.set_global_placeholder(placeholder_text)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Release widget bindings when the window closes."""
        self._output_renderer.detach_all()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Phase 14.F helpers
    # ------------------------------------------------------------------

    def _set_referee_transport_enabled(self, active: bool) -> None:
        """Gate every referee-window transport control on challenge state.

        Wired to `ApplicationCoordinator.signals.challenge_state_changed`
        for `controls_role == "referee"` windows only. The operator window
        has no referee transport, so the signal is a no-op there.
        """
        if self.referee_controls is not None:
            self.referee_controls.set_transport_enabled(active)
        if self.jog_wheel is not None:
            self.jog_wheel.set_wheel_enabled(active)

    # ------------------------------------------------------------------
    # Phase 14.B helpers
    # ------------------------------------------------------------------

    def _build_post_process_link(self) -> QLabel:
        """Bottom-right "Post-process & Exit" hyperlink (Phase 14.B).

        Click handler is a placeholder in 14.B: it logs the intent
        and emits a status-bar message. Phase 14.E swaps the handler
        for the in-app processor + progress modal flow.
        """
        link = QLabel(
            '<a href="post_process_exit" '
            'style="color: #6daffe; font-size: 14px; font-weight: 600;">'
            "Post-process &amp; Exit"
            "</a>",
            self,
        )
        link.setOpenExternalLinks(False)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        link.linkActivated.connect(self._on_post_process_link_clicked)
        return link

    def _on_post_process_link_clicked(self, _href: str) -> None:
        """Phase 14.E: stop recording (if active) and run the in-app
        post-session processor in a modal progress dialog.

        On success: the dialog's "Close" button quits the app via
        `QApplication.quit()` (which closes both windows and routes
        through `aboutToQuit → coordinator.shutdown`). On failure:
        the operator sees the error message and clicks "OK"; the app
        still quits — they re-run from the CLI per the existing
        manual workflow.
        """
        from app.ui.post_process_dialog import PostProcessDialog

        coord = self._application_coordinator
        if coord is None:
            LOGGER.warning(
                "Post-process & Exit clicked with no coordinator attached"
            )
            return
        # Stop recording synchronously if a game is in flight. Phase
        # 14.E spec: "Block on the recording-stopped signal — the
        # post-processor only sees finalized segments."
        # toggle_long_session_recording runs the full stop sequence
        # synchronously (disable_file_recording → ClipManager.stop_game
        # → state transitions) before returning, so a single call is
        # enough — no explicit signal wait needed.
        if coord._recording_manager.is_any_recording():
            LOGGER.info("Post-process & Exit: stopping recording first")
            coord.toggle_long_session_recording()
        session_paths = coord._session_manager.get_active_session_paths()
        if session_paths is None:
            LOGGER.warning(
                "Post-process & Exit: no active session — nothing to process"
            )
            self._status_bar.showMessage(
                "No active session to process.", 4000
            )
            return
        dialog = PostProcessDialog(
            session_path=session_paths.root_dir,
            metadata_db_path=self._settings.metadata_db_path,
            parent=self,
        )
        dialog.start()
        dialog.exec()

    def _on_pause_button_pressed(self) -> None:
        """Phase 14.C: Play/Pause toggle handler.

        From PAUSED (rate==0): resume at 1.0×. From REPLAY/LIVE: pause
        at the current playback position. Routes through the existing
        controller primitives; the controller's gating handles the
        live-only / pre-recording cases.
        """
        state = self._controller.get_state()
        if state.playback_overlay.playback_rate <= 0.0:
            self._controller.set_playback_rate(1.0)
        else:
            self._controller.pause_playback()

