"""Recorder long + short segment file behavior."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app.config.settings import AppSettings
from app.core.models import MediaFrame, SessionPaths
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
            rec.begin_long_recording(session, "Cam", 15.0, feed_id="feed_main")
            self.assertTrue(rec.is_recording())
            f = _frame()
            rec.write_frame(f)
            rec.advance_short_segment()
            rec.write_frame(f)
            rec.write_frame(f)
            rec.advance_short_segment()
            rec.write_frame(f)
            rec.end_long_recording()
            self.assertFalse(rec.is_recording())

            feed_rec = session.get_feed_paths("feed_main").recording_dir
            long_files = list(feed_rec.glob("game.*"))
            self.assertTrue(long_files, "long file should exist")
            seg_dir = feed_rec / "segments"
            segs = sorted(seg_dir.glob("rally_*.mp4")) + sorted(seg_dir.glob("rally_*.avi"))
            self.assertGreaterEqual(len(segs), 2)
            manifest_path = feed_rec / settings.recording_manifest_filename
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(data["feed_id"], "feed_main")
            self.assertEqual(data["source_name"], "Cam")
            self.assertIn("long_output_path", data)
            self.assertGreaterEqual(data["long_frame_count"], 4)
            self.assertEqual(len(data["segments"]), 2)

    def test_second_long_session_uses_incremented_name(self) -> None:
        with TemporaryDirectory() as tmp:
            session = _session_with_feed(Path(tmp))
            settings = AppSettings()
            settings.recording_filename = "game.mp4"
            rec = Recorder(settings)
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
