"""Grid of video tiles with a single panel-level playback overlay."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.core.models import FeedDefinition, PlaybackMode, PlaybackOverlayInfo
from app.media.frame_overlay import build_playback_overlay_lines
from app.ui.video_widget import VideoWidget


class MultiFeedVideoPanel(QWidget):
    """One `VideoWidget` per enabled feed in a responsive grid."""

    def __init__(self, feeds: list[FeedDefinition], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("multiFeedVideoPanel")
        self._feeds = list(feeds)
        self._tiles: dict[str, VideoWidget] = {}

        self._grid_host = QWidget(self)
        grid = QGridLayout(self._grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)

        n = len(self._feeds)
        cols = min(4, max(1, math.ceil(math.sqrt(n)))) if n else 1
        for idx, feed in enumerate(self._feeds):
            row, col = divmod(idx, cols)
            cell = QWidget(self._grid_host)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(6)
            title = QLabel(feed.display_name, cell)
            title.setStyleSheet("color: #e8e8e8; font-size: 14px; font-weight: 600;")
            title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            tile = VideoWidget(cell)
            tile.setMinimumHeight(160)
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            cell_layout.addWidget(title)
            cell_layout.addWidget(tile, stretch=1)
            grid.addWidget(cell, row, col)
            self._tiles[feed.feed_id] = tile

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._grid_host, stretch=1)

        self._playback_overlay_label = QLabel(self)
        self._playback_overlay_label.setObjectName("multiFeedPlaybackOverlayLabel")
        self._playback_overlay_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._playback_overlay_label.setWordWrap(True)
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._playback_overlay_label.hide()
        self.setStyleSheet(
            """
            QLabel#multiFeedPlaybackOverlayLabel {
                background-color: rgba(24, 24, 24, 212);
                border: 1px solid #505050;
                border-radius: 10px;
                color: #f3f3f3;
                font-size: 15px;
                font-weight: 600;
                padding: 12px 14px;
            }
            """
        )

    @property
    def widgets_by_feed_id(self) -> dict[str, VideoWidget]:
        return dict(self._tiles)

    def set_playback_overlay(self, overlay: PlaybackOverlayInfo) -> None:
        """Show one playback-status pill for the whole panel."""
        lines = build_playback_overlay_lines(overlay)
        if not lines:
            self._playback_overlay_label.hide()
            self._playback_overlay_label.setText("")
            return
        self._playback_overlay_label.setText("\n".join(lines))
        self._playback_overlay_label.setMaximumWidth(max(260, self.width() // 2))
        self._playback_overlay_label.adjustSize()
        self._layout_playback_overlay()
        self._playback_overlay_label.show()
        self._playback_overlay_label.raise_()

    def apply_tile_visibility(
        self,
        mode: PlaybackMode,
        *,
        program_live_only: bool,
    ) -> None:
        """Toggle each tile between active preview styling and placeholder-style chrome.

        Slice 3.A.3 added the `live` flag so native-mode tiles can
        switch between the d3d11 surface (LIVE) and the QLabel layer
        (REPLAY/PAUSED — replay frames arrive via `display_frame`).
        """
        show_video = True
        if program_live_only:
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

    def apply_freeze_indicators(self, feeds_in_freeze: tuple[str, ...] | list[str]) -> None:
        """Phase 6: show the FROZEN badge on each tile whose feed_id is in the list.

        Tiles not in the list have their badge hidden. `feeds_in_freeze`
        comes from `UiState.feeds_in_freeze_frame`, populated by the
        controller's multi-feed render loop when a tile's
        `nearest_frame_location` returned a clamped (`is_freeze=True`)
        location.
        """
        freeze_set = set(feeds_in_freeze)
        for feed_id, tile in self._tiles.items():
            tile.set_freeze_indicator(feed_id in freeze_set)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_playback_overlay()
        if self._playback_overlay_label.isVisible():
            self._playback_overlay_label.raise_()

    def _layout_playback_overlay(self) -> None:
        if self._playback_overlay_label.text() == "":
            return
        margin = 18
        self._playback_overlay_label.adjustSize()
        overlay_width = self._playback_overlay_label.width()
        overlay_height = self._playback_overlay_label.height()
        x_position = max(margin, self.width() - overlay_width - margin)
        self._playback_overlay_label.move(x_position, margin)
        self._playback_overlay_label.resize(overlay_width, overlay_height)
