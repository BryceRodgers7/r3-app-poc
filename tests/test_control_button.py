"""`ControlButton` press feedback: the minimum-duration flash + latched state.

The style tests read rendered pixels rather than properties: the QSS
rule order is what makes a flash visible on a button that disabled
itself in response to the press, and only the paint shows that.
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.ui.control_button import (
    FLASH_MS,
    GO_TONE,
    NEUTRAL_TONE,
    PRESS_FEEDBACK_COLOR,
    STOP_TONE,
    ControlButton,
)


_ACTIVE_FILL = "#1f6feb"
_DISABLED_FILL = "#22272e"


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


def _fill_color(button: ControlButton) -> str:
    """Background color just inside the border, clear of the label."""
    button.resize(120, 72)
    return button.grab().toImage().pixelColor(8, 36).name()


class FlashLifecycleTests(_QtTestCase):
    def test_press_starts_flash_before_release(self) -> None:
        button = ControlButton("Next Play")
        QTest.mousePress(button, Qt.MouseButton.LeftButton)
        self.assertTrue(button.is_flashing())
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)

    def test_flash_outlives_an_instant_click(self) -> None:
        button = ControlButton("Next Play")
        button.click()
        self.assertTrue(button.is_flashing())

    def test_flash_ends_after_flash_interval(self) -> None:
        button = ControlButton("Next Play")
        button.click()
        QTest.qWait(FLASH_MS + 100)
        self.assertFalse(button.is_flashing())

    def test_flash_is_on_before_the_click_handler_runs(self) -> None:
        # Begin Game's handler blocks the UI thread; the flash has to
        # be up before it starts, not after it returns.
        button = ControlButton("Begin Game")
        seen: list[bool] = []
        button.clicked.connect(lambda: seen.append(button.is_flashing()))
        button.click()
        self.assertEqual(seen, [True])

    def test_repeat_press_restarts_the_timer(self) -> None:
        button = ControlButton("Step ▶")
        with mock.patch.object(button._flash_timer, "start") as start:
            button.click()
            button.click()
        self.assertEqual(start.call_count, 2)

    def test_disabled_button_does_not_flash(self) -> None:
        button = ControlButton("Challenge")
        button.setEnabled(False)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertFalse(button.is_flashing())

    def test_flash_timer_is_precise(self) -> None:
        # A default timer on Windows is a WM_TIMER, which starves while
        # feeds keep the event loop busy: End Game stayed amber for over
        # two seconds in the real app.
        button = ControlButton("End Game")
        self.assertEqual(
            button._flash_timer.timerType(), Qt.TimerType.PreciseTimer
        )


class ActiveStateTests(_QtTestCase):
    def test_starts_inactive(self) -> None:
        self.assertFalse(ControlButton("1/2x").is_active())

    def test_set_active_round_trip(self) -> None:
        button = ControlButton("1/2x")
        button.set_active(True)
        self.assertTrue(button.is_active())
        button.set_active(False)
        self.assertFalse(button.is_active())

    def test_click_does_not_latch_active(self) -> None:
        # Active is driven from UiState, so it can't claim a mode the
        # controller refused.
        button = ControlButton("1/2x")
        button.click()
        self.assertFalse(button.is_active())

    def test_unchanged_active_skips_repolish(self) -> None:
        # set_active runs on every UiState tick.
        button = ControlButton("1/2x")
        button.set_active(True)
        with mock.patch.object(button, "_repolish") as repolish:
            button.set_active(True)
        repolish.assert_not_called()


class ToneTests(_QtTestCase):
    def test_tone_sets_background(self) -> None:
        button = ControlButton("Begin Game", tone=GO_TONE)
        self.assertIn(f"background-color: {GO_TONE.background}", button.styleSheet())

    def test_set_tone_swaps_background(self) -> None:
        button = ControlButton("Begin Game", tone=GO_TONE)
        button.set_tone(STOP_TONE)
        self.assertIn(f"background-color: {STOP_TONE.background}", button.styleSheet())

    def test_unchanged_tone_skips_stylesheet_swap(self) -> None:
        # set_recording_label runs on every UiState tick.
        button = ControlButton("Begin Game", tone=GO_TONE)
        with mock.patch.object(button, "setStyleSheet") as set_style:
            button.set_tone(GO_TONE)
        set_style.assert_not_called()


class RenderedStateTests(_QtTestCase):
    """What's on screen in each combination, QSS precedence included."""

    def test_resting(self) -> None:
        self.assertEqual(_fill_color(ControlButton("x")), NEUTRAL_TONE.background)

    def test_active(self) -> None:
        button = ControlButton("x")
        button.set_active(True)
        self.assertEqual(_fill_color(button), _ACTIVE_FILL)

    def test_disabled(self) -> None:
        button = ControlButton("x")
        button.setEnabled(False)
        self.assertEqual(_fill_color(button), _DISABLED_FILL)

    def test_active_stays_lit_while_disabled(self) -> None:
        # An open challenge: Challenge can't be re-pressed, but it
        # still has to read as "on".
        button = ControlButton("x")
        button.set_active(True)
        button.setEnabled(False)
        self.assertEqual(_fill_color(button), _ACTIVE_FILL)

    def test_flash_beats_resting_tone(self) -> None:
        button = ControlButton("x", tone=STOP_TONE)
        button.click()
        self.assertEqual(_fill_color(button), PRESS_FEEDBACK_COLOR)

    def test_flash_beats_active(self) -> None:
        button = ControlButton("x")
        button.set_active(True)
        button.click()
        self.assertEqual(_fill_color(button), PRESS_FEEDBACK_COLOR)

    def test_flash_beats_disabled_and_active(self) -> None:
        # Challenge's exact sequence: the press opens the clip, which
        # disables the button and latches it active mid-flash.
        button = ControlButton("x")
        button.click()
        button.set_active(True)
        button.setEnabled(False)
        self.assertEqual(_fill_color(button), PRESS_FEEDBACK_COLOR)

    def test_returns_to_active_after_flash(self) -> None:
        button = ControlButton("x")
        button.set_active(True)
        button.click()
        QTest.qWait(FLASH_MS + 100)
        self.assertEqual(_fill_color(button), _ACTIVE_FILL)


if __name__ == "__main__":
    unittest.main()
