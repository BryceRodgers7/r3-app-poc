"""Zoom/pan geometry: the math both the native and QImage paths draw from."""

from __future__ import annotations

import unittest

from app.ui.feed_view import (
    MAX_SURFACE_DIMENSION,
    MAX_SURFACE_PIXELS,
    MAX_ZOOM,
    MIN_ZOOM,
    ZOOM_PER_WHEEL_NOTCH,
    FeedView,
    Tile,
    clamped,
    max_zoom,
    panned,
    picture_rect,
    pinched,
    scaled,
    visible_source_rect,
    zoomed,
)


_WIDE = 16 / 9
_TILE = Tile(1600, 900, _WIDE)  # exact 16:9, so the picture fills it
_TALL_TILE = Tile(800, 900, _WIDE)  # letterboxed top and bottom at 1x
_SMALL_TILE = Tile(800, 450, _WIDE)  # small enough to reach MAX_ZOOM
_MAXIMIZED_TILE = Tile(2322, 1306, _WIDE)  # the surface measured at 4x


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
        self.assertEqual(
            zoomed(FeedView(), 50, 400, 225, _SMALL_TILE).zoom, MAX_ZOOM
        )
        self.assertEqual(
            zoomed(FeedView(), 50, 800, 450, _TILE).zoom, max_zoom(_TILE)
        )

    def test_scaled_multiplies_by_a_factor(self) -> None:
        self.assertAlmostEqual(scaled(FeedView(), 2.5, 400, 225, _SMALL_TILE).zoom, 2.5)
        self.assertAlmostEqual(
            scaled(FeedView(zoom=4.0), 0.5, 400, 225, _SMALL_TILE).zoom, 2.0
        )

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


class MaxZoomTests(unittest.TestCase):
    def test_a_small_tile_reaches_the_full_limit(self) -> None:
        self.assertEqual(max_zoom(_SMALL_TILE), MAX_ZOOM)
        self.assertEqual(MAX_ZOOM, 8.0)

    def test_the_measured_surface_is_the_most_a_tile_asks_for(self) -> None:
        # 4x of a maximized tile is what was seen to work on the sink.
        self.assertAlmostEqual(max_zoom(_MAXIMIZED_TILE), 4.0, places=2)
        for tile in (_SMALL_TILE, _TILE, _MAXIMIZED_TILE, Tile(3840, 2160, _WIDE)):
            _, _, width, height = picture_rect(FeedView(zoom=max_zoom(tile)), tile)
            self.assertLessEqual(width * height, MAX_SURFACE_PIXELS * 1.001)
            self.assertLessEqual(max(width, height), MAX_SURFACE_DIMENSION)

    def test_a_bigger_tile_zooms_less(self) -> None:
        self.assertLess(max_zoom(_TILE), max_zoom(_SMALL_TILE))
        self.assertLess(max_zoom(_MAXIMIZED_TILE), max_zoom(_TILE))

    def test_a_huge_tile_still_zooms_in_a_little(self) -> None:
        self.assertGreaterEqual(max_zoom(Tile(20000, 11250, _WIDE)), MIN_ZOOM)

    def test_a_tile_with_no_area_does_not_divide_by_zero(self) -> None:
        self.assertEqual(max_zoom(Tile(0, 0, _WIDE)), MAX_ZOOM)

    def test_clamped_pulls_an_oversized_zoom_down_to_the_tile(self) -> None:
        # Set on a small tile, shown on a large one.
        view = clamped(FeedView(zoom=MAX_ZOOM), _MAXIMIZED_TILE)
        self.assertAlmostEqual(view.zoom, max_zoom(_MAXIMIZED_TILE))


class PinchTests(unittest.TestCase):
    """Two fingers: spread to zoom, move together to pan."""

    @staticmethod
    def _source_under(view: FeedView, tile: Tile, x: float, y: float) -> tuple[float, float]:
        left, top, width, height = picture_rect(view, tile)
        return (x - left) / width, (y - top) / height

    def test_spreading_zooms_in(self) -> None:
        before = ((300.0, 225.0), (500.0, 225.0))
        after = ((200.0, 225.0), (600.0, 225.0))
        view = pinched(FeedView(), before, after, _SMALL_TILE)
        self.assertAlmostEqual(view.zoom, 2.0)

    def test_pinching_closed_zooms_out(self) -> None:
        before = ((200.0, 225.0), (600.0, 225.0))
        after = ((300.0, 225.0), (500.0, 225.0))
        view = pinched(FeedView(zoom=4.0), before, after, _SMALL_TILE)
        self.assertAlmostEqual(view.zoom, 2.0)

    def test_the_picture_stays_under_the_fingers(self) -> None:
        start = FeedView(zoom=2.0, center_x=0.4, center_y=0.6)
        before = ((250.0, 150.0), (450.0, 250.0))
        # Spread 1.5x and moved, but not twisted: zoom and pan can't follow a rotation.
        after = ((200.0, 125.0), (500.0, 275.0))
        view = pinched(start, before, after, _SMALL_TILE)
        for finger_before, finger_after in zip(before, after):
            expected = self._source_under(start, _SMALL_TILE, *finger_before)
            actual = self._source_under(view, _SMALL_TILE, *finger_after)
            self.assertAlmostEqual(actual[0], expected[0], places=3)
            self.assertAlmostEqual(actual[1], expected[1], places=3)

    def test_moving_both_fingers_together_pans_without_zooming(self) -> None:
        before = ((300.0, 225.0), (500.0, 225.0))
        after = ((240.0, 225.0), (440.0, 225.0))
        start = FeedView(zoom=2.0)
        view = pinched(start, before, after, _SMALL_TILE)
        self.assertAlmostEqual(view.zoom, 2.0)
        self.assertGreater(view.center_x, start.center_x)  # dragged left
        self.assertEqual(
            picture_rect(view, _SMALL_TILE)[0] - picture_rect(start, _SMALL_TILE)[0],
            -60,
        )

    def test_pinching_out_stops_at_the_tiles_limit(self) -> None:
        before = ((390.0, 225.0), (410.0, 225.0))
        after = ((0.0, 225.0), (800.0, 225.0))  # 40x
        view = pinched(FeedView(), before, after, _SMALL_TILE)
        self.assertEqual(view.zoom, MAX_ZOOM)

    def test_pinching_in_stops_at_1x(self) -> None:
        before = ((0.0, 225.0), (800.0, 225.0))
        after = ((390.0, 225.0), (410.0, 225.0))
        view = pinched(FeedView(zoom=2.0), before, after, _SMALL_TILE)
        self.assertEqual(view, FeedView())

    def test_fingers_on_the_same_spot_make_no_zoom(self) -> None:
        # No usable distance to take a ratio of.
        before = ((400.0, 225.0), (400.0, 225.0))
        after = ((300.0, 225.0), (500.0, 225.0))
        self.assertEqual(pinched(FeedView(), before, after, _SMALL_TILE).zoom, MIN_ZOOM)


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
