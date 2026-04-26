"""Focused tests for the disk-backed replay store."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app.core.models import AudioChunk, AudioFormat, MediaFrame, SessionPaths
from app.media.muxed_writer import MuxedWriterInfo
from app.media.replay_buffer import ReplayBuffer


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
        self.info = MuxedWriterInfo("mp4", "fake-h264", "fake-aac", output_path)

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


class ReplayStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root_dir = Path(self._temp_dir.name) / "session_001"
        recording_dir = root_dir / "recording"
        rolling_dir = root_dir / "rolling"
        clips_dir = root_dir / "clips"
        for path in (root_dir, recording_dir, rolling_dir, clips_dir):
            path.mkdir(parents=True, exist_ok=True)

        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root_dir,
            recording_dir=recording_dir,
            rolling_dir=rolling_dir,
            clips_dir=clips_dir,
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _make_frame(self, frame_id: int, timestamp: float) -> MediaFrame:
        image = np.full((24, 32, 3), frame_id % 255, dtype=np.uint8)
        return MediaFrame(
            frame_id=frame_id,
            timestamp=timestamp,
            image=image,
            source_name="Test Source",
            feed_id="feed_test",
        )

    def _make_audio_chunk(self, timestamp: float) -> AudioChunk:
        audio_format = AudioFormat(sample_rate=48_000, channels=2)
        return AudioChunk(
            timestamp=timestamp,
            data=b"\x01\x00" * audio_format.channels * 480,
            format=audio_format,
            source_name="Test Source",
            feed_id="feed_test",
        )

    def test_store_persists_frames_and_manifest(self) -> None:
        store = ReplayBuffer(buffer_duration_seconds=60, jpeg_quality=85, writer_factory=_FakeMuxedWriter)
        store.start(self.session_paths, feed_id="feed_test")

        store.append_frame(self._make_frame(1, 1.0))
        store.append_frame(self._make_frame(2, 2.0))
        store.append_frame(self._make_frame(3, 3.0))

        frame_ref = store.get_frame_ref_at_or_before(2.4)
        self.assertIsNotNone(frame_ref)
        assert frame_ref is not None
        self.assertEqual(frame_ref.frame_id, 2)
        self.assertEqual(frame_ref.sequence_index, 1)
        self.assertTrue(frame_ref.image_path.exists())
        self.assertIsNone(store.get_multifile_location_pattern())
        self.assertIsNotNone(store.get_media_segment_at_or_before(2.4))

        decoded_frame = store.get_frame_at_or_before(2.4)
        self.assertIsNotNone(decoded_frame)
        assert decoded_frame is not None
        self.assertEqual(decoded_frame.frame_id, 2)
        self.assertEqual(decoded_frame.image_bgr.shape, (24, 32, 3))

        manifest_path = self.session_paths.get_feed_paths("feed_test").rolling_dir / "rolling_manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["frame_count"], 3)
        self.assertEqual(manifest["feed_id"], "feed_test")
        self.assertEqual(manifest["frames"][-1]["frame_id"], 3)
        self.assertEqual(manifest["frames"][-1]["feed_id"], "feed_test")
        self.assertTrue(manifest["media_segments"])
        self.assertTrue(manifest["media_segments"][0]["media_path"].endswith(".mp4"))

    def test_store_persists_audio_segments_and_manifest(self) -> None:
        store = ReplayBuffer(
            buffer_duration_seconds=60,
            jpeg_quality=85,
            audio_segment_seconds=0.5,
            writer_factory=_FakeMuxedWriter,
        )
        store.start(self.session_paths, feed_id="feed_test")

        store.append_audio_chunk(self._make_audio_chunk(10.0))
        store.append_audio_chunk(self._make_audio_chunk(10.2))
        store.append_audio_chunk(self._make_audio_chunk(10.7))

        first_segment = store.get_media_segment_at_or_before(10.1)
        second_segment = store.get_media_segment_at_or_before(10.8)
        self.assertIsNotNone(first_segment)
        self.assertIsNotNone(second_segment)
        assert first_segment is not None and second_segment is not None
        self.assertTrue(first_segment.media_path.exists())
        self.assertTrue(second_segment.media_path.exists())
        self.assertNotEqual(first_segment.sequence_index, second_segment.sequence_index)

        manifest_path = self.session_paths.get_feed_paths("feed_test").rolling_dir / "rolling_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["media_segments"]), 2)
        self.assertGreater(manifest["media_segments"][0]["audio_bytes"], 0)
        self.assertTrue(manifest["media_segments"][0]["has_audio"])

    def test_store_prunes_old_frames_and_deletes_files(self) -> None:
        store = ReplayBuffer(buffer_duration_seconds=1, jpeg_quality=85, writer_factory=_FakeMuxedWriter)
        store.start(self.session_paths, feed_id="feed_test")

        store.append_frame(self._make_frame(1, 1.0))
        store.append_frame(self._make_frame(2, 1.9))
        first_frame_ref = store.get_frame_ref_at_or_before(1.1)
        self.assertIsNotNone(first_frame_ref)
        assert first_frame_ref is not None
        first_frame_path = first_frame_ref.image_path

        store.append_frame(self._make_frame(3, 2.5))

        oldest_timestamp, latest_timestamp = store.get_buffer_range()
        self.assertEqual(oldest_timestamp, 1.9)
        self.assertEqual(latest_timestamp, 2.5)
        self.assertFalse(first_frame_path.exists())


if __name__ == "__main__":
    unittest.main()
