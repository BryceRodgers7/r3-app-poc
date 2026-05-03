"""Operator alert banner (Phase 10.A) — surfaces §11.3 warnings.

Polls the process-wide health log on a 1s timer; when one or more open
events match the §11.3 operator-visible-warnings allowlist, shows the
most-severe one as a colored bar with a count badge for additional
concurrent open events. Hides when nothing relevant is open.

Diagnostic-only categories (`audio_missing`, `invalid_transition`,
`feed_recovered`, `disk_recovered`, `recording_started`,
`recording_stopped`, `session_finalized`) deliberately stay out of the
banner; they live in `health_events.jsonl` and the diagnostics widget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from app.core.health_events import (
    HealthEvent,
    HealthEventLog,
    HealthSeverity,
    default_log,
)


_OPERATOR_VISIBLE_CATEGORIES: dict[str, str] = {
    "feed_lost": "Feed disconnected",
    "feed_failed_permanent": "Feed unavailable — reconnect attempts exhausted",
    "feed_degraded": "Recording degraded",
    "replay_degraded": "Replay unavailable",
    "disk_low": "Disk nearly full",
    "recording_branch_saturated": "Disk too slow — recording degraded",
    "recording_error": "Encoder failure",
    "session_dirty": "Session not safely recording",
}


_SEVERITY_RANK: dict[str, int] = {
    HealthSeverity.ERROR.value: 2,
    HealthSeverity.WARNING.value: 1,
    HealthSeverity.INFO.value: 0,
}


@dataclass(frozen=True)
class BannerState:
    """Pure-data snapshot of what the banner should render right now."""

    visible: bool
    primary: HealthEvent | None
    extra_count: int

    @property
    def severity(self) -> str | None:
        return self.primary.severity if self.primary is not None else None


def select_banner_state(events: Iterable[HealthEvent]) -> BannerState:
    """Pick the most-severe operator-visible event; count the rest.

    `events` is whatever `HealthEventLog.open_events()` returns — the
    function tolerates extra non-operator-visible categories so the
    caller doesn't need to pre-filter. Tie-breaking on severity is by
    most-recent event id (highest id wins), so a fresh ERROR replaces a
    stale ERROR rather than getting stuck behind it.
    """
    relevant = [e for e in events if e.category in _OPERATOR_VISIBLE_CATEGORIES]
    if not relevant:
        return BannerState(visible=False, primary=None, extra_count=0)
    primary = max(
        relevant,
        key=lambda e: (_SEVERITY_RANK.get(e.severity, 0), e.id),
    )
    return BannerState(visible=True, primary=primary, extra_count=len(relevant) - 1)


def format_banner_text(state: BannerState) -> str:
    """Compose the primary label text. Returns "" when not visible."""
    if not state.visible or state.primary is None:
        return ""
    label = _OPERATOR_VISIBLE_CATEGORIES.get(
        state.primary.category, state.primary.category
    )
    feed_id = state.primary.feed_id
    feed_suffix = f" [{feed_id}]" if feed_id else ""
    return f"{label}{feed_suffix} — {state.primary.message}"


def stylesheet_for_severity(severity: str | None) -> str:
    """Return a Qt stylesheet appropriate for the banner's severity."""
    if severity == HealthSeverity.ERROR.value:
        bg, fg = "#5b1414", "#ffe1e1"
    elif severity == HealthSeverity.WARNING.value:
        bg, fg = "#4a2a00", "#ffd866"
    else:
        bg, fg = "#1e1e1e", "#d4d4d4"
    return (
        f"QFrame#alertBannerFrame {{ background-color: {bg}; "
        f"border-radius: 6px; }}"
        f"QLabel {{ color: {fg}; font-weight: 600; font-size: 13px; }}"
    )


class AlertBanner(QFrame):
    """Foreground operator banner for §11.3 health categories."""

    REFRESH_MS = 1000

    def __init__(
        self,
        log: HealthEventLog | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("alertBannerFrame")
        self._log = log if log is not None else default_log()

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._extra_label = QLabel("")
        self._extra_label.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self._extra_label)

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setVisible(False)

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        """Recompute banner state from the current open events."""
        self.apply_state(select_banner_state(self._log.open_events()))

    def apply_state(self, state: BannerState) -> None:
        if not state.visible or state.primary is None:
            self.setVisible(False)
            self._label.setText("")
            self._extra_label.setVisible(False)
            self._extra_label.setText("")
            return
        self._label.setText(format_banner_text(state))
        if state.extra_count > 0:
            self._extra_label.setText(f"+{state.extra_count} more")
            self._extra_label.setVisible(True)
        else:
            self._extra_label.setText("")
            self._extra_label.setVisible(False)
        self.setStyleSheet(stylesheet_for_severity(state.severity))
        self.setVisible(True)
