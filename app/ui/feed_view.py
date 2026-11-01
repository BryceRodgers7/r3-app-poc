"""Zoom and pan of one video tile, as pure geometry.

A tile draws its feed two ways: a native d3d11 surface for live video and
a QImage layer for replay. Both take their picture position from the same
`FeedView`, so a view carries across live and replay. The view is
normalized to the source picture rather than to the tile, which is what
lets it survive tile resizes and grid reflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


MIN_ZOOM = 1.0
# The sink's swapchain is as large as the zoomed picture, so this also
# bounds GPU memory: 4x of a maximized tile is a 9288x5224 surface.
MAX_ZOOM = 4.0
ZOOM_PER_WHEEL_NOTCH = 1.25


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


def clamped(view: FeedView, tile: Tile) -> FeedView:
    """Return `view` adjusted so the tile shows no space it could fill."""
    zoom = max(MIN_ZOOM, min(MAX_ZOOM, view.zoom))
    width, height = _fitted_size(tile)
    return FeedView(
        zoom,
        _clamped_center(view.center_x, width * zoom, tile.width),
        _clamped_center(view.center_y, height * zoom, tile.height),
    )


def zoomed(
    view: FeedView, notches: float, anchor_x: float, anchor_y: float, tile: Tile
) -> FeedView:
    """Zoom by wheel `notches`, keeping the point under the cursor in place."""
    zoom = max(MIN_ZOOM, min(MAX_ZOOM, view.zoom * ZOOM_PER_WHEEL_NOTCH**notches))
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
