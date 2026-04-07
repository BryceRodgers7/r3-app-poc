"""Coordinator for per-feed recording services."""

from __future__ import annotations

from app.media.recorder import Recorder


class RecordingManager:
    """Track recorder instances by feed identifier."""

    def __init__(self) -> None:
        self._recorders: dict[str, Recorder] = {}

    def register(self, feed_id: str, recorder: Recorder) -> None:
        """Register a recorder for a feed."""
        self._recorders[feed_id] = recorder

    def get(self, feed_id: str) -> Recorder:
        """Return the recorder for a feed."""
        return self._recorders[feed_id]

    def is_recording(self, feed_id: str) -> bool:
        """Return whether the feed recorder is active."""
        recorder = self._recorders.get(feed_id)
        return recorder.is_recording() if recorder is not None else False

    def is_any_recording(self) -> bool:
        """Return whether any recorder is active."""
        return any(recorder.is_recording() for recorder in self._recorders.values())

    def stop_all(self) -> None:
        """Stop all registered recorders."""
        for recorder in self._recorders.values():
            recorder.stop()
