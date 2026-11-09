"""Tests for the "Starting cameras…" window."""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from app.ui.starting_cameras_window import StartingCamerasWindow


class StartingCamerasWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def make(self, names: list[str]) -> StartingCamerasWindow:
        window = StartingCamerasWindow(names)
        self.addCleanup(window.deleteLater)
        return window

    def test_shows_the_starting_message(self) -> None:
        window = self.make(["Baseline A"])
        self.assertEqual(window.title_label.text(), "Starting cameras…")

    def test_lists_each_camera_by_its_short_name(self) -> None:
        window = self.make(["Baseline A", "Baseline B", "Sideline A", "Sideline B"])
        self.assertEqual(
            [label.text() for label in window.camera_labels],
            ["○  Baseline A", "○  Baseline B", "○  Sideline A", "○  Sideline B"],
        )
        self.assertIn("Connecting 4 cameras", window.detail_label.text())

    def test_singular_wording_for_one_camera(self) -> None:
        window = self.make(["Court"])
        self.assertIn("Connecting 1 camera.", window.detail_label.text())

    def test_it_cannot_be_closed_by_the_operator_and_has_no_buttons(self) -> None:
        window = self.make(["A"])
        self.assertFalse(window.windowFlags() & Qt.WindowType.WindowCloseButtonHint)
        self.assertEqual(window.findChildren(QPushButton), [])

    def test_progress_is_an_unmeasured_busy_bar(self) -> None:
        window = self.make(["A"])
        self.assertEqual((window.progress.minimum(), window.progress.maximum()), (0, 0))

    def test_show_and_paint_shows_the_window(self) -> None:
        window = self.make(["A"])
        window.show_and_paint()
        self.assertTrue(window.isVisible())
        window.close()
        self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
