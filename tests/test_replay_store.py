"""Focused tests for the disk-backed replay store."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app.core.models import MediaFrame, SessionPaths
from app.media.replay_buffer import ReplayBuffer


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
        )

    def test_store_persists_frames_and_manifest(self) -> None:
        store = ReplayBuffer(buffer_duration_seconds=60, jpeg_quality=85)
        store.start(self.session_paths)

        store.append_frame(self._make_frame(1, 1.0))
        store.append_frame(self._make_frame(2, 2.0))
        store.append_frame(self._make_frame(3, 3.0))

        frame_ref = store.get_frame_ref_at_or_before(2.4)
        self.assertIsNotNone(frame_ref)
        assert frame_ref is not None
        self.assertEqual(frame_ref.frame_id, 2)
        self.assertEqual(frame_ref.sequence_index, 1)
        self.assertTrue(frame_ref.image_path.exists())
        self.assertIsNotNone(store.get_multifile_location_pattern())

        decoded_frame = store.get_frame_at_or_before(2.4)
        self.assertIsNotNone(decoded_frame)
        assert decoded_frame is not None
        self.assertEqual(decoded_frame.frame_id, 2)
        self.assertEqual(decoded_frame.image_bgr.shape, (24, 32, 3))

        manifest_path = self.session_paths.rolling_dir / "rolling_manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["frame_count"], 3)
        self.assertEqual(manifest["frames"][-1]["frame_id"], 3)

    def test_store_prunes_old_frames_and_deletes_files(self) -> None:
        store = ReplayBuffer(buffer_duration_seconds=1, jpeg_quality=85)
        store.start(self.session_paths)

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
