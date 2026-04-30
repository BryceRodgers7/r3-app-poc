"""Read-only diagnostics readout sourced from `TelemetryHub`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.core.disk_budget import BudgetVerdict
from app.core.health_events import default_log
from app.core.telemetry import TelemetryHub, latency_snapshots

if TYPE_CHECKING:
    from app.config.settings import AppSettings
    from app.core.application_coordinator import ApplicationCoordinator


class DiagnosticsWidget(QFrame):
    """Compact telemetry summary: app state, per-feed FPS+state, disk,
    recent latency, invalid-transition count.

    Slice 3.B added queue-depth gauges next to each per-feed line.
    Slice 3.C added a banner at the top that surfaces transitional
    `python_push` feeds visibly, escalating to a warning style when
    `app_mode = "production"` so refactors that accidentally regress
    a feed off the native path can't ship silently.
    """

    REFRESH_MS = 1000

    def __init__(
        self,
        hub: TelemetryHub,
        parent: QWidget | None = None,
        *,
        coordinator: "ApplicationCoordinator | None" = None,
        settings: "AppSettings | None" = None,
    ) -> None:
        super().__init__(parent)
        self._hub = hub
        self._coordinator = coordinator
        self._settings = settings

        self._title = QLabel("Diagnostics")
        self._banner_label = QLabel("")
        self._banner_label.setWordWrap(True)
        self._banner_label.setVisible(False)
        self._app_state_label = QLabel("-")
        self._feeds_label = QLabel("-")
        self._disk_label = QLabel("-")
        self._disk_budget_label = QLabel("-")
        self._latency_label = QLabel("-")
        self._replay_lag_label = QLabel("-")
        self._health_label = QLabel("-")
        for label in (
            self._app_state_label,
            self._feeds_label,
            self._disk_label,
            self._disk_budget_label,
            self._latency_label,
            self._replay_lag_label,
            self._health_label,
        ):
            label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self._title)
        layout.addWidget(self._banner_label)
        layout.addWidget(self._app_state_label)
        layout.addWidget(self._feeds_label)
        layout.addWidget(self._disk_label)
        layout.addWidget(self._disk_budget_label)
        layout.addWidget(self._latency_label)
        layout.addWidget(self._replay_lag_label)
        layout.addWidget(self._health_label)

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            """
            QFrame {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border-radius: 6px;
            }
            QLabel {
                color: #d4d4d4;
                font-family: Consolas, "Courier New", monospace;
                font-size: 12px;
            }
            """
        )

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _refresh(self) -> None:
        coord = self._coordinator
        if coord is not None:
            app_state = coord.get_app_state().value
            recording_state = coord._recording_manager.recording_state.state.value
            replay_sm = coord.operator_controller.replay_state
            replay_state = replay_sm.state.value if replay_sm is not None else "-"
            self._app_state_label.setText(
                f"app: {app_state}  rec: {recording_state}  replay: {replay_state}"
            )
        else:
            self._app_state_label.setText("app: (no coordinator)")

        snaps = self._hub.snapshot()
        self._update_pipeline_banner(snaps)
        if not snaps:
            self._feeds_label.setText("(no feeds registered)")
        else:
            lines = []
            for s in snaps:
                fs = self._hub.feed_state(s.feed_id)
                fs_text = fs.state.value if fs is not None else "?"
                mode_text = (
                    "native"
                    if s.pipeline_mode == "native"
                    else f"py-push ({s.python_frames_per_sec:4.1f}/s)"
                )
                lines.append(
                    f"{s.feed_id:<12} {fs_text:<13} {mode_text:<22} "
                    f"src {s.source_fps:5.1f}  prv {s.preview_fps:5.1f}  "
                    f"rec {s.recording_fps:5.1f}  drop {s.dropped_per_sec:4.1f}/s  "
                    f"qprv {s.queue_depth_preview}/{s.queue_max_preview}  "
                    f"qrec {s.queue_depth_recording}/{s.queue_max_recording}"
                )
            self._feeds_label.setText("\n".join(lines))

        disk = self._hub.disk_snapshot()
        if disk is None:
            self._disk_label.setText("disk: (no sample yet)")
        elif not disk.available:
            self._disk_label.setText(f"disk: {disk.path} unavailable")
        else:
            free_gb = disk.free_bytes / (1024.0 ** 3)
            total_gb = disk.total_bytes / (1024.0 ** 3)
            self._disk_label.setText(
                f"disk: {free_gb:.1f}/{total_gb:.1f} GB free  "
                f"write {disk.write_mb_s_estimate:.1f} MB/s"
            )

        self._update_disk_budget_label()

        lat = [s for s in latency_snapshots() if s.count > 0]
        if not lat:
            self._latency_label.setText("latency: (idle)")
        else:
            self._latency_label.setText(
                "\n".join(
                    f"{s.name:<22} n={s.count:<4} avg {s.avg_ms:5.1f}  "
                    f"p95 {s.p95_ms:5.1f}  max {s.max_ms:5.1f} ms"
                    for s in lat
                )
            )

        self._update_replay_lag_label()

        log = default_log()
        self._health_label.setText(
            f"invalid_transitions: {log.category_count('invalid_transition')}  "
            f"feed_lost: {log.category_count('feed_lost')}  "
            f"disk_low: {log.category_count('disk_low')}  "
            f"recording_branch_saturated: "
            f"{log.category_count('recording_branch_saturated')}  "
            f"audio_missing: {log.category_count('audio_missing')}"
        )

    def _update_replay_lag_label(self) -> None:
        """Phase 7.B replay-lag readout. A wedged splitmuxsink shows up
        as `replay lag` growing unboundedly instead of hovering near
        `recording_segment_duration_seconds`."""
        coord = self._coordinator
        if coord is None:
            self._replay_lag_label.setText("replay lag: (no coordinator)")
            return
        ui_state = coord.operator_controller.get_state()
        if not ui_state.replay_available:
            self._replay_lag_label.setText("replay lag: (no segments yet)")
            return
        lag = ui_state.live_lag_behind_replayable_seconds
        self._replay_lag_label.setText(
            f"replay lag: {lag:5.1f}s behind live "
            f"(span {ui_state.replay_buffer_span_seconds:5.1f}s)"
        )

    def _update_disk_budget_label(self) -> None:
        """Render the Phase 7.A `disk: X/Y MB/s est ✓⚠✗` readout."""
        coord = self._coordinator
        assessment = getattr(coord, "disk_budget", None) if coord is not None else None
        if assessment is None:
            self._disk_budget_label.setText("disk budget: (not assessed)")
            self._disk_budget_label.setStyleSheet("")
            return
        glyph, color = {
            BudgetVerdict.OK: ("✓", "#9ece6a"),
            BudgetVerdict.WARN: ("⚠", "#ffd866"),
            BudgetVerdict.OVER_BUDGET: ("✗", "#ff6e6e"),
        }[assessment.verdict]
        self._disk_budget_label.setText(
            f"disk budget: {assessment.estimated_mb_s:.0f}/"
            f"{assessment.budget_mb_s:.0f} MB/s est {glyph}"
        )
        self._disk_budget_label.setStyleSheet(f"QLabel {{ color: {color}; }}")

    def _update_pipeline_banner(self, snaps) -> None:
        """Show / hide the §3.C transitional pipeline banner."""
        transitional = [s for s in snaps if s.pipeline_mode == "python_push"]
        if not transitional:
            self._banner_label.setVisible(False)
            self._banner_label.setText("")
            return
        feed_list = ", ".join(s.feed_id for s in transitional)
        is_production = (
            self._settings is not None
            and getattr(self._settings, "app_mode", "development") == "production"
        )
        if is_production:
            self._banner_label.setStyleSheet(
                "QLabel { color: #ffd866; "
                "background-color: #4a2a00; padding: 4px; border-radius: 4px; }"
            )
            self._banner_label.setText(
                f"⚠ python_push pipeline active for: {feed_list}. "
                f"app_mode=production expects native; preview is GIL-bound."
            )
        else:
            self._banner_label.setStyleSheet(
                "QLabel { color: #a0a0a0; padding: 2px 4px; }"
            )
            self._banner_label.setText(
                f"pipeline=python_push (transitional) for: {feed_list}"
            )
        self._banner_label.setVisible(True)
