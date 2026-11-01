"""Zoom/pan geometry: the math both the native and QImage paths draw from."""

from __future__ import annotations

import unittest

from app.ui.feed_view import (
    MAX_ZOOM,
    MIN_ZOOM,
    ZOOM_PER_WHEEL_NOTCH,
    FeedView,
    Tile,
    clamped,
    panned,
    picture_rect,
    visible_source_rect,
    zoomed,
)


_WIDE = 16 / 9
_TILE = Tile(1600, 900, _WIDE)  # exact 16:9, so the picture fills it
_TALL_TILE = Tile(800, 900, _WIDE)  # letterboxed top and bottom at 1x


class PictureRectTests(unittest.TestCase):
    def test_unzoomed_picture_fills_a_matching_tile(self) -> None:
        self.assertEqual(picture_rect(FeedView(), _TILE), (0, 0, 1600, 900))

    def test_unzoomed_picture_is_letterboxed_and_centered(self) -> None:
        self.assertEqual(picture_rect(FeedView(), _TALL_TILE), (0, 225, 800, 450))

    def test_zoom_scales_about_the_center(self) -> None:
        self.assertEqual(
            picture_rect(FeedView(zoom=2.0), _TILE), (-800, -450, 3200, 1800)
        )

    def test_center_moves_the_picture(self) -> None:
        view = FeedView(zoom=2.0, center_x=0.25, center_y=0.75)
        self.assertEqual(picture_rect(view, _TILE), (0, -900, 3200, 1800))

    def test_rect_stays_valid_for_the_sink(self) -> None:
        # The sink blanks on a -1 width or height, its reset value.
        x, y, width, height = picture_rect(FeedView(zoom=MIN_ZOOM), Tile(1, 1, _WIDE))
        self.assertGreaterEqual(width, 0)
        self.assertGreaterEqual(height, 0)
        self.assertNotEqual((x, y, width, height), (-1, -1, -1, -1))


class ZoomTests(unittest.TestCase):
    def test_one_notch_multiplies_zoom(self) -> None:
        view = zoomed(FeedView(), 1, 800, 450, _TILE)
        self.assertAlmostEqual(view.zoom, ZOOM_PER_WHEEL_NOTCH)

    def test_fractional_notches_from_a_smooth_wheel(self) -> None:
        coarse = zoomed(FeedView(), 1, 800, 450, _TILE)
        fine = FeedView()
        for _ in range(4):
            fine = zoomed(fine, 0.25, 800, 450, _TILE)
        self.assertAlmostEqual(fine.zoom, coarse.zoom)

    def test_zoom_stops_at_the_limits(self) -> None:
        self.assertEqual(zoomed(FeedView(), -5, 800, 450, _TILE).zoom, MIN_ZOOM)
        self.assertEqual(zoomed(FeedView(), 50, 800, 450, _TILE).zoom, MAX_ZOOM)

    def test_point_under_the_cursor_stays_put(self) -> None:
        start = FeedView(zoom=2.0, center_x=0.4, center_y=0.6)
        anchor_x, anchor_y = 1000, 300

        def source_at(view: FeedView) -> tuple[float, float]:
            x, y, width, height = picture_rect(view, _TILE)
            return (anchor_x - x) / width, (anchor_y - y) / height

        after = zoomed(start, 1, anchor_x, anchor_y, _TILE)
        for before_value, after_value in zip(source_at(start), source_at(after)):
            self.assertAlmostEqual(before_value, after_value, places=3)

    def test_zooming_out_fully_recenters(self) -> None:
        view = zoomed(FeedView(zoom=1.25, center_x=0.45, center_y=0.55), -1, 0, 0, _TILE)
        self.assertEqual(view, FeedView())


class PanTests(unittest.TestCase):
    def test_dragging_right_shows_more_of_the_left(self) -> None:
        view = panned(FeedView(zoom=2.0), 100, 0, _TILE)
        self.assertLess(view.center_x, 0.5)

    def test_picture_follows_the_cursor_one_to_one(self) -> None:
        before = picture_rect(FeedView(zoom=2.0), _TILE)
        after = picture_rect(panned(FeedView(zoom=2.0), 120, -60, _TILE), _TILE)
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (120, -60))

    def test_cannot_pan_past_the_picture_edge(self) -> None:
        x, y, _, _ = picture_rect(panned(FeedView(zoom=2.0), 99999, 99999, _TILE), _TILE)
        self.assertEqual((x, y), (0, 0))

    def test_unzoomed_picture_does_not_pan(self) -> None:
        self.assertEqual(panned(FeedView(), 200, 200, _TILE), FeedView())

    def test_axis_the_picture_does_not_fill_stays_centered(self) -> None:
        # 1.5x in a tall tile: wider than the tile, still shorter than it.
        view = panned(FeedView(zoom=1.5), 40, 40, _TALL_TILE)
        self.assertEqual(view.center_y, 0.5)
        self.assertLess(view.center_x, 0.5)


class ClampTests(unittest.TestCase):
    def test_view_survives_a_tile_of_the_same_shape(self) -> None:
        view = FeedView(zoom=2.0, center_x=0.3, center_y=0.6)
        self.assertEqual(clamped(view, Tile(800, 450, _WIDE)), view)

    def test_a_narrower_tile_pulls_the_view_back_in(self) -> None:
        # At 1.5x the 16:9 picture no longer overflows this tile's height.
        view = FeedView(zoom=1.5, center_x=0.5, center_y=0.3)
        self.assertEqual(clamped(view, _TALL_TILE).center_y, 0.5)


class VisibleSourceRectTests(unittest.TestCase):
    def test_full_image_when_unzoomed(self) -> None:
        source, target = visible_source_rect(FeedView(), _TILE, 1920, 1080)
        self.assertEqual(source, (0, 0, 1920, 1080))
        self.assertEqual(target, (1600, 900))

    def test_zoom_crops_the_middle(self) -> None:
        source, target = visible_source_rect(FeedView(zoom=2.0), _TILE, 1920, 1080)
        self.assertEqual(source, (480, 270, 960, 540))
        self.assertEqual(target, (1600, 900))

    def test_crop_matches_the_native_picture(self) -> None:
        # Live and replay must show the same region of the frame.
        view = FeedView(zoom=2.0, center_x=0.25, center_y=0.75)
        x, y, width, height = picture_rect(view, _TILE)
        source, _ = visible_source_rect(view, _TILE, 1920, 1080)
        self.assertAlmostEqual(source[0], -x / width * 1920, delta=1)
        self.assertAlmostEqual(source[1], -y / height * 1080, delta=1)

    def test_unfilled_axis_keeps_the_whole_image_height(self) -> None:
        source, target = visible_source_rect(FeedView(zoom=1.5), _TALL_TILE, 1920, 1080)
        self.assertEqual((source[1], source[3]), (0, 1080))
        self.assertEqual(target, (800, 675))

    def test_crop_never_leaves_the_image(self) -> None:
        for view in (
            FeedView(zoom=MAX_ZOOM, center_x=0.125, center_y=0.125),
            FeedView(zoom=MAX_ZOOM, center_x=0.875, center_y=0.875),
        ):
            (x, y, width, height), _ = visible_source_rect(view, _TILE, 1921, 1081)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + width, 1921)
            self.assertLessEqual(y + height, 1081)


if __name__ == "__main__":
    unittest.main()
