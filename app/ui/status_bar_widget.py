"""Status panel for high-level replay state."""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QLabel, QFrame, QWidget

from app.core.app_state import UiState


class StatusBarWidget(QFrame):
    """Displays current mode, recording, source, session, and detail status."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app_state_value = QLabel("-")
        self.mode_value = QLabel("-")
        self.recording_value = QLabel("-")
        self.play_value = QLabel("-")
        self.source_value = QLabel("-")
        self.ingest_value = QLabel("-")
        self.ingest_value.setWordWrap(True)
        self.session_value = QLabel("-")
        self.replay_value = QLabel("-")
        self.detail_value = QLabel("-")

        layout = QGridLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(8)

        labels = (
            ("App State", self.app_state_value),
            ("Mode", self.mode_value),
            ("Recording", self.recording_value),
            ("Play", self.play_value),
            ("Source", self.source_value),
            ("Ingest", self.ingest_value),
            ("Session", self.session_value),
            ("Replay", self.replay_value),
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

    def set_app_state_summary(self, app_state: str, recording_state: str) -> None:
        """Set the §10.1 top-level state and §10.3 recording-state strings."""
        self.app_state_value.setText(f"{app_state}  (rec: {recording_state})")

    def update_state(self, state: UiState) -> None:
        """Refresh all labels from the latest application state."""
        if state.current_playback_mode.value == "REPLAY":
            self.mode_value.setText(f"REPLAY -{state.seconds_behind_live:.0f}s")
        else:
            self.mode_value.setText(state.current_playback_mode.value.replace("_", " "))

        self.recording_value.setText("RECORDING" if state.is_recording else "IDLE")
        self.play_value.setText(_format_play_badge(state))
        self.source_value.setText(
            f"{state.current_source_name or 'Unknown'} ({'CONNECTED' if state.source_connected else 'DISCONNECTED'})"
        )
        if state.ingest_telemetry is not None:
            self.ingest_value.setText(state.ingest_telemetry.summary_line())
        else:
            self.ingest_value.setText("-")
        self.session_value.setText(state.current_session_id or "No session")
        self.replay_value.setText(_format_replay_coverage(state))

        if state.error_message:
            self.detail_value.setText(state.error_message)
        elif state.warning_message:
            self.detail_value.setText(state.warning_message)
        elif state.current_playback_mode.value == "PAUSED":
            self.detail_value.setText("Playback frozen while ingest and recording continue")
        elif state.current_playback_mode.value == "REPLAY":
            self.detail_value.setText(
                f"Replay at {state.playback_overlay.playback_rate:0.2f}x "
                f"(~{state.seconds_behind_live:.0f}s behind live)"
            )
        else:
            self.detail_value.setText("Showing newest live frame")


def _format_play_badge(state: UiState) -> str:
    """Status-bar Play row.

    Shows `Play #N` when a play has been opened in the current game
    (most-recent play number; carries through timeouts / challenges
    so the badge doesn't flicker). Reads "—" outside of recording
    and during pre-game (before the first Next Play press).
    """
    if state.current_play_number is None:
        return "—"
    return f"Play #{state.current_play_number}"


def _format_mmss(seconds: float) -> str:
    """Render a non-negative duration in M:SS."""
    if seconds < 0:
        seconds = 0.0
    total_seconds = int(seconds)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _format_replay_coverage(state: UiState) -> str:
    """Phase 7.B status-bar replay-coverage indicator."""
    if not state.replay_available:
        if state.is_recording:
            return "not yet available — first segment finalizing"
        return "not available — start a game recording"
    latest_ns = state.latest_replayable_session_time_ns
    assert latest_ns is not None  # implied by replay_available
    latest_s = latest_ns / 1_000_000_000.0
    earliest_s = max(0.0, latest_s - state.replay_buffer_span_seconds)
    return (
        f"covers {_format_mmss(earliest_s)} – {_format_mmss(latest_s)} "
        f"(latest finalized −{state.live_lag_behind_replayable_seconds:.0f}s)"
    )
