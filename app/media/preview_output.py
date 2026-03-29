"""Preview output ownership for the live video surface."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ui.video_widget import VideoWidget


class PreviewOutput:
    """Owns the widget that will eventually host the live preview sink."""

    def __init__(self) -> None:
        self._video_widget: VideoWidget | None = None

    def bind_widget(self, widget: VideoWidget) -> None:
        """Register the widget that should display preview output."""
        self._video_widget = widget

    def show_placeholder_message(self, message: str) -> None:
        """Update the placeholder video surface text."""
        if self._video_widget is not None:
            self._video_widget.set_overlay_text(message)

    def detach_widget(self) -> None:
        """Release the current preview widget binding."""
        self._video_widget = None
