"""Phase 14.B: operator-window Play/Clip counter overlay."""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication, QWidget

from app.ui.operator_status_overlay import OperatorStatusOverlay


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


class OperatorStatusOverlayTests(_QtTestCase):
    def test_hidden_at_construction(self) -> None:
        parent = QWidget()
        overlay = OperatorStatusOverlay(parent)
        self.assertFalse(overlay.isVisible())

    def test_hidden_when_not_recording(self) -> None:
        parent = QWidget()
        parent.show()
        try:
            overlay = OperatorStatusOverlay(parent)
            overlay.set_counters(
                is_recording=False, play_number=1, clip_number=3,
            )
            self.assertFalse(overlay.isVisible())
        finally:
            parent.hide()

    def test_hidden_when_no_clip_number(self) -> None:
        # Even while recording, if clip_number is None there's nothing
        # meaningful to show (transient gap between Start press and
        # ClipManager.start_game).
        parent = QWidget()
        parent.show()
        try:
            overlay = OperatorStatusOverlay(parent)
            overlay.set_counters(
                is_recording=True, play_number=None, clip_number=None,
            )
            self.assertFalse(overlay.isVisible())
        finally:
            parent.hide()

    def test_pre_game_shows_play_na_with_clip_number(self) -> None:
        parent = QWidget()
        parent.show()
        try:
            overlay = OperatorStatusOverlay(parent)
            overlay.set_counters(
                is_recording=True, play_number=None, clip_number=0,
            )
            self.assertTrue(overlay.isVisible())
            self.assertIn("Play N/A", overlay.text())
            self.assertIn("Clip 0", overlay.text())
        finally:
            parent.hide()

    def test_during_play_shows_both_numbers(self) -> None:
        parent = QWidget()
        parent.show()
        try:
            overlay = OperatorStatusOverlay(parent)
            overlay.set_counters(
                is_recording=True, play_number=3, clip_number=5,
            )
            self.assertTrue(overlay.isVisible())
            self.assertIn("Play 3", overlay.text())
            self.assertIn("Clip 5", overlay.text())
        finally:
            parent.hide()


if __name__ == "__main__":
    unittest.main()
