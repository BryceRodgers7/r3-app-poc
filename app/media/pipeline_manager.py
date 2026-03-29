"""Placeholder home for the future GStreamer pipeline graph."""

from __future__ import annotations

from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer
from app.media.source_interface import SourceInterface


class PipelineManager:
    """Coordinates future media pipeline startup and shutdown."""

    def __init__(
        self,
        source: SourceInterface,
        preview_output: PreviewOutput,
        recorder: Recorder,
        replay_buffer: ReplayBuffer,
    ) -> None:
        self._source = source
        self._preview_output = preview_output
        self._recorder = recorder
        self._replay_buffer = replay_buffer
        self._preview_running = False
        self._recording_running = False
        self._replay_running = False

    def describe_architecture(self) -> str:
        """Describe the intended tee/fan-out architecture for later implementation."""
        return (
            "source -> decode/normalize -> tee -> "
            "[preview branch, recorder branch, rolling replay branch]"
        )

    def start_preview(self) -> None:
        """Start the preview branch without affecting recording or replay buffering."""
        # TODO: Build a sink path for the live preview widget.
        self._preview_running = True
        self._preview_output.show_placeholder_message("Live preview branch active")

    def start_recording(self) -> None:
        """Start the full-session recording branch."""
        # TODO: Bind muxer/filesink elements for continuous recording.
        self._recording_running = True

    def start_replay_buffer(self) -> None:
        """Start the rolling buffer branch."""
        # TODO: Build the segmenter and eviction strategy for replay storage.
        self._replay_running = True

    def stop_preview(self) -> None:
        """Stop only the preview branch."""
        self._preview_running = False

    def stop_recording(self) -> None:
        """Stop only the recording branch."""
        self._recording_running = False

    def stop_replay_buffer(self) -> None:
        """Stop only the rolling replay buffer branch."""
        self._replay_running = False

    def stop_all(self) -> None:
        """Stop all branches and disconnect the source."""
        self.stop_preview()
        self.stop_recording()
        self.stop_replay_buffer()
        self._source.disconnect_source()

    def is_source_connected(self) -> bool:
        """Return whether the underlying ingest source is connected."""
        return self._source.is_connected()

    def connect_source(self) -> bool:
        """Connect the source and return the result."""
        # TODO: Initialize the root GStreamer pipeline before returning.
        return self._source.connect_source()
