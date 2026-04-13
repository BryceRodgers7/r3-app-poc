"""Renderer abstraction for one output surface."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from app.core.models import MediaFrame

if TYPE_CHECKING:
    from app.ui.video_widget import VideoWidget


class MultiFeedOutputRenderer(QObject):
    """Route frames to the correct `VideoWidget` by `MediaFrame.feed_id`."""

    def __init__(self) -> None:
        super().__init__()
        self._per_feed: dict[str, OutputRenderer] = {}

    def bind_feed_widget(self, feed_id: str, widget: "VideoWidget") -> None:
        """Attach one output renderer to a tile for this feed."""
        renderer = OutputRenderer()
        renderer.bind_widget(widget)
        self._per_feed[feed_id] = renderer

    def feed_ids(self) -> list[str]:
        """Return bound feed identifiers in insertion order."""
        return list(self._per_feed.keys())

    def show_frame(self, frame: MediaFrame) -> None:
        """Display a frame on the matching feed tile, if any."""
        renderer = self._per_feed.get(frame.feed_id)
        if renderer is not None:
            renderer.show_frame(frame)

    def show_placeholder_message(self, message: str) -> None:
        """Set the same placeholder text on every bound tile."""
        for renderer in self._per_feed.values():
            renderer.show_placeholder_message(message)

    def show_placeholder_for_feed(self, feed_id: str, message: str) -> None:
        """Set placeholder text for a single feed tile."""
        renderer = self._per_feed.get(feed_id)
        if renderer is not None:
            renderer.show_placeholder_message(message)

    def detach_all(self) -> None:
        """Disconnect all per-feed renderers."""
        for renderer in self._per_feed.values():
            renderer.detach_widget()
        self._per_feed.clear()


class OutputRenderer(QObject):
    """Own the widget binding for one displayed output."""

    frame_ready = Signal(object)
    placeholder_text_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._video_widget: VideoWidget | None = None

    def bind_widget(self, widget: "VideoWidget") -> None:
        """Bind the renderer to a widget."""
        if self._video_widget is widget:
            return
        self.detach_widget()
        self._video_widget = widget
        self.frame_ready.connect(widget.display_frame)
        self.placeholder_text_changed.connect(widget.set_placeholder_text)

    def show_placeholder_message(self, message: str) -> None:
        """Display a placeholder message on the bound widget."""
        self.placeholder_text_changed.emit(message)

    def show_frame(self, frame: MediaFrame) -> None:
        """Display a rendered frame on the bound widget."""
        self.frame_ready.emit(frame)

    def detach_widget(self) -> None:
        """Release the current widget binding, if any."""
        if self._video_widget is not None:
            self.frame_ready.disconnect(self._video_widget.display_frame)
            self.placeholder_text_changed.disconnect(self._video_widget.set_placeholder_text)
            self._video_widget = None
