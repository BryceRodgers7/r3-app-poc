"""Phase 14.C: referee-window transport rebuild.

Covers the widget-level contract for the rebuilt
`RefereeControlsWidget`:

- Speed buttons (2x, 1/2x, 1/4x, 1/8x) emit distinct signals.
- Rewind button label reflects `rewind_seconds`.
- Pause label flips between Play and Pause based on `playback_rate`.

Phase 14.F removed the `ScrubberSlider` (replaced by `JogWheel`) —
slider tests moved to `test_jog_wheel.py`. The free-floating play-
number badge was later removed (no chrome overlays the video feed).
"""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.ui.referee_controls_widget import RefereeControlsWidget


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


class RefereeControlsButtonSurfaceTests(_QtTestCase):
    def test_all_speed_buttons_present(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        for attr in (
            "speed_2x_button",
            "half_speed_button",
            "quarter_speed_button",
            "eighth_speed_button",
        ):
            self.assertTrue(hasattr(controls, attr), f"missing {attr}")

    def test_speed_buttons_emit_distinct_signals(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        emitted: list[str] = []
        controls.speed_2x_requested.connect(lambda: emitted.append("2x"))
        controls.half_speed_requested.connect(lambda: emitted.append("0.5"))
        controls.quarter_speed_requested.connect(lambda: emitted.append("0.25"))
        controls.eighth_speed_requested.connect(lambda: emitted.append("0.125"))
        controls.speed_2x_button.clicked.emit()
        controls.half_speed_button.clicked.emit()
        controls.quarter_speed_button.clicked.emit()
        controls.eighth_speed_button.clicked.emit()
        self.assertEqual(emitted, ["2x", "0.5", "0.25", "0.125"])

    def test_rewind_label_uses_configured_seconds(self) -> None:
        # Phase 14.C: the label is built from settings at construction
        # so a future tweak of `[replay] rewind_seconds` shows up in
        # the UI without code changes.
        controls = RefereeControlsWidget(button_height=72, rewind_seconds=5)
        self.assertEqual(controls.rewind_button.text(), "Rewind 5s")
        controls = RefereeControlsWidget(button_height=72, rewind_seconds=10)
        self.assertEqual(controls.rewind_button.text(), "Rewind 10s")

    def test_pause_label_flips_with_rate(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        # Active playback → Pause.
        controls.set_pause_label_for_rate(1.0)
        self.assertIn("Pause", controls.pause_button.text())
        # Paused (rate==0) → Play.
        controls.set_pause_label_for_rate(0.0)
        self.assertIn("Play", controls.pause_button.text())
        # Slow motion is still "advancing", so Pause.
        controls.set_pause_label_for_rate(0.25)
        self.assertIn("Pause", controls.pause_button.text())

    def test_active_rate_lights_only_the_matching_button(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        controls.set_active_rate(0.5)
        self.assertTrue(controls.half_speed_button.is_active())
        self.assertFalse(controls.speed_2x_button.is_active())
        self.assertFalse(controls.quarter_speed_button.is_active())
        self.assertFalse(controls.eighth_speed_button.is_active())

    def test_active_rate_moves_between_buttons(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        controls.set_active_rate(0.5)
        controls.set_active_rate(0.125)
        self.assertFalse(controls.half_speed_button.is_active())
        self.assertTrue(controls.eighth_speed_button.is_active())

    def test_no_speed_button_lit_at_normal_speed_or_paused(self) -> None:
        controls = RefereeControlsWidget(button_height=72)
        for rate in (1.0, 0.0):
            controls.set_active_rate(0.5)
            controls.set_active_rate(rate)
            self.assertFalse(controls.half_speed_button.is_active())


if __name__ == "__main__":
    unittest.main()
