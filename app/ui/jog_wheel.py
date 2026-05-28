"""Jog wheel for the referee window (Phase 14.F).

Replaces the Phase 14.C `ScrubberSlider` per the original spec in
`docs/window-requirements.md` line 60 ("inertia wheel ... 1 degree =
1 frame").

Interaction model:
  - Mouse-drag only. Click and drag in a circle around the wheel.
    Each degree of rotation emits a `seek_by_frames_requested(int)`
    with +1 (forward) or -1 (backward).
  - Sub-degree motion accumulates and emits when it crosses an
    integer boundary — keeps signal traffic proportional to motion,
    not to mouse-event rate.
  - On release, the wheel coasts with angular velocity proportional
    to the most recent drag sample. The coast emits frame seeks
    while it decays. Decay is an exponential per-tick factor.
  - All input is gated by Phase 14.F: the wheel is interactive only
    while a challenge is active. Outside a challenge it is greyed
    out and ignores mouse events.

The wheel is a pure seek source. It does not consume playback
position from the controller; the controller's `step_frames`
primitive already clamps to the clip-bounds and replayable range.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget


_WHEEL_SIZE_PX = 140
_COAST_INTERVAL_MS = 16  # ~60 Hz repaint + emit cadence
_COAST_DECAY_PER_TICK = 0.95  # ~225 ms half-life at 16 ms tick
_COAST_STOP_THRESHOLD_RAD_S = math.radians(5.0)  # 5 °/s
_VELOCITY_FRESHNESS_NS = 100_000_000  # release ignores velocity older than 100 ms


def _normalize_delta(delta: float) -> float:
    """Wrap an angular delta into (-π, π] so atan2-boundary crossings
    don't manifest as huge spurious rotations."""
    while delta > math.pi:
        delta -= 2 * math.pi
    while delta < -math.pi:
        delta += 2 * math.pi
    return delta


class JogWheel(QWidget):
    """Circular jog wheel: rotate to seek by integer frames.

    1° of rotation = 1 frame. Positive (counter-clockwise) = forward.
    """

    seek_by_frames_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_WHEEL_SIZE_PX, _WHEEL_SIZE_PX)

        # Display state — the indicator notch's rotation.
        self._angle_radians: float = 0.0

        # Drag state.
        self._dragging: bool = False
        self._drag_last_angle: float | None = None
        self._drag_last_delta_radians: float = 0.0
        self._drag_last_time_ns: int = 0

        # Coast (inertia) state.
        self._coast_velocity_rad_s: float = 0.0
        self._coast_timer = QTimer(self)
        self._coast_timer.setInterval(_COAST_INTERVAL_MS)
        self._coast_timer.timeout.connect(self._on_coast_tick)

        # Accumulator: fractional degrees carry across moves so a slow
        # drag still emits cleanly. Emit only on integer crossings.
        self._frame_carry_degrees: float = 0.0

        # Phase 14.F gate — starts disabled (no challenge active at
        # window construction). MainWindow flips this on
        # `challenge_state_changed`.
        self._wheel_enabled: bool = False
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_wheel_enabled(self, active: bool) -> None:
        """Enable or disable mouse-drag input (Phase 14.F gate).

        Disabling cancels any in-flight coast, clears the carry
        accumulator (so a fresh challenge starts clean), and forces a
        re-paint so the greyed appearance shows immediately.
        """
        if active == self._wheel_enabled:
            return
        self._wheel_enabled = active
        if not active:
            self._stop_coast()
            self._dragging = False
            self._drag_last_angle = None
            self._frame_carry_degrees = 0.0
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update()

    def is_wheel_enabled(self) -> bool:
        return self._wheel_enabled

    # ------------------------------------------------------------------
    # Input — mouse drag only (per 14.F spec)
    # ------------------------------------------------------------------

    def _angle_from_pos(self, pos: QPointF) -> float | None:
        """Cursor angle around the wheel center, or None if outside
        the wheel's bounding circle."""
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        dx = pos.x() - cx
        dy = pos.y() - cy
        radius = min(cx, cy)
        if dx * dx + dy * dy > radius * radius:
            return None
        # Qt's Y axis grows downward — flip dy so a clockwise drag at
        # the screen reads as a negative (backward) angular delta.
        return math.atan2(-dy, dx)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._wheel_enabled:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        angle = self._angle_from_pos(event.position())
        if angle is None:
            return
        self._stop_coast()
        self._dragging = True
        self._drag_last_angle = angle
        self._drag_last_delta_radians = 0.0
        self._drag_last_time_ns = time.monotonic_ns()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._wheel_enabled or not self._dragging:
            return
        if self._drag_last_angle is None:
            return
        angle = self._angle_from_pos(event.position())
        if angle is None:
            return
        delta = _normalize_delta(angle - self._drag_last_angle)
        now_ns = time.monotonic_ns()
        self._apply_rotation(delta)
        self._drag_last_angle = angle
        self._drag_last_delta_radians = delta
        self._drag_last_time_ns = now_ns
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        self._drag_last_angle = None
        # Velocity is the last drag sample's rate. Stale samples (the
        # user paused mid-drag before releasing) coast to zero.
        now_ns = time.monotonic_ns()
        dt_ns = max(1, now_ns - self._drag_last_time_ns)
        if dt_ns > _VELOCITY_FRESHNESS_NS:
            velocity = 0.0
        else:
            velocity = self._drag_last_delta_radians / (dt_ns / 1e9)
        if abs(velocity) > _COAST_STOP_THRESHOLD_RAD_S and self._wheel_enabled:
            self._coast_velocity_rad_s = velocity
            self._coast_timer.start()
        event.accept()

    # ------------------------------------------------------------------
    # Rotation + seek emission
    # ------------------------------------------------------------------

    def _apply_rotation(self, delta_radians: float) -> None:
        """Advance the indicator and emit any integer-frame seek that
        crosses during this delta."""
        self._angle_radians += delta_radians
        degrees = math.degrees(delta_radians)
        self._frame_carry_degrees += degrees
        whole = int(self._frame_carry_degrees)
        if whole != 0:
            self._frame_carry_degrees -= whole
            self.seek_by_frames_requested.emit(whole)
        self.update()

    def _on_coast_tick(self) -> None:
        dt_s = _COAST_INTERVAL_MS / 1000.0
        delta = self._coast_velocity_rad_s * dt_s
        self._apply_rotation(delta)
        self._coast_velocity_rad_s *= _COAST_DECAY_PER_TICK
        if abs(self._coast_velocity_rad_s) < _COAST_STOP_THRESHOLD_RAD_S:
            self._stop_coast()

    def _stop_coast(self) -> None:
        if self._coast_timer.isActive():
            self._coast_timer.stop()
        self._coast_velocity_rad_s = 0.0

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._wheel_enabled:
            rim_color = QColor("#6daffe")
            face_color = QColor("#2a3340")
            tick_color = QColor("#cdd6e0")
            indicator_color = QColor("#ffffff")
        else:
            rim_color = QColor("#445060")
            face_color = QColor("#1c2027")
            tick_color = QColor("#4a5060")
            indicator_color = QColor("#6a727f")
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        outer_r = min(cx, cy) - 4
        inner_r = outer_r - 12
        painter.setBrush(face_color)
        painter.setPen(QPen(rim_color, 3))
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(outer_r), int(outer_r))
        painter.setPen(QPen(tick_color, 2))
        for deg in range(0, 360, 15):
            theta = math.radians(deg) + self._angle_radians
            x0 = cx + math.cos(theta) * inner_r
            y0 = cy - math.sin(theta) * inner_r
            x1 = cx + math.cos(theta) * outer_r
            y1 = cy - math.sin(theta) * outer_r
            painter.drawLine(int(x0), int(y0), int(x1), int(y1))
        # Indicator notch: a thicker radial line so the user can see
        # the wheel rotate. Starts at top (π/2) and rotates with the
        # accumulated angle.
        painter.setPen(QPen(indicator_color, 3))
        theta = math.pi / 2 + self._angle_radians
        x_inner = cx + math.cos(theta) * (inner_r - 8)
        y_inner = cy - math.sin(theta) * (inner_r - 8)
        x_outer = cx + math.cos(theta) * inner_r
        y_outer = cy - math.sin(theta) * inner_r
        painter.drawLine(int(x_inner), int(y_inner), int(x_outer), int(y_outer))
