"""Phase 14.B: camera visibility ribbon + per-tile show/hide on MultiFeedVideoPanel."""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.core.models import (
    CLIP_TYPE_PLAY,
    CLIP_TYPE_PRE_GAME,
    CLIP_TYPE_TIMEOUT,
    FeedDefinition,
)
from app.ui.camera_visibility_ribbon import CameraVisibilityRibbon
from app.ui.multi_feed_video_panel import MultiFeedVideoPanel


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


class CameraVisibilityRibbonTests(_QtTestCase):
    def test_one_button_per_feed_with_one_indexed_labels(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(3))
        self.assertEqual(len(ribbon._buttons_by_feed), 3)
        # Buttons are labeled 1, 2, 3 — matches the PDF mock.
        labels = sorted(
            b.text() for b in ribbon._buttons_by_feed.values()
        )
        self.assertEqual(labels, ["1", "2", "3"])

    def test_all_buttons_start_checked_visible(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(2))
        for fid in ("cam_1", "cam_2"):
            self.assertTrue(ribbon.is_feed_visible(fid))

    def test_toggle_emits_signal_with_feed_id_and_visibility(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(2))
        events: list[tuple[str, bool]] = []
        ribbon.feed_visibility_toggled.connect(
            lambda fid, visible: events.append((fid, visible))
        )
        # Click cam_1's button to hide it.
        ribbon._buttons_by_feed["cam_1"].click()
        self.assertEqual(events, [("cam_1", False)])
        self.assertFalse(ribbon.is_feed_visible("cam_1"))
        # Re-toggle to restore.
        ribbon._buttons_by_feed["cam_1"].click()
        self.assertEqual(events[-1], ("cam_1", True))
        self.assertTrue(ribbon.is_feed_visible("cam_1"))

    def test_selector_label_starts_with_placeholder(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(2))
        self.assertEqual(ribbon.selector_label.text(), "Clip selector widget")

    def test_selector_label_renders_play_context(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(2))
        ribbon.set_selector_label(
            clip_type=CLIP_TYPE_PLAY, clip_number=3, play_number=2,
        )
        self.assertEqual(ribbon.selector_label.text(), "Clip 3 — play 2")

    def test_selector_label_renders_non_play_context(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(2))
        ribbon.set_selector_label(
            clip_type=CLIP_TYPE_TIMEOUT, clip_number=4, play_number=2,
        )
        self.assertEqual(ribbon.selector_label.text(), "Clip 4 — timeout")

    def test_selector_label_no_clip_open(self) -> None:
        ribbon = CameraVisibilityRibbon(_feeds(2))
        ribbon.set_selector_label(
            clip_type=None, clip_number=None, play_number=None,
        )
        self.assertEqual(ribbon.selector_label.text(), "No clip open")


class MultiFeedVideoPanelTileVisibilityTests(_QtTestCase):
    def test_initially_all_tiles_visible(self) -> None:
        panel = MultiFeedVideoPanel(_feeds(3))
        for fid in ("cam_1", "cam_2", "cam_3"):
            self.assertTrue(panel._cells[fid].isVisibleTo(panel._grid_host))

    def test_set_tile_visible_false_hides_cell(self) -> None:
        panel = MultiFeedVideoPanel(_feeds(3))
        panel.set_tile_visible("cam_2", False)
        self.assertTrue(panel._cells["cam_1"].isVisibleTo(panel._grid_host))
        self.assertFalse(panel._cells["cam_2"].isVisibleTo(panel._grid_host))
        self.assertTrue(panel._cells["cam_3"].isVisibleTo(panel._grid_host))

    def test_set_tile_visible_round_trip(self) -> None:
        panel = MultiFeedVideoPanel(_feeds(2))
        panel.set_tile_visible("cam_1", False)
        self.assertFalse(panel._cells["cam_1"].isVisibleTo(panel._grid_host))
        panel.set_tile_visible("cam_1", True)
        self.assertTrue(panel._cells["cam_1"].isVisibleTo(panel._grid_host))

    def test_set_tile_visible_idempotent(self) -> None:
        # Calling with the current state should be a no-op (no
        # exception, no extra layout work).
        panel = MultiFeedVideoPanel(_feeds(2))
        panel.set_tile_visible("cam_1", True)  # already visible
        self.assertTrue(panel._cells["cam_1"].isVisibleTo(panel._grid_host))

    def test_set_tile_visible_unknown_feed_no_op(self) -> None:
        panel = MultiFeedVideoPanel(_feeds(2))
        # Defensive — must not raise.
        panel.set_tile_visible("cam_missing", False)

    def test_hiding_all_tiles_does_not_crash(self) -> None:
        # Degenerate but legal — operator hiding every camera. Grid
        # reflow should accept zero visible cells.
        panel = MultiFeedVideoPanel(_feeds(2))
        panel.set_tile_visible("cam_1", False)
        panel.set_tile_visible("cam_2", False)
        for fid in ("cam_1", "cam_2"):
            self.assertFalse(panel._cells[fid].isVisibleTo(panel._grid_host))


if __name__ == "__main__":
    unittest.main()
