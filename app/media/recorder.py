"""Full-session and short-segment recording service."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import cv2

from app.config.settings import AppSettings
from app.core.models import MediaFrame, SessionPaths


class Recorder:
    """Writes a long session file and optional short segment files from the record tee.

    OpenCV VideoWriter is used for both; lazy-open on first frame per writer.
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._session_paths: SessionPaths | None = None
        self._feed_id = "default"
        self._long_active = False
        self._long_writer: cv2.VideoWriter | None = None
        self._long_output_path: Path | None = None
        self._long_frame_count = 0
        self._long_session_counter = 0
        self._short_writer: cv2.VideoWriter | None = None
        self._short_output_path: Path | None = None
        self._short_segment_index = 0
        self._short_frame_count = 0
        self._completed_segment_entries: list[dict[str, str | int]] = []
        self._manifest_path: Path | None = None
        self._fps_hint = settings.target_fps
        self._source_name = settings.default_source_name
        self._lock = threading.Lock()

    def begin_long_recording(
        self,
        session_paths: SessionPaths,
        source_name: str,
        fps_hint: float,
        feed_id: str = "default",
    ) -> None:
        """Start a new long (game) recording in the current session."""
        with self._lock:
            self._release_short_writer_locked()
            self._release_long_writer_locked()
            self._completed_segment_entries.clear()
            self._session_paths = session_paths
            self._feed_id = feed_id
            self._source_name = source_name
            self._fps_hint = max(fps_hint, 1.0)
            self._long_session_counter += 1
            feed_paths = session_paths.get_feed_paths(feed_id)
            feed_paths.recording_dir.mkdir(parents=True, exist_ok=True)
            self._manifest_path = feed_paths.recording_dir / self._settings.recording_manifest_filename
            self._long_output_path = self._long_path_for_take_locked(feed_paths.recording_dir)
            self._long_frame_count = 0
            self._short_segment_index = 0
            self._short_output_path = None
            self._short_frame_count = 0
            self._long_active = True

    def end_long_recording(self) -> None:
        """Stop long recording and any open short segment; write manifest."""
        with self._lock:
            if self._long_active:
                self._finalize_open_short_locked()
                self._write_manifest_locked()
            self._release_short_writer_locked()
            self._release_long_writer_locked()
            self._long_active = False
            self._long_output_path = None
            self._short_segment_index = 0
            self._short_output_path = None

    def advance_short_segment(self) -> bool:
        """Close the current short file and start numbering for the next segment.

        Returns True if a new segment will be opened on the next frame (long must be active).
        """
        with self._lock:
            if not self._long_active:
                return False
            self._finalize_open_short_locked()
            self._short_segment_index += 1
            self._short_output_path = None
            self._short_writer = None
            self._short_frame_count = 0
            return True

    def stop(self) -> None:
        """Release all writers (e.g. application shutdown)."""
        with self._lock:
            if self._long_active:
                self._finalize_open_short_locked()
                self._write_manifest_locked()
            self._release_short_writer_locked()
            self._release_long_writer_locked()
            self._long_active = False
            self._long_output_path = None
            self._manifest_path = None
            self._short_segment_index = 0
            self._short_output_path = None

    def write_frame(self, frame: MediaFrame) -> None:
        """Write a frame to active long/short writers."""
        with self._lock:
            if not self._long_active:
                return

            if self._long_writer is None:
                self._open_long_writer_locked(frame)
            if self._long_writer is None:
                return

            self._long_writer.write(frame.image_bgr)
            self._long_frame_count += 1

            if self._short_segment_index > 0:
                if self._short_writer is None:
                    self._open_short_writer_locked(frame)
                if self._short_writer is not None:
                    self._short_writer.write(frame.image_bgr)
                    self._short_frame_count += 1

    def is_recording(self) -> bool:
        """Return whether a long (game) recording is active."""
        return self._long_active

    def get_recording_target(self) -> Path | None:
        """Return the directory where full-session media should be written."""
        if self._long_output_path is not None:
            return self._long_output_path.parent
        if self._session_paths is None:
            return None
        return self._session_paths.get_feed_paths(self._feed_id).recording_dir

    def get_output_path(self) -> Path | None:
        """Return the current long recording file path."""
        return self._long_output_path

    def _long_path_for_take_locked(self, recording_dir: Path) -> Path:
        base = Path(self._settings.recording_filename)
        if self._long_session_counter <= 1:
            return recording_dir / base.name
        stem = base.stem
        suffix = base.suffix or ".mp4"
        return recording_dir / f"{stem}_{self._long_session_counter:03d}{suffix}"

    def _segments_dir_locked(self, recording_dir: Path) -> Path:
        sub = self._settings.short_segments_subdir.strip() or "segments"
        return recording_dir / sub

    def _short_path_for_index_locked(self, recording_dir: Path) -> Path:
        seg_dir = self._segments_dir_locked(recording_dir)
        prefix = self._settings.short_segment_filename_prefix.strip() or "segment"
        return seg_dir / f"{prefix}_{self._short_segment_index:04d}.mp4"

    def _open_long_writer_locked(self, frame: MediaFrame) -> None:
        assert self._long_output_path is not None
        self._long_writer = self._create_writer(self._long_output_path, frame)
        if self._long_writer is None:
            self._long_active = False

    def _open_short_writer_locked(self, frame: MediaFrame) -> None:
        assert self._session_paths is not None
        recording_dir = self._session_paths.get_feed_paths(self._feed_id).recording_dir
        self._short_output_path = self._short_path_for_index_locked(recording_dir)
        self._short_output_path.parent.mkdir(parents=True, exist_ok=True)
        self._short_writer = self._create_writer(self._short_output_path, frame)
        self._short_frame_count = 0

    def _create_writer(self, output_path: Path, frame: MediaFrame) -> cv2.VideoWriter | None:
        frame_height, frame_width = frame.image_bgr.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(
            str(output_path),
            fourcc,
            self._fps_hint,
            (frame_width, frame_height),
        )

        if not writer.isOpened():
            fallback_path = output_path.with_suffix(".avi")
            writer = cv2.VideoWriter(
                str(fallback_path),
                cv2.VideoWriter_fourcc(*"XVID"),
                self._fps_hint,
                (frame_width, frame_height),
            )
            if output_path == self._long_output_path:
                self._long_output_path = fallback_path
            elif output_path == self._short_output_path:
                self._short_output_path = fallback_path

        if not writer.isOpened():
            return None
        return writer

    def _finalize_open_short_locked(self) -> None:
        if self._short_writer is not None and self._short_output_path is not None:
            self._completed_segment_entries.append(
                {
                    "path": str(self._short_output_path),
                    "frame_count": self._short_frame_count,
                    "segment_index": self._short_segment_index,
                }
            )
        self._release_short_writer_locked()

    def _write_manifest_locked(self) -> None:
        if self._manifest_path is None:
            return

        manifest: dict[str, object] = {
            "feed_id": self._feed_id,
            "source_name": self._source_name,
            "long_output_path": str(self._long_output_path) if self._long_output_path else None,
            "long_frame_count": self._long_frame_count,
            "fps_hint": self._fps_hint,
            "segments": list(self._completed_segment_entries),
            "written_at": time.time(),
        }
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _release_long_writer_locked(self) -> None:
        if self._long_writer is not None:
            self._long_writer.release()
            self._long_writer = None

    def _release_short_writer_locked(self) -> None:
        if self._short_writer is not None:
            self._short_writer.release()
            self._short_writer = None
