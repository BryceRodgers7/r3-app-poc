"""Recorder long + short segment file behavior."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from app.config.settings import AppSettings
from app.core.models import AudioChunk, AudioFormat, MediaFrame, SessionPaths
from app.media.muxed_writer import MuxedWriterInfo
from app.media.recorder import Recorder


def _session_with_feed(temp: Path, feed_id: str = "feed_main") -> SessionPaths:
    root = temp / "session_001"
    rec = root / "recording"
    roll = root / "rolling"
    clips = root / "clips"
    for p in (root, rec, roll, clips):
        p.mkdir(parents=True, exist_ok=True)
    return SessionPaths(
        session_id="session_001",
        root_dir=root,
        recording_dir=rec,
        rolling_dir=roll,
        clips_dir=clips,
    )


def _frame(frame_id: int = 1) -> MediaFrame:
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    return MediaFrame(
        frame_id=frame_id,
        timestamp=100.0,
        image=image,
        source_name="Test",
        feed_id="feed_main",
    )


def _audio_chunk(timestamp: float = 100.0) -> AudioChunk:
    audio_format = AudioFormat(sample_rate=48_000, channels=2)
    return AudioChunk(
        timestamp=timestamp,
        data=b"\x00\x00" * audio_format.channels * 480,
        format=audio_format,
        source_name="Test",
        feed_id="feed_main",
    )


class _FakeMuxedWriter:
    def __init__(
        self,
        output_path: Path,
        *,
        fps_hint: float,
        audio_format: AudioFormat | None = None,
        audio_bitrate: int = 128_000,
    ) -> None:
        self.output_path = output_path
        self.video_frame_count = 0
        self.audio_bytes = 0
        self.info = MuxedWriterInfo(
            container="mp4",
            video_encoder="fake-h264",
            audio_encoder="fake-aac",
            output_path=output_path,
        )

    def write_frame(self, frame: MediaFrame) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(b"fake mp4")
        self.video_frame_count += 1

    def write_audio_chunk(self, chunk: AudioChunk) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.output_path.exists():
            self.output_path.write_bytes(b"fake mp4")
        self.audio_bytes += len(chunk.data)

    def close(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.output_path.exists():
            self.output_path.write_bytes(b"fake mp4")


class RecorderTests(unittest.TestCase):
    def test_idle_skips_write_frame(self) -> None:
        settings = AppSettings()
        rec = Recorder(settings)
        rec.write_frame(_frame())
        self.assertFalse(rec.is_recording())

    def test_long_recording_manifest_and_short_segments(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session = _session_with_feed(tmp_path)
            settings = AppSettings()
            settings.recording_filename = "game.mp4"
            settings.short_segments_subdir = "segments"
            settings.short_segment_filename_prefix = "rally"
            rec = Recorder(settings)
            with patch("app.media.recorder.MuxedMediaWriter", _FakeMuxedWriter):
                rec.begin_long_recording(session, "Cam", 15.0, feed_id="feed_main")
                self.assertTrue(rec.is_recording())
                f = _frame()
                rec.write_frame(f)
                rec.write_audio_chunk(_audio_chunk())
                rec.advance_short_segment()
                rec.write_frame(f)
                rec.write_audio_chunk(_audio_chunk(100.1))
                rec.write_frame(f)
                rec.write_audio_chunk(_audio_chunk(100.2))
                rec.advance_short_segment()
                rec.write_frame(f)
                rec.write_audio_chunk(_audio_chunk(100.3))
                rec.end_long_recording()
            self.assertFalse(rec.is_recording())

            feed_rec = session.get_feed_paths("feed_main").recording_dir
            long_files = list(feed_rec.glob("game.*"))
            self.assertTrue(long_files, "long file should exist")
            seg_dir = feed_rec / "segments"
            segs = sorted(seg_dir.glob("rally_*.mp4"))
            self.assertGreaterEqual(len(segs), 2)
            manifest_path = feed_rec / settings.recording_manifest_filename
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(data["feed_id"], "feed_main")
            self.assertEqual(data["source_name"], "Cam")
            self.assertIn("long_output_path", data)
            self.assertGreaterEqual(data["long_frame_count"], 4)
            self.assertTrue(data["has_audio"])
            self.assertGreater(data["long_audio_bytes"], 0)
            self.assertEqual(data["audio_format"]["sample_rate"], 48000)
            self.assertEqual(data["container"], "mp4")
            self.assertEqual(data["audio_codec"], "fake-aac")
            self.assertEqual(len(data["segments"]), 2)
            self.assertTrue(all(seg["has_audio"] for seg in data["segments"]))
            self.assertTrue(all(seg["path"].endswith(".mp4") for seg in data["segments"]))

    def test_second_long_session_uses_incremented_name(self) -> None:
        with TemporaryDirectory() as tmp:
            session = _session_with_feed(Path(tmp))
            settings = AppSettings()
            settings.recording_filename = "game.mp4"
            rec = Recorder(settings)
            with patch("app.media.recorder.MuxedMediaWriter", _FakeMuxedWriter):
                rec.begin_long_recording(session, "Cam", 15.0, "feed_main")
                rec.write_frame(_frame())
                rec.end_long_recording()
                rec.begin_long_recording(session, "Cam", 15.0, "feed_main")
                rec.write_frame(_frame(2))
                rec.end_long_recording()
            feed_rec = session.get_feed_paths("feed_main").recording_dir
            names = {p.name for p in feed_rec.glob("game*")}
            self.assertIn("game.mp4", names)
            self.assertTrue(any(n.startswith("game_00") for n in names))


if __name__ == "__main__":
    unittest.main()
