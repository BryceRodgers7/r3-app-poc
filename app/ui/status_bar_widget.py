"""Status panel for high-level replay state."""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QLabel, QFrame, QWidget

from app.core.app_state import AppState


class StatusBarWidget(QFrame):
    """Displays current mode, recording, source, session, and detail status."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode_value = QLabel("-")
        self.recording_value = QLabel("-")
        self.source_value = QLabel("-")
        self.session_value = QLabel("-")
        self.detail_value = QLabel("-")

        layout = QGridLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(8)

        labels = (
            ("Mode", self.mode_value),
            ("Recording", self.recording_value),
            ("Source", self.source_value),
            ("Session", self.session_value),
            ("Detail", self.detail_value),
        )
        for row, (title, value_label) in enumerate(labels):
            layout.addWidget(QLabel(f"{title}:"), row, 0)
            layout.addWidget(value_label, row, 1)

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            """
            QFrame {
                background-color: #f2f2f2;
                border-radius: 8px;
            }
            QLabel {
                font-size: 14px;
            }
            """
        )

    def update_state(self, state: AppState) -> None:
        """Refresh all labels from the latest application state."""
        if state.current_playback_mode.value == "REPLAY":
            self.mode_value.setText(f"REPLAY -{state.seconds_behind_live:.0f}s")
        else:
            self.mode_value.setText(state.current_playback_mode.value.replace("_", " "))

        self.recording_value.setText("RECORDING" if state.is_recording else "IDLE")
        self.source_value.setText(
            f"{state.current_source_name or 'Unknown'} ({'CONNECTED' if state.source_connected else 'DISCONNECTED'})"
        )
        self.session_value.setText(state.current_session_id or "No session")

        if state.error_message:
            self.detail_value.setText(state.error_message)
        elif state.warning_message:
            self.detail_value.setText(state.warning_message)
        elif state.current_playback_mode.value == "PAUSED":
            self.detail_value.setText("Playback frozen while ingest and recording continue")
        elif state.current_playback_mode.value == "REPLAY":
            self.detail_value.setText(
                f"Rolling buffer contains about {state.replay_buffer_span_seconds:.0f}s"
            )
        else:
            self.detail_value.setText("Showing newest live frame")
