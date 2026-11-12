"""`read_video_timestamps_ms`: frame timestamps from mkv block headers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.media.mkv_timestamps import read_video_timestamps_ms


def _load_gst():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return None
    Gst.init(None)
    return Gst


_GST = _load_gst()


class MkvTimestampTests(unittest.TestCase):
    def test_non_matroska_files_give_none(self) -> None:
        with TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.mkv"
            junk.write_bytes(b"\0" * 64)
            empty = Path(tmp) / "empty.mkv"
            empty.write_bytes(b"")
            self.assertIsNone(read_video_timestamps_ms(junk))
            self.assertIsNone(read_video_timestamps_ms(empty))
            self.assertIsNone(read_video_timestamps_ms(Path(tmp) / "missing.mkv"))

    @unittest.skipIf(_GST is None, "GStreamer (gi) not available")
    def test_matroskamux_file_with_audio_and_100us_timestamps(self) -> None:
        Gst = _GST
        for name in ("videotestsrc", "jpegenc", "matroskamux", "audiotestsrc", "opusenc"):
            if Gst.ElementFactory.find(name) is None:
                self.skipTest(f"{name} not available")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "segment_00000.mkv"
            pipeline = Gst.parse_launch(
                "matroskamux name=mux timecodescale=100000 ! "
                f"filesink location=\"{path.as_posix()}\" "
                "videotestsrc num-buffers=25 ! video/x-raw,width=64,height=36,framerate=30000/1001 "
                "! jpegenc ! mux. "
                "audiotestsrc num-buffers=40 ! audioconvert ! opusenc ! mux."
            )
            pipeline.set_state(Gst.State.PLAYING)
            pipeline.get_bus().timed_pop_filtered(
                10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
            )
            pipeline.set_state(Gst.State.NULL)
            stamps = read_video_timestamps_ms(path)
        assert stamps is not None
        self.assertEqual(len(stamps), 25)  # video frames only, not audio
        self.assertEqual(stamps[0], 0.0)
        for k, value in enumerate(stamps):
            self.assertAlmostEqual(value, k * 1001 / 30, delta=0.06)


if __name__ == "__main__":
    unittest.main()
