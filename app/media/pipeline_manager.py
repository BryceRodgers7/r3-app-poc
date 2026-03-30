"""Placeholder home for the future GStreamer pipeline graph."""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.core.models import MediaFrame, SessionPaths
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
        self._frame_callback: Callable[[MediaFrame], None] | None = None
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._capture_lock = threading.Lock()

    def describe_architecture(self) -> str:
        """Describe the intended tee/fan-out architecture for later implementation."""
        return (
            "source -> decode/normalize -> tee -> "
            "[preview branch, recorder branch, rolling replay branch]"
        )

    def start_preview(self) -> None:
        """Start the preview branch without affecting recording or replay buffering."""
        self._preview_running = True
        self._preview_output.show_placeholder_message("Starting live preview...")
        self._ensure_capture_loop()

    def start_recording(self, session_paths: SessionPaths) -> None:
        """Start the full-session recording branch."""
        self._recorder.start(
            session_paths=session_paths,
            source_name=self._source.get_display_name(),
            fps_hint=self._source.get_nominal_fps(),
        )
        self._recording_running = True
        self._ensure_capture_loop()

    def start_replay_buffer(self, session_paths: SessionPaths) -> None:
        """Start the rolling buffer branch."""
        self._replay_buffer.start(session_paths)
        self._replay_running = True
        self._ensure_capture_loop()

    def stop_preview(self) -> None:
        """Stop only the preview branch."""
        self._preview_running = False

    def stop_recording(self) -> None:
        """Stop only the recording branch."""
        self._recording_running = False
        self._recorder.stop()

    def stop_replay_buffer(self) -> None:
        """Stop only the rolling replay buffer branch."""
        self._replay_running = False
        self._replay_buffer.stop()

    def stop_all(self) -> None:
        """Stop all branches and disconnect the source."""
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None
        self.stop_preview()
        self.stop_recording()
        self.stop_replay_buffer()
        self._source.disconnect_source()

    def is_source_connected(self) -> bool:
        """Return whether the underlying ingest source is connected."""
        return self._source.is_connected()

    def connect_source(self) -> bool:
        """Connect the source and return the result."""
        # TODO: Replace the temporary capture loop with a GStreamer root pipeline and tee.
        return self._source.connect_source()

    def set_frame_callback(self, callback: Callable[[MediaFrame], None]) -> None:
        """Register the controller callback for incoming live frames."""
        self._frame_callback = callback

    def get_source_name(self) -> str:
        """Return the current source display name."""
        return self._source.get_display_name()

    def _ensure_capture_loop(self) -> None:
        with self._capture_lock:
            if self._capture_thread is not None and self._capture_thread.is_alive():
                return
            self._stop_event.clear()
            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                name="capture-loop",
                daemon=True,
            )
            self._capture_thread.start()

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            frame = self._source.read_frame()
            if frame is None:
                continue

            # TODO: Replace this temporary Python-level fan-out with a GStreamer
            # tee once preview, recording, and replay all hang off one pipeline.
            if self._replay_running:
                self._replay_buffer.append_frame(frame)

            if self._recording_running:
                self._recorder.write_frame(frame)

            if self._preview_running and self._frame_callback is not None:
                self._frame_callback(frame)
