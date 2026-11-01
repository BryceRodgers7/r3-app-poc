"""Seek bar for the clip under review on the referee window.

The bar spans exactly the challenge fence, so every point on it is a
place the referee is allowed to go, and it reads as elapsed / total
time within that clip. It is interactive only during a challenge, the
same gate as the rest of the referee transport (Phase 14.F).

A seek renders every feed synchronously on the UI thread, so a drag can
outrun it. Mouse moves only note where the handle is; a short timer
seeks to the latest position, so moves that queue up behind a slow seek
collapse into one. The release always lands on the exact final position.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.control_button import PRESS_FEEDBACK_COLOR


_BAR_HEIGHT_PX = 36
_TRACK_HEIGHT_PX = 6
_HANDLE_RADIUS_PX = 9
_LABEL_GAP_PX = 12
_SEEK_INTERVAL_MS = 33
_NS_PER_TENTH = 100_000_000
_TEXT_COLOR = "#f4f6f8"
_MUTED_COLOR = "#6b7480"
_TRACK_COLOR = "#2f3742"
_DISABLED_TRACK_COLOR = "#22272e"
_PLAYED_COLOR = "#1f6feb"


def format_clip_time(duration_ns: int) -> str:
    """Render a time within the clip as `m:ss.t`."""
    minutes, tenths = divmod(max(0, duration_ns) // _NS_PER_TENTH, 600)
    return f"{minutes}:{tenths / 10:04.1f}"


class ReviewSeekBar(QWidget):
    """Horizontal seek bar over one clip's session-time range.

    Gesture lifecycle matches `JogWheel`: `seek_started` fires on press
    and `seek_finished` on release, so the controller can capture the
    playback rate and decide whether to resume.
    """

    # qint64: session-time exceeds the 32-bit range of `Signal(int)`.
    seek_requested = Signal("qint64")
    seek_started = Signal()
    seek_finished = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_BAR_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)

        self._start_ns: int | None = None
        self._end_ns: int | None = None
        self._position_ns: int | None = None
        self._dragging = False
        self._pending_seek_ns: int | None = None
        # Same Phase 14.F gate as the jog wheel: off until a challenge opens.
        self._bar_enabled = False

        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(_SEEK_INTERVAL_MS)
        self._seek_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._seek_timer.timeout.connect(self._emit_pending_seek)

    def set_bar_enabled(self, active: bool) -> None:
        """Enable or disable input (Phase 14.F gate).

        Disabling abandons any drag without emitting `seek_finished`:
        a challenge ending shouldn't trigger an auto-resume.
        """
        if active == self._bar_enabled:
            return
        self._bar_enabled = active
        if not active:
            self._dragging = False
            self._seek_timer.stop()
            self._pending_seek_ns = None
        self.update()

    def is_bar_enabled(self) -> bool:
        return self._bar_enabled

    def update_review(
        self,
        review_range_ns: tuple[int, int] | None,
        position_ns: int | None,
    ) -> None:
        """Refresh from `UiState`. A drag in progress keeps its handle."""
        start_ns, end_ns = review_range_ns if review_range_ns is not None else (None, None)
        position = self._position_ns if self._dragging else position_ns
        current = (self._start_ns, self._end_ns, self._position_ns)
        if (start_ns, end_ns, position) == current:
            return  # runs on every UiState tick
        self._start_ns, self._end_ns, self._position_ns = start_ns, end_ns, position
        self.update()

    def _label_width(self) -> int:
        return self.fontMetrics().horizontalAdvance("00:00.0 / 00:00.0")

    def _track_span(self) -> tuple[float, float]:
        left = float(_HANDLE_RADIUS_PX)
        right = self.width() - self._label_width() - _LABEL_GAP_PX - _HANDLE_RADIUS_PX
        return left, max(left + 1.0, float(right))

    def _has_clip(self) -> bool:
        return (
            self._start_ns is not None
            and self._end_ns is not None
            and self._end_ns > self._start_ns
        )

    def _position_at(self, x: float) -> int:
        assert self._start_ns is not None and self._end_ns is not None
        left, right = self._track_span()
        fraction = max(0.0, min(1.0, (x - left) / (right - left)))
        return self._start_ns + round((self._end_ns - self._start_ns) * fraction)

    def _played_fraction(self) -> float:
        if not self._has_clip() or self._position_ns is None:
            return 0.0
        assert self._start_ns is not None and self._end_ns is not None
        played = (self._position_ns - self._start_ns) / (self._end_ns - self._start_ns)
        return max(0.0, min(1.0, played))

    def _move_handle(self, x: float) -> None:
        self._position_ns = self._position_at(x)
        self._pending_seek_ns = self._position_ns
        self.update()

    def _drag_to(self, x: float) -> None:
        self._move_handle(x)
        if not self._seek_timer.isActive():
            self._seek_timer.start()

    def _emit_pending_seek(self) -> None:
        if self._pending_seek_ns is None:
            return
        target_ns = self._pending_seek_ns
        self._pending_seek_ns = None
        self.seek_requested.emit(target_ns)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            not self._bar_enabled
            or not self._has_clip()
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return
        self._dragging = True
        self.seek_started.emit()
        self._drag_to(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            return
        self._drag_to(event.position().x())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        self._seek_timer.stop()
        self._move_handle(event.position().x())
        self._emit_pending_seek()
        self.seek_finished.emit()
        event.accept()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self._bar_enabled and self._has_clip()
        left, right = self._track_span()
        middle = self.height() / 2
        track = QRectF(left, middle - _TRACK_HEIGHT_PX / 2, right - left, _TRACK_HEIGHT_PX)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_TRACK_COLOR if active else _DISABLED_TRACK_COLOR))
        painter.drawRoundedRect(track, _TRACK_HEIGHT_PX / 2, _TRACK_HEIGHT_PX / 2)
        if not active:
            return
        assert self._start_ns is not None and self._end_ns is not None
        played = track.width() * self._played_fraction()
        painter.setBrush(QColor(_PLAYED_COLOR))
        painter.drawRoundedRect(
            QRectF(track.left(), track.top(), played, track.height()),
            _TRACK_HEIGHT_PX / 2,
            _TRACK_HEIGHT_PX / 2,
        )
        painter.setBrush(QColor(PRESS_FEEDBACK_COLOR if self._dragging else _TEXT_COLOR))
        radius = float(_HANDLE_RADIUS_PX)
        painter.drawEllipse(
            QRectF(left + played - radius, middle - radius, radius * 2, radius * 2)
        )
        elapsed_ns = (self._position_ns or self._start_ns) - self._start_ns
        total_ns = self._end_ns - self._start_ns
        painter.setPen(QColor(_TEXT_COLOR))
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            f"{format_clip_time(elapsed_ns)} / {format_clip_time(total_ns)}",
        )
