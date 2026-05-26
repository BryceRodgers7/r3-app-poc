"""Phase 14.C: referee-window transport rebuild + scrubber + play badge.

Covers the widget-level contract for the rebuilt
`RefereeControlsWidget`:

- Speed buttons (2x, 1/2x, 1/4x, 1/8x) emit distinct signals.
- Rewind button label reflects `rewind_seconds`.
- Pause label flips between Play and Pause based on `playback_rate`.
- `ScrubberSlider` emits seek requests in session-time ns and clamps
  to its configured range.
- `RefereePlayBadge` shows "Play N/A" pre-game and "Play NN"
  otherwise; the challenge hook flips the stylesheet.
"""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.ui.referee_controls_widget import RefereeControlsWidget
from app.ui.referee_play_badge import RefereePlayBadge
from app.ui.scrubber_slider import ScrubberSlider


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


class ScrubberSliderTests(_QtTestCase):
    def test_disabled_when_no_replayable_range(self) -> None:
        slider = ScrubberSlider()
        slider.update_position(earliest_ns=None, latest_ns=None, current_ns=None)
        self.assertFalse(slider.isEnabled())

    def test_enabled_when_range_present(self) -> None:
        slider = ScrubberSlider()
        slider.update_position(
            earliest_ns=0,
            latest_ns=20_000_000_000,
            current_ns=5_000_000_000,
        )
        self.assertTrue(slider.isEnabled())

    def test_seek_emits_session_time_ns(self) -> None:
        slider = ScrubberSlider()
        slider.update_position(
            earliest_ns=0,
            latest_ns=20_000_000_000,
            current_ns=0,
        )
        emitted: list[int] = []
        slider.seek_to_session_time_requested.connect(emitted.append)
        # Setting the value directly (not via drag) emits via the
        # valueChanged → _on_value_changed path. Convert ns → steps.
        target_ns = 7_500_000_000
        slider.setValue(slider._ns_to_step(target_ns))
        self.assertEqual(len(emitted), 1)
        # Step-quantization to 10 ms; allow a tolerance.
        self.assertAlmostEqual(emitted[0], target_ns, delta=10_000_000)

    def test_position_tracks_current_ns_when_not_dragging(self) -> None:
        slider = ScrubberSlider()
        slider.update_position(
            earliest_ns=0,
            latest_ns=20_000_000_000,
            current_ns=4_000_000_000,
        )
        first = slider.value()
        slider.update_position(
            earliest_ns=0,
            latest_ns=20_000_000_000,
            current_ns=12_000_000_000,
        )
        second = slider.value()
        self.assertGreater(second, first)


class RefereePlayBadgeTests(_QtTestCase):
    def test_pre_game_shows_na(self) -> None:
        badge = RefereePlayBadge()
        badge.set_play_state(play_number=None)
        self.assertIn("N/A", badge.text())

    def test_play_number_renders(self) -> None:
        badge = RefereePlayBadge()
        badge.set_play_state(play_number=7)
        self.assertIn("7", badge.text())
        self.assertIn("Play", badge.text())

    def test_challenge_active_changes_style(self) -> None:
        badge = RefereePlayBadge()
        default_style = badge.styleSheet()
        badge.set_challenge_active(True)
        self.assertNotEqual(badge.styleSheet(), default_style)
        # Red — match the End Game red used on the operator window.
        self.assertIn("d24747", badge.styleSheet())
        badge.set_challenge_active(False)
        self.assertEqual(badge.styleSheet(), default_style)


if __name__ == "__main__":
    unittest.main()
