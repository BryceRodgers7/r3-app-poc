"""Zoom and pan of one video tile, as pure geometry.

A tile draws its feed two ways: a native d3d11 surface for live video and
a QImage layer for replay. Both take their picture position from the same
`FeedView`, so a view carries across live and replay. The view is
normalized to the source picture rather than to the tile, which is what
lets it survive tile resizes and grid reflows.

The native sink draws a zoomed picture by being given a render rectangle
as big as the whole zoomed picture, which the window then clips. That
surface has a ceiling (see `surface_zoom`). Past it, `native_view` cuts
the picture down to a half or a quarter of the frame before it reaches the
sink, so the rectangle only has to cover that part.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple


MIN_ZOOM = 1.0
MAX_ZOOM = 8.0
# The largest swapchain a tile asks the sink for, in pixels. This is the
# surface measured working on d3d11videosink (4x of a maximized 2322x1306
# tile is 9288x5224). A bare sink was seen to survive 16000x8992 here but
# at about 1.2 GB of shared GPU memory, which is not worth risking.
MAX_SURFACE_PIXELS = 9288 * 5224
# Direct3D 11 caps a texture, so a swapchain buffer, at 16384 on a side.
# A bare sink fails with a stream error past it.
MAX_SURFACE_DIMENSION = 16384
# How much of the frame is kept before the sink: all of it, a half, a
# quarter (per side). Few and fixed, because changing the crop's size makes
# the sink renegotiate, while sliding it does not.
CROP_LEVELS = (1, 2, 4)
ZOOM_PER_WHEEL_NOTCH = 1.25
# Two touches closer than this (tile pixels) give no usable pinch ratio.
_MIN_PINCH_DISTANCE = 4.0
# A rectangle may overshoot the surface limits by this much (crop sizes
# are rounded to even pixels).
_SURFACE_SLACK = 1.002


class Tile(NamedTuple):
    """The space a picture of this aspect ratio is fitted into.

    `crop_levels` is how far the native path may crop the frame before the
    sink; `(1,)` means it can't, so the surface limit is the zoom limit.
    """

    width: int
    height: int
    aspect_ratio: float
    crop_levels: tuple[int, ...] = CROP_LEVELS


class NativeView(NamedTuple):
    """What the native sink is told for a view: a crop, and where to draw what is left.

    `crop` is `(left, top, right, bottom)` source pixels cut away before
    the sink (all zero for none). `rect` is `(x, y, width, height)`: where
    the remaining picture lands in the tile, overhanging it when zoomed.
    `level` is the crop level in use, kept so the next view can stay on it.
    """

    crop: tuple[int, int, int, int]
    rect: tuple[int, int, int, int]
    level: int = 1


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


def surface_zoom(tile: Tile) -> float:
    """The most a tile zooms by drawing the whole picture into one sink surface."""
    width, height = _fitted_size(tile)
    if width <= 0 or height <= 0:
        return MAX_ZOOM
    by_pixels = math.sqrt(MAX_SURFACE_PIXELS / (width * height))
    by_dimension = MAX_SURFACE_DIMENSION / max(width, height)
    return max(MIN_ZOOM, min(by_pixels, by_dimension))


def max_zoom(tile: Tile) -> float:
    """The most this tile can zoom: `MAX_ZOOM`, or less if no crop level reaches it.

    Each crop level covers zooms from itself up to itself times
    `surface_zoom`, and the levels must run on without a gap. A normal tile
    gets all the way to `MAX_ZOOM`; one too big for that is held to the
    last zoom it can draw. The replay crop has no such limit, but a view
    moves between live and replay, so both obey the same one.
    """
    reach = surface_zoom(tile)
    for level in tile.crop_levels:
        if level > 1 and level <= reach:
            reach = level * surface_zoom(tile)
    return max(MIN_ZOOM, min(MAX_ZOOM, reach))


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


def _picture(view: FeedView, tile: Tile) -> tuple[float, float, float, float]:
    """Where the whole picture lands in the tile, unrounded."""
    width, height = _fitted_size(tile)
    width *= view.zoom
    height *= view.zoom
    return (
        tile.width / 2 - view.center_x * width,
        tile.height / 2 - view.center_y * height,
        width,
        height,
    )


def picture_rect(view: FeedView, tile: Tile) -> tuple[int, int, int, int]:
    """Where the whole picture lands, as `(x, y, width, height)` in the tile.

    Overhangs the tile when zoomed; a native sink is clipped by its window.
    """
    x, y, width, height = _picture(view, tile)
    return round(x), round(y), round(width), round(height)


def _even(value: float) -> int:
    """Nearest even integer: video formats with subsampled colour need even crops."""
    return int(round(value / 2)) * 2


def _surface_fits(width: float, height: float) -> bool:
    return (
        width * height <= MAX_SURFACE_PIXELS * _SURFACE_SLACK
        and max(width, height) <= MAX_SURFACE_DIMENSION * _SURFACE_SLACK
    )


def _crop_for(
    level: int,
    visible: tuple[float, float, float, float],
    source: tuple[int, int],
    previous: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int]:
    """`(left, top, width, height)` of the kept part of the frame, in source pixels.

    `visible` is the part the tile shows, as fractions of the frame
    `(x0, y0, x1, y1)`. A crop already in use is kept while it still holds
    all of it, so panning slides the crop only now and then.
    """
    source_width, source_height = source
    width = min(source_width, _even(source_width / level))
    height = min(source_height, _even(source_height / level))
    x0, y0, x1, y1 = visible
    if previous is not None and previous[2:] == (width, height):
        left, top = previous[:2]
        if (
            left <= x0 * source_width
            and (left + width) >= x1 * source_width
            and top <= y0 * source_height
            and (top + height) >= y1 * source_height
        ):
            return left, top, width, height
    # Centered on what is shown, so there is room to pan either way.
    left = _even((x0 + x1) / 2 * source_width - width / 2)
    top = _even((y0 + y1) / 2 * source_height - height / 2)
    left = max(0, min(source_width - width, left)) // 2 * 2
    top = max(0, min(source_height - height, top)) // 2 * 2
    return left, top, width, height


def native_view(
    view: FeedView,
    tile: Tile,
    source: tuple[int, int],
    previous: NativeView | None = None,
) -> NativeView:
    """Plan how the native sink shows `view`: what to crop, and where to draw it.

    Zoom that fits in one sink surface is the render rectangle alone, as
    before. Past that, part of the frame is cropped away before the sink
    and the rectangle covers only the rest, so the surface stays bounded
    while the zoom keeps growing. `source` is the frame size in pixels;
    `previous` (the last plan) is what lets the plan stay on its crop level
    and crop position instead of changing them on every pan.
    """
    source_width, source_height = source
    x, y, width, height = _picture(view, tile)
    visible = (
        max(0.0, -x / width),
        max(0.0, -y / height),
        min(1.0, (tile.width - x) / width),
        min(1.0, (tile.height - y) / height),
    )
    visible_width = visible[2] - visible[0]
    visible_height = visible[3] - visible[1]
    previous_crop = None
    if previous is not None and previous.level > 1:
        left, top, right, bottom = previous.crop
        previous_crop = (
            left,
            top,
            source_width - left - right,
            source_height - top - bottom,
        )

    levels = [level for level in tile.crop_levels if level >= 1] or [1]
    if previous is not None and previous.level in levels:
        levels.remove(previous.level)
        levels.insert(0, previous.level)  # stay put while it still works
    fallback: NativeView | None = None
    for level in levels:
        if level == 1:
            crop = (0, 0, 0, 0)
            kept = (0.0, 0.0, 1.0, 1.0)
        else:
            left, top, kept_width, kept_height = _crop_for(
                level, visible, source, previous_crop
            )
            crop = (
                left,
                top,
                source_width - left - kept_width,
                source_height - top - kept_height,
            )
            kept = (
                left / source_width,
                top / source_height,
                (left + kept_width) / source_width,
                (top + kept_height) / source_height,
            )
        holds_view = (
            kept[2] - kept[0] >= visible_width - 1e-9
            and kept[3] - kept[1] >= visible_height - 1e-9
        )
        rect_width = (kept[2] - kept[0]) * width
        rect_height = (kept[3] - kept[1]) * height
        plan = NativeView(
            crop,
            (
                round(x + kept[0] * width),
                round(y + kept[1] * height),
                round(rect_width),
                round(rect_height),
            ),
            level,
        )
        if holds_view and _surface_fits(rect_width, rect_height):
            return plan
        if holds_view and (
            fallback is None
            or rect_width * rect_height < fallback.rect[2] * fallback.rect[3]
        ):
            fallback = plan  # shows everything, if over the surface budget
    if fallback is not None:
        return fallback
    # Nothing both holds the view and fits (a tile far past any real size):
    # show the whole picture rather than cut a visible part off.
    return NativeView((0, 0, 0, 0), picture_rect(view, tile), 1)


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
