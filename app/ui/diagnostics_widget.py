"""Read-only diagnostics readout sourced from `TelemetryHub`."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.core.telemetry import TelemetryHub, latency_snapshots


class DiagnosticsWidget(QFrame):
    """Compact telemetry summary: per-feed FPS, disk, recent latency."""

    REFRESH_MS = 1000

    def __init__(self, hub: TelemetryHub, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hub = hub

        self._title = QLabel("Diagnostics")
        self._feeds_label = QLabel("-")
        self._disk_label = QLabel("-")
        self._latency_label = QLabel("-")
        for label in (self._feeds_label, self._disk_label, self._latency_label):
            label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self._title)
        layout.addWidget(self._feeds_label)
        layout.addWidget(self._disk_label)
        layout.addWidget(self._latency_label)

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
        snaps = self._hub.snapshot()
        if not snaps:
            self._feeds_label.setText("(no feeds registered)")
        else:
            self._feeds_label.setText(
                "\n".join(
                    f"{s.feed_id:<12} src {s.source_fps:5.1f}  prv {s.preview_fps:5.1f}  "
                    f"rec {s.recording_fps:5.1f}"
                    for s in snaps
                )
            )

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
