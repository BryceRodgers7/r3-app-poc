"""Full-session recording service."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import cv2

from app.config.settings import AppSettings
from app.core.models import MediaFrame, SessionPaths


class Recorder:
    """Tracks full-session recording state independently of playback.

    The current implementation still writes a single local video file through
    OpenCV, but it is now fed from the recording branch of the GStreamer tee.
    TODO: Replace this writer with a dedicated GStreamer encoder/filesink branch
    once the production media pipeline is introduced.
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._session_paths: SessionPaths | None = None
        self._is_recording = False
        self._writer: cv2.VideoWriter | None = None
        self._output_path: Path | None = None
        self._manifest_path: Path | None = None
        self._frame_count = 0
        self._fps_hint = settings.target_fps
        self._source_name = settings.default_source_name
        self._lock = threading.Lock()

    def start(self, session_paths: SessionPaths, source_name: str, fps_hint: float) -> None:
        """Prepare the recorder for a new session."""
        with self._lock:
            self._session_paths = session_paths
            self._source_name = source_name
            self._fps_hint = max(fps_hint, 1.0)
            self._output_path = session_paths.recording_dir / self._settings.recording_filename
            self._manifest_path = session_paths.recording_dir / self._settings.recording_manifest_filename
            self._frame_count = 0
            self._release_writer()
            self._is_recording = True

    def stop(self) -> None:
        """Stop recording while preserving session metadata."""
        with self._lock:
            if self._is_recording:
                self._write_manifest()
            self._release_writer()
            self._is_recording = False

    def write_frame(self, frame: MediaFrame) -> None:
        """Write a frame to the session recording without affecting playback."""
        with self._lock:
            if not self._is_recording:
                return

            if self._writer is None:
                self._open_writer(frame)

            if self._writer is None:
                return

            self._writer.write(frame.image_bgr)
            self._frame_count += 1

    def is_recording(self) -> bool:
        """Return whether the recorder is active."""
        return self._is_recording

    def get_recording_target(self) -> Path | None:
        """Return the directory where full-session media should be written."""
        if self._session_paths is None:
            return None
        return self._session_paths.recording_dir

    def get_output_path(self) -> Path | None:
        """Return the current recording file path."""
        return self._output_path

    def _open_writer(self, frame: MediaFrame) -> None:
        assert self._output_path is not None

        frame_height, frame_width = frame.image_bgr.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(
            str(self._output_path),
            fourcc,
            self._fps_hint,
            (frame_width, frame_height),
        )

        if not writer.isOpened():
            fallback_path = self._output_path.with_suffix(".avi")
            writer = cv2.VideoWriter(
                str(fallback_path),
                cv2.VideoWriter_fourcc(*"XVID"),
                self._fps_hint,
                (frame_width, frame_height),
            )
            self._output_path = fallback_path

        if not writer.isOpened():
            self._is_recording = False
            self._writer = None
            return

        self._writer = writer

    def _write_manifest(self) -> None:
        if self._manifest_path is None or self._output_path is None:
            return

        manifest = {
            "source_name": self._source_name,
            "output_path": str(self._output_path),
            "frame_count": self._frame_count,
            "fps_hint": self._fps_hint,
        }
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _release_writer(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
