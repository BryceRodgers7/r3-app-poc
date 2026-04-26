"""Full-session and short-segment recording service."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.config.settings import AppSettings
from app.core.models import AudioChunk, AudioFormat, MediaFrame, SessionPaths
from app.media.muxed_writer import MuxedMediaWriter, MuxedWriterInfo


class Recorder:
    """Writes long session and short segment files from the record tee."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._session_paths: SessionPaths | None = None
        self._feed_id = "default"
        self._long_active = False
        self._long_writer: MuxedMediaWriter | None = None
        self._long_output_path: Path | None = None
        self._long_frame_count = 0
        self._long_audio_bytes = 0
        self._long_audio_format: AudioFormat | None = None
        self._long_writer_info: MuxedWriterInfo | None = None
        self._long_session_counter = 0
        self._short_writer: MuxedMediaWriter | None = None
        self._short_output_path: Path | None = None
        self._short_writer_info: MuxedWriterInfo | None = None
        self._short_segment_index = 0
        self._short_frame_count = 0
        self._short_audio_bytes = 0
        self._completed_segment_entries: list[dict[str, str | int | bool | None]] = []
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
        """Start a new long recording in the current session."""
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
            self._long_audio_bytes = 0
            self._long_audio_format = None
            self._long_writer_info = None
            self._short_segment_index = 0
            self._short_output_path = None
            self._short_writer_info = None
            self._short_frame_count = 0
            self._short_audio_bytes = 0
            self._long_active = True

    def end_long_recording(self) -> None:
        """Stop long recording and any open short segment; write manifest."""
        with self._lock:
            if self._long_active:
                self._finalize_open_short_locked()
                self._release_long_writer_locked()
                self._write_manifest_locked()
            self._release_short_writer_locked()
            self._release_long_writer_locked()
            self._long_active = False
            self._long_output_path = None
            self._short_segment_index = 0
            self._short_output_path = None
            self._short_writer_info = None

    def advance_short_segment(self) -> bool:
        """Close the current short file and start numbering for the next segment."""
        with self._lock:
            if not self._long_active:
                return False
            self._finalize_open_short_locked()
            self._short_segment_index += 1
            self._short_output_path = None
            self._short_writer = None
            self._short_writer_info = None
            self._short_frame_count = 0
            self._short_audio_bytes = 0
            return True

    def stop(self) -> None:
        """Release all writers, for example during application shutdown."""
        with self._lock:
            if self._long_active:
                self._finalize_open_short_locked()
                self._release_long_writer_locked()
                self._write_manifest_locked()
            self._release_short_writer_locked()
            self._release_long_writer_locked()
            self._long_active = False
            self._long_output_path = None
            self._manifest_path = None
            self._short_segment_index = 0
            self._short_output_path = None
            self._short_writer_info = None

    def write_frame(self, frame: MediaFrame) -> None:
        """Write a frame to active long/short muxed outputs."""
        with self._lock:
            if not self._long_active:
                return

            if self._long_writer is None:
                self._open_long_writer_locked()
            if self._long_writer is not None:
                self._long_writer.write_frame(frame)
                self._sync_long_stats_locked()

            if self._short_segment_index > 0:
                if self._short_writer is None:
                    self._open_short_writer_locked()
                if self._short_writer is not None:
                    self._short_writer.write_frame(frame)
                    self._sync_short_stats_locked()

    def write_audio_chunk(self, chunk: AudioChunk) -> None:
        """Write a PCM audio chunk to active long/short muxed outputs."""
        with self._lock:
            if not self._long_active:
                return

            self._long_audio_format = chunk.format
            if self._long_writer is None:
                self._open_long_writer_locked(audio_format=chunk.format)
            if self._long_writer is not None:
                self._long_writer.write_audio_chunk(chunk)
                self._sync_long_stats_locked()

            if self._short_segment_index > 0:
                if self._short_writer is None:
                    self._open_short_writer_locked(audio_format=chunk.format)
                if self._short_writer is not None:
                    self._short_writer.write_audio_chunk(chunk)
                    self._sync_short_stats_locked()

    def is_recording(self) -> bool:
        """Return whether a long recording is active."""
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

    def _open_long_writer_locked(self, audio_format: AudioFormat | None = None) -> None:
        assert self._long_output_path is not None
        self._long_writer = MuxedMediaWriter(
            self._long_output_path,
            fps_hint=self._fps_hint,
            audio_format=audio_format or self._long_audio_format,
            audio_bitrate=self._settings.audio_bitrate,
        )

    def _open_short_writer_locked(self, audio_format: AudioFormat | None = None) -> None:
        assert self._session_paths is not None
        recording_dir = self._session_paths.get_feed_paths(self._feed_id).recording_dir
        self._short_output_path = self._short_path_for_index_locked(recording_dir)
        self._short_output_path.parent.mkdir(parents=True, exist_ok=True)
        self._short_writer = MuxedMediaWriter(
            self._short_output_path,
            fps_hint=self._fps_hint,
            audio_format=audio_format or self._long_audio_format,
            audio_bitrate=self._settings.audio_bitrate,
        )

    def _finalize_open_short_locked(self) -> None:
        if self._short_writer is not None:
            self._release_short_writer_locked()
        if self._short_output_path is not None and (self._short_frame_count > 0 or self._short_audio_bytes > 0):
            self._completed_segment_entries.append(
                {
                    "path": str(self._short_output_path),
                    "frame_count": self._short_frame_count,
                    "has_audio": self._short_audio_bytes > 0,
                    "audio_bytes": self._short_audio_bytes,
                    "segment_index": self._short_segment_index,
                    "container": self._short_writer_info.container if self._short_writer_info else None,
                    "video_codec": self._short_writer_info.video_encoder if self._short_writer_info else None,
                    "audio_codec": self._short_writer_info.audio_encoder if self._short_writer_info else None,
                }
            )

    def _write_manifest_locked(self) -> None:
        if self._manifest_path is None:
            return

        manifest: dict[str, object] = {
            "feed_id": self._feed_id,
            "source_name": self._source_name,
            "long_output_path": str(self._long_output_path) if self._long_output_path else None,
            "long_frame_count": self._long_frame_count,
            "has_audio": self._long_audio_bytes > 0,
            "long_audio_bytes": self._long_audio_bytes,
            "audio_format": self._audio_format_manifest_locked(),
            "container": self._long_writer_info.container if self._long_writer_info else None,
            "video_codec": self._long_writer_info.video_encoder if self._long_writer_info else None,
            "audio_codec": self._long_writer_info.audio_encoder if self._long_writer_info else None,
            "fps_hint": self._fps_hint,
            "segments": list(self._completed_segment_entries),
            "written_at": time.time(),
        }
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _release_long_writer_locked(self) -> None:
        if self._long_writer is not None:
            self._long_writer.close()
            self._sync_long_stats_locked()
            self._long_writer = None

    def _release_short_writer_locked(self) -> None:
        if self._short_writer is not None:
            self._short_writer.close()
            self._sync_short_stats_locked()
            self._short_writer = None

    def _sync_long_stats_locked(self) -> None:
        if self._long_writer is None:
            return
        self._long_output_path = self._long_writer.output_path
        self._long_frame_count = self._long_writer.video_frame_count
        self._long_audio_bytes = self._long_writer.audio_bytes
        self._long_writer_info = self._long_writer.info

    def _sync_short_stats_locked(self) -> None:
        if self._short_writer is None:
            return
        self._short_output_path = self._short_writer.output_path
        self._short_frame_count = self._short_writer.video_frame_count
        self._short_audio_bytes = self._short_writer.audio_bytes
        self._short_writer_info = self._short_writer.info

    def _audio_format_manifest_locked(self) -> dict[str, int | str] | None:
        audio_format = self._long_audio_format
        if audio_format is None:
            return None
        return {
            "sample_rate": audio_format.sample_rate,
            "channels": audio_format.channels,
            "sample_format": audio_format.sample_format,
        }