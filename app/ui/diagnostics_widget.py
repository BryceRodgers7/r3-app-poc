"""Read-only diagnostics readout sourced from `TelemetryHub`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.core.health_events import default_log
from app.core.telemetry import TelemetryHub, latency_snapshots

if TYPE_CHECKING:
    from app.core.application_coordinator import ApplicationCoordinator


class DiagnosticsWidget(QFrame):
    """Compact telemetry summary: app state, per-feed FPS+state, disk,
    recent latency, invalid-transition count."""

    REFRESH_MS = 1000

    def __init__(
        self,
        hub: TelemetryHub,
        parent: QWidget | None = None,
        *,
        coordinator: "ApplicationCoordinator | None" = None,
    ) -> None:
        super().__init__(parent)
        self._hub = hub
        self._coordinator = coordinator

        self._title = QLabel("Diagnostics")
        self._app_state_label = QLabel("-")
        self._feeds_label = QLabel("-")
        self._disk_label = QLabel("-")
        self._latency_label = QLabel("-")
        self._health_label = QLabel("-")
        for label in (
            self._app_state_label,
            self._feeds_label,
            self._disk_label,
            self._latency_label,
            self._health_label,
        ):
            label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self._title)
        layout.addWidget(self._app_state_label)
        layout.addWidget(self._feeds_label)
        layout.addWidget(self._disk_label)
        layout.addWidget(self._latency_label)
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
        if not snaps:
            self._feeds_label.setText("(no feeds registered)")
        else:
            lines = []
            for s in snaps:
                fs = self._hub.feed_state(s.feed_id)
                fs_text = fs.state.value if fs is not None else "?"
                lines.append(
                    f"{s.feed_id:<12} {fs_text:<13} "
                    f"src {s.source_fps:5.1f}  prv {s.preview_fps:5.1f}  "
                    f"rec {s.recording_fps:5.1f}  drop {s.dropped_per_sec:4.1f}/s"
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

        log = default_log()
        self._health_label.setText(
            f"invalid_transitions: {log.category_count('invalid_transition')}  "
            f"feed_lost: {log.category_count('feed_lost')}  "
            f"disk_low: {log.category_count('disk_low')}"
        )
