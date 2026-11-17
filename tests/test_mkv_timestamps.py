"""`read_video_timestamps_ms`: frame timestamps from mkv block headers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.media.mkv_timestamps import read_video_frame_index, read_video_timestamps_ms
from tests.mkv_builder import build_mkv


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


class MkvFrameIndexTests(unittest.TestCase):
    """`read_video_frame_index`: where each video frame's bytes are in the file."""

    # JPEG-shaped placeholders: start marker, a distinct body, end marker.
    FRAMES = [bytes([0xFF, 0xD8, 0xFF]) + bytes([i]) * (10 + i) + bytes([0xFF, 0xD9]) for i in range(6)]

    def _index(self, data: bytes):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "segment_00000.mkv"
            path.write_bytes(data)
            return read_video_frame_index(path)

    def test_each_offset_and_size_points_at_that_frames_bytes(self) -> None:
        data = build_mkv(self.FRAMES)
        index = self._index(data)
        assert index is not None
        self.assertEqual(len(index.offsets), len(self.FRAMES))
        self.assertFalse(index.laced)
        for frame, offset, size in zip(self.FRAMES, index.offsets, index.sizes):
            self.assertEqual(data[offset:offset + size], frame)

    def test_timestamps_match_the_timestamp_reader(self) -> None:
        data = build_mkv(self.FRAMES, timestamps_ms=[0, 33, 67, 100, 133, 167])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "segment_00000.mkv"
            path.write_bytes(data)
            index = read_video_frame_index(path)
            stamps = read_video_timestamps_ms(path)
        assert index is not None
        self.assertEqual(index.timestamps_ms, stamps)
        self.assertEqual(stamps, [0.0, 33.0, 67.0, 100.0, 133.0, 167.0])

    def test_audio_blocks_are_not_indexed(self) -> None:
        data = build_mkv(self.FRAMES, audio_frames=4)
        index = self._index(data)
        assert index is not None
        self.assertEqual(len(index.offsets), len(self.FRAMES))
        for frame, offset, size in zip(self.FRAMES, index.offsets, index.sizes):
            self.assertEqual(data[offset:offset + size], frame)

    def test_block_groups_are_indexed_like_simple_blocks(self) -> None:
        data = build_mkv(self.FRAMES, block_groups=True)
        index = self._index(data)
        assert index is not None
        for frame, offset, size in zip(self.FRAMES, index.offsets, index.sizes):
            self.assertEqual(data[offset:offset + size], frame)

    def test_laced_blocks_are_flagged_so_they_are_not_read_as_single_frames(self) -> None:
        index = self._index(build_mkv(self.FRAMES, lacing_flag=0x02))
        assert index is not None
        self.assertTrue(index.laced)

    def test_a_file_cut_short_still_indexes_what_it_has(self) -> None:
        data = build_mkv(self.FRAMES)
        index = self._index(data[: len(data) - 8])  # the last frame's bytes are cut
        assert index is not None
        self.assertEqual(len(index.offsets), len(self.FRAMES))
        last_end = index.offsets[-1] + index.sizes[-1]
        self.assertGreater(last_end, len(data) - 8)  # so a reader must check the bounds

    def test_non_matroska_and_missing_files_give_none(self) -> None:
        self.assertIsNone(self._index(bytes(64)))
        self.assertIsNone(self._index(b""))
        self.assertIsNone(read_video_frame_index(Path("does_not_exist.mkv")))

    @unittest.skipIf(_GST is None, "GStreamer (gi) not available")
    def test_matroskamux_jpeg_file_frames_are_whole_jpegs(self) -> None:
        Gst = _GST
        for name in ("videotestsrc", "jpegenc", "matroskamux"):
            if Gst.ElementFactory.find(name) is None:
                self.skipTest(f"{name} not available")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "segment_00000.mkv"
            pipeline = Gst.parse_launch(
                f"matroskamux name=mux ! filesink location=\"{path.as_posix()}\" "
                "videotestsrc num-buffers=12 ! video/x-raw,width=64,height=36,framerate=30/1 "
                "! jpegenc ! mux."
            )
            pipeline.set_state(Gst.State.PLAYING)
            pipeline.get_bus().timed_pop_filtered(
                10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
            )
            pipeline.set_state(Gst.State.NULL)
            index = read_video_frame_index(path)
            data = path.read_bytes()
        assert index is not None
        self.assertEqual(len(index.offsets), 12)
        self.assertFalse(index.laced)
        for offset, size in zip(index.offsets, index.sizes):
            self.assertEqual(data[offset:offset + 3], bytes([0xFF, 0xD8, 0xFF]))  # JPEG start
            self.assertEqual(data[offset + size - 2:offset + size], bytes([0xFF, 0xD9]))  # JPEG end


if __name__ == "__main__":
    unittest.main()
