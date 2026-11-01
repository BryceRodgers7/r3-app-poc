"""Grid of video tiles, one per enabled feed.

Nothing is drawn over the tiles — status chrome lives outside the
video panel.
"""

from __future__ import annotations

import math

from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.core.models import FeedDefinition, PlaybackMode
from app.ui.video_widget import VideoWidget


_FEED_ASPECT_RATIO = 16 / 9
_MAX_GRID_COLUMNS = 4
_GRID_SPACING = 4
_TITLE_SPACING = 2
# Low enough that the most-stacked grid `best_grid_columns` can pick
# still fits inside the control rail's own minimum height, so a reflow
# never forces the window to grow.
_TILE_MIN_HEIGHT = 90


def best_grid_columns(
    count: int,
    width: int,
    height: int,
    *,
    spacing: int = _GRID_SPACING,
    title_height: int = 0,
) -> int:
    """Return the column count giving each feed the largest picture.

    Scores each candidate by the 16:9 picture that fits in its cell, so
    the answer tracks the panel's shape: two feeds sit side by side in
    a wide panel and stack in a narrow one. Ties go to fewer columns.
    """
    best_columns = 1
    best_picture_width = 0.0
    for columns in range(1, min(count, _MAX_GRID_COLUMNS) + 1):
        rows = math.ceil(count / columns)
        cell_width = (width - spacing * (columns - 1)) / columns
        cell_height = (height - spacing * (rows - 1)) / rows - title_height
        picture_width = min(cell_width, cell_height * _FEED_ASPECT_RATIO)
        if picture_width > best_picture_width:
            best_columns = columns
            best_picture_width = picture_width
    return best_columns


class MultiFeedVideoPanel(QWidget):
    """One `VideoWidget` per enabled feed in a responsive grid."""

    def __init__(self, feeds: list[FeedDefinition], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("multiFeedVideoPanel")
        self._feeds = list(feeds)
        self._tiles: dict[str, VideoWidget] = {}
        # Phase 14.B: per-feed visibility. False means the camera
        # ribbon hid this feed's tile on THIS window; ingest and
        # recording are unaffected. Cell widgets are stashed in
        # `_cells` so `set_tile_visible` can reflow the grid without
        # re-creating them.
        self._cells: dict[str, QWidget] = {}
        self._visible: dict[str, bool] = {}
        self._title_height = 0
        # (visible feed ids, columns) last applied to the grid, so a
        # resize that doesn't change the arrangement leaves it untouched.
        self._arrangement: tuple[list[str], int] | None = None

        self._grid_host = QWidget(self)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(_GRID_SPACING)

        for feed in self._feeds:
            cell = QWidget(self._grid_host)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(_TITLE_SPACING)
            title = QLabel(feed.display_name, cell)
            title.setStyleSheet("color: #aeb6c2; font-size: 12px; font-weight: 600;")
            title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            tile = VideoWidget(cell)
            tile.setMinimumHeight(_TILE_MIN_HEIGHT)
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            cell_layout.addWidget(title)
            cell_layout.addWidget(tile, stretch=1)
            self._tiles[feed.feed_id] = tile
            self._cells[feed.feed_id] = cell
            self._visible[feed.feed_id] = True
            self._title_height = title.sizeHint().height() + _TITLE_SPACING

        self._relayout_grid()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._grid_host, stretch=1)

    @property
    def widgets_by_feed_id(self) -> dict[str, VideoWidget]:
        return dict(self._tiles)

    def apply_tile_visibility(
        self,
        mode: PlaybackMode,
        *,
        live_only_window: bool,
    ) -> None:
        """Toggle each tile between active preview styling and placeholder-style chrome.

        Slice 3.A.3 added the `live` flag so native-mode tiles can
        switch between the d3d11 surface (LIVE) and the QLabel layer
        (REPLAY/PAUSED — replay frames arrive via `display_frame`).
        """
        show_video = True
        if live_only_window:
            show_video = mode is not PlaybackMode.SOURCE_LOST
        else:
            show_video = mode in {
                PlaybackMode.LIVE,
                PlaybackMode.PAUSED,
                PlaybackMode.REPLAY,
            }
        live = mode == PlaybackMode.LIVE
        for tile in self._tiles.values():
            tile.set_video_surface_visible(show_video, live=live)

    def set_global_placeholder(self, text: str) -> None:
        """Apply the same placeholder to every tile (e.g. source lost)."""
        for tile in self._tiles.values():
            tile.set_placeholder_text(text)

    def set_tile_visible(self, feed_id: str, visible: bool) -> None:
        """Show or hide a feed's tile on this window only.

        Phase 14.B: driven by `CameraVisibilityRibbon`. Hiding a tile
        removes it from the responsive grid and reflows the remaining
        tiles to fill the freed space. Ingest and recording are not
        affected — this is a per-window UI toggle.

        No-op when `feed_id` isn't on this panel (defensive: the
        ribbon's feed list comes from the same `enabled_feeds` source
        the panel was constructed from, so the lookup should always
        match).
        """
        if feed_id not in self._cells:
            return
        if self._visible.get(feed_id, True) == visible:
            return
        self._visible[feed_id] = visible
        self._relayout_grid()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """The best column count depends on the panel's shape."""
        super().resizeEvent(event)
        self._relayout_grid()

    def _relayout_grid(self) -> None:
        """Re-pack visible cells into the grid that maximizes their size.

        Removes everything from `self._grid` first, then re-adds the
        currently-visible cells in feed-definition order. Runs on every
        resize, so it bails early unless the arrangement changed, and
        only hides cells that are leaving the grid — hiding one that
        stays would flash its native video surface.
        """
        visible_feeds = [
            feed for feed in self._feeds if self._visible.get(feed.feed_id, True)
        ]
        n = len(visible_feeds)
        cols = best_grid_columns(
            n, self.width(), self.height(), title_height=self._title_height
        )
        arrangement = ([feed.feed_id for feed in visible_feeds], cols)
        if arrangement == self._arrangement:
            return
        self._arrangement = arrangement
        for feed_id, cell in self._cells.items():
            self._grid.removeWidget(cell)
            if not self._visible.get(feed_id, True):
                cell.hide()
        for idx, feed in enumerate(visible_feeds):
            row, col = divmod(idx, cols)
            cell = self._cells[feed.feed_id]
            self._grid.addWidget(cell, row, col)
            cell.show()
        rows = math.ceil(n / cols)
        for row in range(self._grid.rowCount()):
            self._grid.setRowStretch(row, 1 if row < rows else 0)
        for col in range(self._grid.columnCount()):
            self._grid.setColumnStretch(col, 1 if col < cols else 0)
