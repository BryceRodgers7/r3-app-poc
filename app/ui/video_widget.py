"""Qt widget for the transitional live/replay video surface."""

from __future__ import annotations

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QResizeEvent, QShowEvent
from PySide6.QtWidgets import QLabel, QStackedLayout, QVBoxLayout, QWidget

from app.core.models import MediaFrame, PlaybackOverlayInfo
from app.media.frame_overlay import build_playback_overlay_lines


class VideoWidget(QWidget):
    """Displays the selected frame and a playback-state overlay."""

    video_surface_resized = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoWidgetRoot")
        self._live_surface = QWidget(self)
        # `WA_NativeWindow` forces Qt to allocate a native child window so
        # GStreamer's video sink can render directly into it.
        # `WA_DontCreateNativeAncestors` is the partner flag — without it
        # Qt may walk up the parent chain creating native windows on
        # demand, which on Windows can leave the child winId() returning
        # a not-yet-realized HWND when MainWindow hasn't been shown yet.
        # That race is the most likely cause of the slice 3.A.3 "third
        # unintended window" symptom (one d3d11videosink ends up with
        # an unusable handle and falls back to creating its own window).
        self._live_surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._live_surface.setAttribute(
            Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True
        )
        self._live_surface.setStyleSheet("background-color: #101010;")
        # Force Qt to create the native child window up front so GStreamer can bind to it.
        self._live_surface.winId()

        self._frame_label = QLabel("Awaiting video...", self)
        self._frame_label.setObjectName("framePlaceholderLabel")
        self._frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_label.setStyleSheet("background-color: #101010; color: #f3f3f3;")
        self._current_image: QImage | None = None
        self._showing_video_surface = False
        # `qimage` (legacy / python_push sources) renders frames into the
        # QLabel via QImage. `native` (3.A.3+ native sources) lets a
        # GStreamer video sink render directly into `_live_surface`.
        self._render_mode: str = "qimage"

        self._surface_stack_host = QWidget(self)
        self._surface_stack_host.setObjectName("videoSurfaceHost")
        self._surface_stack = QStackedLayout(self._surface_stack_host)
        self._surface_stack.setContentsMargins(0, 0, 0, 0)
        self._surface_stack.addWidget(self._live_surface)
        self._surface_stack.addWidget(self._frame_label)
        self._surface_stack.setCurrentWidget(self._frame_label)

        self._playback_overlay_label = QLabel(self._surface_stack_host)
        self._playback_overlay_label.setObjectName("playbackOverlayLabel")
        self._playback_overlay_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._playback_overlay_label.setWordWrap(True)
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._playback_overlay_label.hide()

        self._display_resolution_label = QLabel(self._surface_stack_host)
        self._display_resolution_label.setObjectName("displayResolutionLabel")
        self._display_resolution_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._display_resolution_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Phase 6: degraded-replay indicator. Shown when this tile is
        # currently rendering a clamped freeze frame instead of an
        # exact-coverage frame (per §15.5 / §8.6.1) — typically because
        # this feed joined later than the playback target, was
        # disconnected at that point, or has reached the end of its
        # coverage.
        self._freeze_badge_label = QLabel("FROZEN", self._surface_stack_host)
        self._freeze_badge_label.setObjectName("freezeBadgeLabel")
        self._freeze_badge_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._freeze_badge_label.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self._freeze_badge_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addWidget(self._surface_stack_host, stretch=1)

        self.setMinimumHeight(420)
        self.setStyleSheet(
            """
            QWidget#videoWidgetRoot {
                background-color: #1f1f1f;
                border: 2px solid #505050;
                border-radius: 10px;
            }
            QWidget#videoSurfaceHost {
                background-color: #101010;
                border: none;
            }
            QLabel#framePlaceholderLabel {
                background-color: #101010;
                border: none;
                color: #f3f3f3;
                font-size: 20px;
                font-weight: 600;
            }
            QLabel#playbackOverlayLabel {
                background-color: rgba(24, 24, 24, 212);
                border: 1px solid #505050;
                border-radius: 10px;
                color: #f3f3f3;
                font-size: 15px;
                font-weight: 600;
                padding: 12px 14px;
            }
            QLabel#displayResolutionLabel {
                background-color: rgba(24, 24, 24, 212);
                border: 1px solid #505050;
                border-radius: 8px;
                color: #e8e8e8;
                font-size: 13px;
                font-weight: 500;
                padding: 8px 10px;
            }
            QLabel#freezeBadgeLabel {
                background-color: rgba(180, 90, 0, 220);
                border: 1px solid #ffb060;
                border-radius: 8px;
                color: #fff8e0;
                font-size: 13px;
                font-weight: 700;
                letter-spacing: 1px;
                padding: 6px 10px;
            }
            """
        )

    def set_freeze_indicator(self, visible: bool) -> None:
        """Show/hide the degraded-replay freeze badge (Phase 6).

        The badge is a small "FROZEN" pill rendered in the top-right
        corner of the tile, alongside the playback overlay. It tells
        the operator that this tile is currently displaying a clamped
        freeze frame rather than an exact-coverage match — useful when
        a feed joined later than the rewind target or was disconnected
        at that session time.
        """
        if visible:
            self._freeze_badge_label.adjustSize()
            self._layout_freeze_badge()
            self._freeze_badge_label.show()
            self._freeze_badge_label.raise_()
        else:
            self._freeze_badge_label.hide()

    def set_placeholder_text(self, text: str) -> None:
        """Update the placeholder message shown when no embedded video is active."""
        self._frame_label.setPixmap(QPixmap())
        self._frame_label.setText(text)
        self._update_display_resolution_overlay()
        self._stack_overlay_z_order()

    def set_playback_overlay(self, overlay: PlaybackOverlayInfo) -> None:
        """Render playback-state details over the video surface."""
        lines = build_playback_overlay_lines(overlay)
        if not lines:
            self._playback_overlay_label.hide()
            self._update_display_resolution_overlay()
            self._stack_overlay_z_order()
            return

        self._playback_overlay_label.setText("\n".join(lines))
        self._playback_overlay_label.setMaximumWidth(max(260, self._surface_stack_host.width() // 2))
        self._playback_overlay_label.adjustSize()
        self._update_display_resolution_overlay()
        self._layout_playback_overlay()
        self._playback_overlay_label.show()
        self._stack_overlay_z_order()

    def get_video_surface_handle(self) -> int:
        """Return the native child-window handle used by embedded video output."""
        return int(self._live_surface.winId())

    def set_render_mode(self, mode: str) -> None:
        """Set whether 'show video' should reveal the native surface or QImage label."""
        if mode not in {"qimage", "native"}:
            raise ValueError(f"Unsupported render mode: {mode!r}")
        self._render_mode = mode
        # If video is already meant to be visible, re-apply with the new mode.
        if self._showing_video_surface:
            self.set_video_surface_visible(True)

    def set_video_surface_visible(self, enabled: bool, *, live: bool = True) -> None:
        """Switch between active-video mode and placeholder mode.

        When `enabled` is True, the surface picked depends on render
        mode and the `live` flag:

        - `qimage` mode: always show the QLabel pixmap layer (legacy
          path; replay and live both render into the same QLabel via
          `display_frame`).
        - `native` mode + `live=True`: show `_live_surface` so the
          GStreamer d3d11videosink renders directly. No Python frame
          hop on the live path.
        - `native` mode + `live=False` (replay/pause): show the
          QLabel layer because replay frames arrive via `display_frame`
          from the segment decoder. `restore_live_surface` flips back
          when the operator returns to live.

        When `enabled` is False, always show the QLabel placeholder
        (e.g. SOURCE_LOST).
        """
        self._showing_video_surface = enabled
        if not enabled:
            self._surface_stack.setCurrentWidget(self._frame_label)
        elif self._render_mode == "native" and live:
            self._surface_stack.setCurrentWidget(self._live_surface)
        else:
            self._surface_stack.setCurrentWidget(self._frame_label)
        self._update_display_resolution_overlay()
        self._layout_playback_overlay()
        self._stack_overlay_z_order()
        self.video_surface_resized.emit()

    def display_frame(self, frame: MediaFrame) -> None:
        """Render a new frame inside the preview area.

        In native render mode this is the replay path — receiving a
        frame here means the playback controller is showing a non-live
        timestamp, so the QStackedLayout flips to the QLabel layer and
        leaves the d3d11 native surface running invisibly behind it.
        `restore_live_surface()` flips back when the operator returns
        to LIVE.
        """
        if self._render_mode == "native" and self._showing_video_surface:
            self._surface_stack.setCurrentWidget(self._frame_label)
        rgb_image = cv2.cvtColor(frame.image_bgr, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb_image.shape
        bytes_per_line = channels * width
        self._current_image = QImage(
            rgb_image.data,
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        ).copy()
        self._refresh_pixmap()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the rendered frame scaled to the current widget size."""
        super().resizeEvent(event)
        self._refresh_pixmap()
        self._layout_playback_overlay()
        self._layout_freeze_badge()
        self._stack_overlay_z_order()
        if self._showing_video_surface:
            self.video_surface_resized.emit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._update_display_resolution_overlay()
        self._stack_overlay_z_order()

    def _scaled_display_pixmap(self) -> QPixmap | None:
        if self._current_image is None:
            return None
        pixmap = QPixmap.fromImage(self._current_image)
        return pixmap.scaled(
            self._frame_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _refresh_pixmap(self) -> None:
        scaled_pixmap = self._scaled_display_pixmap()
        if scaled_pixmap is None:
            self._update_display_resolution_overlay()
            self._stack_overlay_z_order()
            return

        self._frame_label.setText("")
        self._frame_label.setPixmap(scaled_pixmap)
        self._update_display_resolution_overlay(scaled_pixmap)
        self._stack_overlay_z_order()

    def _update_display_resolution_overlay(self, scaled_pixmap: QPixmap | None = None) -> None:
        """Show pixel size of the fitted video (or viewport when no frame). Updates on resize."""
        if scaled_pixmap is None:
            scaled_pixmap = self._scaled_display_pixmap()

        if scaled_pixmap is not None and not scaled_pixmap.isNull():
            width, height = scaled_pixmap.width(), scaled_pixmap.height()
        else:
            width = max(0, self._frame_label.width())
            height = max(0, self._frame_label.height())

        self._display_resolution_label.setText(f"Display: {width}×{height} px")
        self._display_resolution_label.adjustSize()
        margin = 14
        self._display_resolution_label.move(
            margin,
            max(margin, self._surface_stack_host.height() - self._display_resolution_label.height() - margin),
        )
        self._display_resolution_label.show()

    def _stack_overlay_z_order(self) -> None:
        """Keep the playback pill above the display-size chip when both are visible."""
        self._display_resolution_label.raise_()
        if self._playback_overlay_label.isVisible():
            self._playback_overlay_label.raise_()
        if self._freeze_badge_label.isVisible():
            self._freeze_badge_label.raise_()

    def _layout_playback_overlay(self) -> None:
        if self._playback_overlay_label.text() == "":
            return
        side_margin = 18
        top_margin = 80
        self._playback_overlay_label.adjustSize()
        overlay_width = self._playback_overlay_label.width()
        # adjustSize() under-reports height for wrapped multi-line QLabels; use
        # heightForWidth when available so the first line isn't clipped.
        hfw = self._playback_overlay_label.heightForWidth(overlay_width)
        overlay_height = max(self._playback_overlay_label.height(), hfw)
        x_position = max(side_margin, self._surface_stack_host.width() - overlay_width - side_margin)
        self._playback_overlay_label.move(x_position, top_margin)
        self._playback_overlay_label.resize(overlay_width, overlay_height)

    def _layout_freeze_badge(self) -> None:
        """Pin the FROZEN badge to the top-right corner of the tile."""
        margin = 14
        self._freeze_badge_label.adjustSize()
        x_position = max(
            margin,
            self._surface_stack_host.width()
            - self._freeze_badge_label.width()
            - margin,
        )
        self._freeze_badge_label.move(x_position, margin)
