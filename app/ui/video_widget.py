"""Qt widget for the transitional live/replay video surface."""

from __future__ import annotations

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
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
        self._live_surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._live_surface.setStyleSheet("background-color: #101010;")
        # Force Qt to create the native child window up front so GStreamer can bind to it.
        self._live_surface.winId()

        self._frame_label = QLabel("Awaiting video...", self)
        self._frame_label.setObjectName("framePlaceholderLabel")
        self._frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_label.setStyleSheet("background-color: #101010; color: #f3f3f3;")
        self._current_image: QImage | None = None
        self._showing_video_surface = False

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
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._playback_overlay_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._playback_overlay_label.winId()
        self._playback_overlay_label.hide()

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
            """
        )

    def set_placeholder_text(self, text: str) -> None:
        """Update the placeholder message shown when no embedded video is active."""
        self._frame_label.setPixmap(QPixmap())
        self._frame_label.setText(text)

    def set_playback_overlay(self, overlay: PlaybackOverlayInfo) -> None:
        """Render playback-state details over the video surface."""
        lines = build_playback_overlay_lines(overlay)
        if not lines:
            self._playback_overlay_label.hide()
            return

        self._playback_overlay_label.setText("\n".join(lines))
        self._playback_overlay_label.setMaximumWidth(max(260, self._surface_stack_host.width() // 2))
        self._playback_overlay_label.adjustSize()
        self._layout_playback_overlay()
        self._playback_overlay_label.show()
        self._playback_overlay_label.raise_()

    def get_video_surface_handle(self) -> int:
        """Return the native child-window handle used by embedded video output."""
        return int(self._live_surface.winId())

    def set_video_surface_visible(self, enabled: bool) -> None:
        """Switch between embedded video output and the fallback placeholder label."""
        if enabled == self._showing_video_surface:
            return
        self._showing_video_surface = enabled
        current_widget = self._live_surface if enabled else self._frame_label
        self._surface_stack.setCurrentWidget(current_widget)
        self._layout_playback_overlay()
        if enabled:
            self.video_surface_resized.emit()

    def display_frame(self, frame: MediaFrame) -> None:
        """Render a new frame inside the preview area."""
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
        if self._showing_video_surface:
            self.video_surface_resized.emit()

    def _refresh_pixmap(self) -> None:
        if self._current_image is None:
            return

        pixmap = QPixmap.fromImage(self._current_image)
        scaled_pixmap = pixmap.scaled(
            self._frame_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._frame_label.setText("")
        self._frame_label.setPixmap(scaled_pixmap)

    def _layout_playback_overlay(self) -> None:
        if self._playback_overlay_label.text() == "":
            return
        margin = 18
        self._playback_overlay_label.adjustSize()
        overlay_width = self._playback_overlay_label.width()
        overlay_height = self._playback_overlay_label.height()
        x_position = max(margin, self._surface_stack_host.width() - overlay_width - margin)
        self._playback_overlay_label.move(x_position, margin)
        self._playback_overlay_label.resize(overlay_width, overlay_height)
        self._playback_overlay_label.raise_()
