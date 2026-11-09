"""The "Starting cameras…" window shown while the feeds come up.

After Begin Match (and after a Resume, which skips the pre-match dialog)
the bootstrap builds the coordinator and both windows and starts every
feed. `ApplicationCoordinator.initialize()` runs on the main thread, so
the Qt event loop is blocked for the duration. This window is therefore
deliberately static: it is painted once with `show_and_paint()` and the
progress bar is a plain "busy" bar that is not expected to animate. It
has no buttons and cannot be closed by the operator; the bootstrap closes
it when the Operator and Referee windows are ready.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme


class StartingCamerasWindow(QDialog):
    """Non-interactive progress window listing the cameras being started."""

    def __init__(self, camera_names: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sports Replay")
        # Title bar only: no close, minimize or help buttons.
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setFixedWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)

        self.title_label = QLabel("Starting cameras…")
        self.title_label.setStyleSheet("font-size: 24px; font-weight: 700;")
        count = len(camera_names)
        self.detail_label = QLabel(
            f"Connecting {count} camera{'' if count == 1 else 's'}. "
            "The Operator and Referee screens open next."
        )
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # busy bar: no percentage to report
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)

        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.progress)

        panel = QFrame()
        panel.setObjectName("cameraPanel")
        # Scoped by object name: QLabel is a QFrame, so a bare `QFrame`
        # rule would box every camera label too.
        panel.setStyleSheet(
            f"QFrame#cameraPanel {{ background-color: {theme.SURFACE};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            " QLabel { font-size: 15px; font-weight: 600; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 10, 16, 10)
        panel_layout.setSpacing(8)
        self.camera_labels: list[QLabel] = []
        for name in camera_names:
            label = QLabel(f"○  {name}")
            self.camera_labels.append(label)
            panel_layout.addWidget(label)
        layout.addWidget(panel)

    def show_and_paint(self) -> None:
        """Show the window and let Qt paint it before the caller blocks."""
        self.show()
        # Painting needs the event loop, which the caller is about to
        # block. Two passes cover the native window being created and
        # then painted.
        for _ in range(2):
            QApplication.processEvents()
