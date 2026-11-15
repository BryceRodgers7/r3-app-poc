"""Zoom and pan of one video tile, as pure geometry.

A tile draws its feed two ways: a native d3d11 surface for live video and
a QImage layer for replay. Both take their picture position from the same
`FeedView`, so a view carries across live and replay. The view is
normalized to the source picture rather than to the tile, which is what
lets it survive tile resizes and grid reflows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple


MIN_ZOOM = 1.0
# The most any tile zooms. A small tile reaches it; a large one stops
# sooner (see `max_zoom`), because the native sink's swapchain is as large
# as the zoomed picture.
MAX_ZOOM = 8.0
# The largest swapchain a tile may ask for, in pixels. This is the surface
# measured working on d3d11videosink (4x of a maximized 2322x1306 tile is
# 9288x5224), so no tile ever asks for more than has been seen to work.
MAX_SURFACE_PIXELS = 9288 * 5224
# Direct3D 11 caps a texture, so a swapchain buffer, at 16384 on a side.
MAX_SURFACE_DIMENSION = 16384
ZOOM_PER_WHEEL_NOTCH = 1.25
# Two touches closer than this (tile pixels) give no usable pinch ratio.
_MIN_PINCH_DISTANCE = 4.0


class Tile(NamedTuple):
    """The space a picture of this aspect ratio is fitted into."""

    width: int
    height: int
    aspect_ratio: float


@dataclass(frozen=True, slots=True)
class FeedView:
    """How far a tile is zoomed, and what it is centered on.

    `center_x`/`center_y` locate the point at the middle of the tile as
    fractions of the source picture.
    """

    zoom: float = MIN_ZOOM
    center_x: float = 0.5
    center_y: float = 0.5

    @property
    def is_zoomed(self) -> bool:
        return self.zoom > MIN_ZOOM


def _fitted_size(tile: Tile) -> tuple[float, float]:
    """Size of the picture at 1x, letterboxed into the tile."""
    width = min(float(tile.width), tile.height * tile.aspect_ratio)
    return width, width / tile.aspect_ratio


def _clamped_center(center: float, picture: float, tile: float) -> float:
    """Keep the picture over the tile; center it on axes it doesn't fill."""
    if picture <= tile:
        return 0.5
    half_visible = tile / picture / 2
    return max(half_visible, min(1.0 - half_visible, center))


def max_zoom(tile: Tile) -> float:
    """The most this tile can zoom: `MAX_ZOOM`, or less if the surface would be too big.

    The native sink draws the whole zoomed picture into a swapchain, so a
    big tile hits the surface limits before it hits `MAX_ZOOM`. The replay
    crop has no such limit, but a view moves between live and replay, so
    both obey the same one.
    """
    width, height = _fitted_size(tile)
    if width <= 0 or height <= 0:
        return MAX_ZOOM
    by_pixels = math.sqrt(MAX_SURFACE_PIXELS / (width * height))
    by_dimension = MAX_SURFACE_DIMENSION / max(width, height)
    return max(MIN_ZOOM, min(MAX_ZOOM, by_pixels, by_dimension))


def clamped(view: FeedView, tile: Tile) -> FeedView:
    """Return `view` adjusted so the tile shows no space it could fill."""
    zoom = max(MIN_ZOOM, min(max_zoom(tile), view.zoom))
    width, height = _fitted_size(tile)
    return FeedView(
        zoom,
        _clamped_center(view.center_x, width * zoom, tile.width),
        _clamped_center(view.center_y, height * zoom, tile.height),
    )


def scaled(
    view: FeedView, factor: float, anchor_x: float, anchor_y: float, tile: Tile
) -> FeedView:
    """Multiply the zoom by `factor`, keeping the point under the anchor in place."""
    zoom = max(MIN_ZOOM, min(max_zoom(tile), view.zoom * factor))
    width, height = _fitted_size(tile)

    def center_after(center: float, anchor: float, fitted: float, extent: int) -> float:
        left = extent / 2 - center * fitted * view.zoom
        source = (anchor - left) / (fitted * view.zoom)
        return source + (extent / 2 - anchor) / (fitted * zoom)

    return clamped(
        FeedView(
            zoom,
            center_after(view.center_x, anchor_x, width, tile.width),
            center_after(view.center_y, anchor_y, height, tile.height),
        ),
        tile,
    )


def zoomed(
    view: FeedView, notches: float, anchor_x: float, anchor_y: float, tile: Tile
) -> FeedView:
    """Zoom by wheel `notches`, keeping the point under the cursor in place."""
    return scaled(view, ZOOM_PER_WHEEL_NOTCH**notches, anchor_x, anchor_y, tile)


def panned(view: FeedView, delta_x: float, delta_y: float, tile: Tile) -> FeedView:
    """Move the picture by a drag of `delta` tile pixels."""
    width, height = _fitted_size(tile)
    return clamped(
        FeedView(
            view.zoom,
            view.center_x - delta_x / (width * view.zoom),
            view.center_y - delta_y / (height * view.zoom),
        ),
        tile,
    )


def pinched(
    view: FeedView,
    before: tuple[tuple[float, float], tuple[float, float]],
    after: tuple[tuple[float, float], tuple[float, float]],
    tile: Tile,
) -> FeedView:
    """Follow two fingers: the picture under them stays under them.

    `before` and `after` are the two touch points, in tile pixels, at the
    last event and now. Spreading them zooms, and moving their midpoint
    pans, in the same gesture.
    """
    (ax0, ay0), (bx0, by0) = before
    (ax1, ay1), (bx1, by1) = after
    distance_before = math.hypot(bx0 - ax0, by0 - ay0)
    distance_after = math.hypot(bx1 - ax1, by1 - ay1)
    mid_before = ((ax0 + bx0) / 2, (ay0 + by0) / 2)
    mid_after = ((ax1 + bx1) / 2, (ay1 + by1) / 2)
    factor = (
        distance_after / distance_before
        if distance_before >= _MIN_PINCH_DISTANCE
        else 1.0
    )
    # Zoom about where the fingers were, so that spot stays put; then drag
    # it along with the midpoint.
    return panned(
        scaled(view, factor, mid_before[0], mid_before[1], tile),
        mid_after[0] - mid_before[0],
        mid_after[1] - mid_before[1],
        tile,
    )


def picture_rect(view: FeedView, tile: Tile) -> tuple[int, int, int, int]:
    """Where the whole picture lands, as `(x, y, width, height)` in the tile.

    Overhangs the tile when zoomed; a native sink is clipped by its window.
    """
    width, height = _fitted_size(tile)
    width *= view.zoom
    height *= view.zoom
    return (
        round(tile.width / 2 - view.center_x * width),
        round(tile.height / 2 - view.center_y * height),
        round(width),
        round(height),
    )


def visible_source_rect(
    view: FeedView, tile: Tile, image_width: int, image_height: int
) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """Crop for the QImage path: the source rect, and the size to draw it at."""
    width, height = _fitted_size(tile)
    width *= view.zoom
    height *= view.zoom
    shown_width = min(width, float(tile.width))
    shown_height = min(height, float(tile.height))
    crop_width = max(1, round(image_width * shown_width / width))
    crop_height = max(1, round(image_height * shown_height / height))
    left = round(image_width * view.center_x - crop_width / 2)
    top = round(image_height * view.center_y - crop_height / 2)
    source = (
        max(0, min(image_width - crop_width, left)),
        max(0, min(image_height - crop_height, top)),
        crop_width,
        crop_height,
    )
    return source, (max(1, round(shown_width)), max(1, round(shown_height)))
