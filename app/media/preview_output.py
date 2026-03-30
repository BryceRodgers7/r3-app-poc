"""Preview output ownership for the live video surface."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from app.core.models import MediaFrame

if TYPE_CHECKING:
    from app.ui.video_widget import VideoWidget


class PreviewOutput(QObject):
    """Owns the widget that will eventually host the live preview sink."""

    frame_ready = Signal(object)
    overlay_text_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._video_widget: VideoWidget | None = None

    def bind_widget(self, widget: VideoWidget) -> None:
        """Register the widget that should display preview output."""
        self._video_widget = widget
        self.frame_ready.connect(widget.display_frame)
        self.overlay_text_changed.connect(widget.set_overlay_text)

    def show_placeholder_message(self, message: str) -> None:
        """Update the placeholder video surface text."""
        self.overlay_text_changed.emit(message)

    def show_frame(self, frame: MediaFrame) -> None:
        """Send the selected frame to the bound widget."""
        self.frame_ready.emit(frame)

    def detach_widget(self) -> None:
        """Release the current preview widget binding."""
        if self._video_widget is not None:
            self.frame_ready.disconnect(self._video_widget.display_frame)
            self.overlay_text_changed.disconnect(self._video_widget.set_overlay_text)
        self._video_widget = None
