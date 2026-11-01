"""`MultiFeedVideoPanel` picks the grid that maximizes each feed's picture."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from app.core.models import FeedDefinition
from app.ui.multi_feed_video_panel import MultiFeedVideoPanel, best_grid_columns


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


def _feeds(n: int) -> list[FeedDefinition]:
    return [
        FeedDefinition(
            feed_id=f"cam_{i}",
            display_name=f"Camera {i}",
            source_kind="synthetic",
        )
        for i in range(1, n + 1)
    ]


def _shown_panel(n: int) -> MultiFeedVideoPanel:
    """Panel that receives resize events (Qt defers them while hidden)."""
    panel = MultiFeedVideoPanel(_feeds(n))
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    panel.show()
    return panel


def _position(panel: MultiFeedVideoPanel, feed_id: str) -> tuple[int, int]:
    index = panel._grid.indexOf(panel._cells[feed_id])
    row, column, _, _ = panel._grid.getItemPosition(index)
    return row, column


class BestGridColumnsTests(unittest.TestCase):
    def test_single_feed(self) -> None:
        self.assertEqual(best_grid_columns(1, 1700, 1000), 1)

    def test_no_feeds(self) -> None:
        self.assertEqual(best_grid_columns(0, 1700, 1000), 1)

    def test_four_feeds_on_a_16_9_panel_is_two_by_two(self) -> None:
        self.assertEqual(best_grid_columns(4, 1600, 900), 2)

    def test_two_feeds_side_by_side_in_a_wide_panel(self) -> None:
        self.assertEqual(best_grid_columns(2, 2000, 500), 2)

    def test_two_feeds_stack_in_a_narrow_panel(self) -> None:
        # The pre-fit sqrt grid always put these side by side, at half
        # the width the stacked layout gets.
        self.assertEqual(best_grid_columns(2, 1000, 1000), 1)

    def test_three_feeds_prefer_a_row_when_very_wide(self) -> None:
        self.assertEqual(best_grid_columns(3, 3000, 500), 3)

    def test_column_count_is_capped(self) -> None:
        self.assertEqual(best_grid_columns(8, 10000, 200), 4)

    def test_tie_goes_to_fewer_columns(self) -> None:
        # Both 3 and 4 columns need two rows for six feeds here, and
        # the height is the limit, so the pictures are the same size.
        self.assertEqual(best_grid_columns(6, 10000, 200), 3)

    def test_title_strip_counts_against_cell_height(self) -> None:
        # Identical-looking fit without titles; the strip costs the
        # stacked layout twice, which tips it to side by side.
        self.assertEqual(best_grid_columns(2, 1700, 960, spacing=0), 1)
        self.assertEqual(
            best_grid_columns(2, 1700, 960, spacing=0, title_height=40), 2
        )

    def test_degenerate_panel_does_not_raise(self) -> None:
        self.assertEqual(best_grid_columns(4, 0, 0, title_height=16), 1)


class PanelReflowTests(_QtTestCase):
    def test_resize_changes_the_grid_shape(self) -> None:
        panel = _shown_panel(2)
        panel.resize(2000, 500)
        self.assertEqual(_position(panel, "cam_2"), (0, 1))
        panel.resize(1000, 1000)
        self.assertEqual(_position(panel, "cam_2"), (1, 0))

    def test_hiding_a_feed_refits_the_remainder(self) -> None:
        panel = _shown_panel(4)
        panel.resize(1000, 1000)
        panel.set_tile_visible("cam_3", False)
        panel.set_tile_visible("cam_4", False)
        self.assertEqual(_position(panel, "cam_2"), (1, 0))

    def test_reflow_does_not_hide_cells_that_stay(self) -> None:
        # Hiding a cell flashes its native d3d11 surface.
        panel = _shown_panel(2)
        panel.resize(2000, 500)
        with mock.patch.object(panel._cells["cam_1"], "hide") as hide:
            panel.resize(1000, 1000)
        self.assertEqual(_position(panel, "cam_2"), (1, 0))  # did reflow
        hide.assert_not_called()

    def test_resize_with_same_arrangement_leaves_grid_alone(self) -> None:
        panel = _shown_panel(2)
        panel.resize(2000, 500)
        with mock.patch.object(panel, "_grid") as grid:
            panel.resize(2100, 500)
        grid.addWidget.assert_not_called()

    def test_frame_pixmap_does_not_pin_the_tile_size(self) -> None:
        # Replay pixmaps are scaled to the label, so a content-driven
        # minimum would stop a tile from ever shrinking, and reflowing
        # one big picture into a grid would widen the window.
        panel = MultiFeedVideoPanel(_feeds(1))
        tile = panel.widgets_by_feed_id["cam_1"]
        tile._frame_label.setPixmap(QPixmap(1600, 900))
        self.assertLess(panel.minimumSizeHint().width(), 1600)
        self.assertLess(panel.minimumSizeHint().height(), 900)


if __name__ == "__main__":
    unittest.main()
