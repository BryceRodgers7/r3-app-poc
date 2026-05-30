"""Phase 14.F: `JogWheel` widget contract.

The mouse-event tests exercise `_apply_rotation` and `_on_coast_tick`
directly rather than synthesizing Qt mouse events. Both methods are
the testable seams — mouse handlers are thin shims that call them —
and bypassing event synthesis keeps the tests fast and independent
of Qt event-loop quirks.
"""

from __future__ import annotations

import math
import unittest

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from app.ui.jog_wheel import JogWheel


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


def _press(wheel: JogWheel, x: float, y: float) -> None:
    wheel.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _release(wheel: JogWheel, x: float, y: float) -> None:
    wheel.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


class JogWheelConstructionTests(_QtTestCase):
    def test_starts_disabled(self) -> None:
        # Phase 14.F: no challenge is active at startup, so the wheel
        # must start non-interactive.
        wheel = JogWheel()
        self.assertFalse(wheel.is_wheel_enabled())

    def test_starts_with_arrow_cursor(self) -> None:
        wheel = JogWheel()
        self.assertEqual(wheel.cursor().shape(), Qt.CursorShape.ArrowCursor)

    def test_set_wheel_enabled_flips_cursor(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        self.assertEqual(wheel.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        wheel.set_wheel_enabled(False)
        self.assertEqual(wheel.cursor().shape(), Qt.CursorShape.ArrowCursor)


class JogWheelRotationEmissionTests(_QtTestCase):
    def test_one_degree_emits_one_frame_forward(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        wheel._apply_rotation(math.radians(1.0))
        self.assertEqual(emitted, [1])

    def test_negative_rotation_emits_backward(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        wheel._apply_rotation(math.radians(-3.0))
        self.assertEqual(emitted, [-3])

    def test_sub_degree_motion_does_not_emit(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        wheel._apply_rotation(math.radians(0.4))
        wheel._apply_rotation(math.radians(0.4))
        self.assertEqual(emitted, [])

    def test_sub_degree_motion_accumulates_and_emits(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        # 0.6 + 0.6 = 1.2 → emit 1, carry 0.2
        wheel._apply_rotation(math.radians(0.6))
        wheel._apply_rotation(math.radians(0.6))
        self.assertEqual(emitted, [1])
        # +0.9 → carry 1.1 → emit 1, carry 0.1
        wheel._apply_rotation(math.radians(0.9))
        self.assertEqual(emitted, [1, 1])

    def test_large_rotation_emits_integer_part_in_one_signal(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        wheel._apply_rotation(math.radians(12.5))
        self.assertEqual(emitted, [12])
        # Carry of 0.5 lingers; +0.6 crosses 1.0 → emit 1.
        wheel._apply_rotation(math.radians(0.6))
        self.assertEqual(emitted, [12, 1])


class JogWheelInertiaTests(_QtTestCase):
    def test_coast_tick_decays_velocity(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        wheel._coast_velocity_rad_s = math.radians(180.0)  # 180 deg/s
        before = wheel._coast_velocity_rad_s
        wheel._on_coast_tick()
        # Velocity is multiplied by the decay factor each tick.
        self.assertLess(wheel._coast_velocity_rad_s, before)
        self.assertGreater(wheel._coast_velocity_rad_s, 0.0)

    def test_coast_tick_emits_frames_for_significant_rotation(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        # 360 deg/s * 16ms tick = ~5.76 deg per tick → emits 5
        # (carry accounts for fractional 0.76).
        wheel._coast_velocity_rad_s = math.radians(360.0)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        wheel._on_coast_tick()
        self.assertEqual(len(emitted), 1)
        self.assertGreater(emitted[0], 0)

    def test_coast_stops_when_velocity_falls_below_threshold(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        wheel._coast_timer.start()
        # Set velocity below the 5 deg/s stop threshold.
        wheel._coast_velocity_rad_s = math.radians(1.0)
        wheel._on_coast_tick()
        self.assertFalse(wheel._coast_timer.isActive())
        self.assertEqual(wheel._coast_velocity_rad_s, 0.0)


class JogWheelGatingTests(_QtTestCase):
    def test_disabling_stops_in_flight_coast(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        wheel._coast_velocity_rad_s = math.radians(180.0)
        wheel._coast_timer.start()
        self.assertTrue(wheel._coast_timer.isActive())
        wheel.set_wheel_enabled(False)
        self.assertFalse(wheel._coast_timer.isActive())
        self.assertEqual(wheel._coast_velocity_rad_s, 0.0)

    def test_disabling_clears_frame_carry(self) -> None:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        wheel._frame_carry_degrees = 0.7
        wheel.set_wheel_enabled(False)
        self.assertEqual(wheel._frame_carry_degrees, 0.0)
        # Enabling again, a 0.5° rotation must NOT emit because the
        # carry was cleared (0.5 < 1.0).
        wheel.set_wheel_enabled(True)
        emitted: list[int] = []
        wheel.seek_by_frames_requested.connect(emitted.append)
        wheel._apply_rotation(math.radians(0.5))
        self.assertEqual(emitted, [])

    def test_repeated_set_wheel_enabled_is_idempotent(self) -> None:
        # Setting the same state twice should not toggle internal state
        # or emit spurious cursor changes.
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        cursor_after_first = wheel.cursor().shape()
        wheel.set_wheel_enabled(True)
        self.assertEqual(wheel.cursor().shape(), cursor_after_first)


class JogWheelGestureLifecycleTests(_QtTestCase):
    """`jog_engaged` / `jog_released` bracket a touch-to-settle gesture."""

    def _enabled_wheel(self) -> JogWheel:
        wheel = JogWheel()
        wheel.set_wheel_enabled(True)
        return wheel

    def test_press_emits_engaged_once(self) -> None:
        wheel = self._enabled_wheel()
        engaged: list[int] = []
        wheel.jog_engaged.connect(lambda: engaged.append(1))
        _press(wheel, 70, 70)  # center of the 140px wheel
        self.assertEqual(len(engaged), 1)

    def test_disabled_wheel_does_not_engage(self) -> None:
        wheel = JogWheel()  # starts disabled
        engaged: list[int] = []
        wheel.jog_engaged.connect(lambda: engaged.append(1))
        _press(wheel, 70, 70)
        self.assertEqual(engaged, [])

    def test_release_without_coast_emits_released(self) -> None:
        wheel = self._enabled_wheel()
        released: list[int] = []
        wheel.jog_released.connect(lambda: released.append(1))
        _press(wheel, 70, 70)
        # No drag motion → zero velocity → no coast → settles on release.
        _release(wheel, 70, 70)
        self.assertEqual(len(released), 1)

    def test_coast_stop_emits_released(self) -> None:
        wheel = self._enabled_wheel()
        released: list[int] = []
        wheel.jog_released.connect(lambda: released.append(1))
        _press(wheel, 70, 70)
        # Simulate inertia decaying below the stop threshold.
        wheel._coast_velocity_rad_s = math.radians(1.0)
        wheel._on_coast_tick()
        self.assertEqual(len(released), 1)

    def test_regrab_during_coast_does_not_reengage(self) -> None:
        wheel = self._enabled_wheel()
        engaged: list[int] = []
        wheel.jog_engaged.connect(lambda: engaged.append(1))
        _press(wheel, 70, 70)
        # Pretend the wheel is coasting, then the referee grabs it again.
        wheel._coast_velocity_rad_s = math.radians(180.0)
        wheel._coast_timer.start()
        _press(wheel, 70, 70)
        self.assertEqual(len(engaged), 1)

    def test_disable_cancels_gesture_without_released(self) -> None:
        wheel = self._enabled_wheel()
        released: list[int] = []
        wheel.jog_released.connect(lambda: released.append(1))
        _press(wheel, 70, 70)
        wheel.set_wheel_enabled(False)
        self.assertEqual(released, [])
        self.assertFalse(wheel._gesture_engaged)


if __name__ == "__main__":
    unittest.main()
