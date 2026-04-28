"""Tests for `SegmentDecoder` (slice 4.C.tail rendering helper)."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    Segment,
)
from app.media.segment_decoder import SegmentDecoder
from app.storage.segment_replay_store import SegmentReplayLocation


def _seg(file_path: Path, *, fragment_index: int = 0) -> Segment:
    return Segment(
        session_id="s1",
        feed_id="ndi_main",
        fragment_index=fragment_index,
        file_path=str(file_path),
        codec="mjpeg",
        container="mkv",
        start_pts_ns=0,
        end_pts_ns=4_000_000_000,
        duration_ns=4_000_000_000,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=SEGMENT_STATE_COMPLETE,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at="2026-04-28T01:00:04+00:00",
    )


class _StubCapture:
    """Mimics the cv2.VideoCapture surface SegmentDecoder uses."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.last_set: tuple[int | str, float] | None = None
        self.read_calls = 0
        self._opened = True
        self._fail_on_read = False

    def set(self, prop_id, value) -> bool:
        self.last_set = (prop_id, value)
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.read_calls += 1
        if self._fail_on_read:
            return False, None
        # Return a small BGR frame whose pixel value encodes the seek offset.
        offset_ms = 0.0
        if self.last_set is not None:
            offset_ms = float(self.last_set[1])
        marker = int(offset_ms) % 255
        return True, np.full((24, 32, 3), marker, dtype=np.uint8)

    def isOpened(self) -> bool:
        return self._opened

    def release(self) -> None:
        self._opened = False


class SegmentDecoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.seg_a = self.tmp / "segment_00000.mkv"
        self.seg_b = self.tmp / "segment_00001.mkv"
        self.seg_a.write_bytes(b"\0")  # placeholder content; stub doesn't read it.
        self.seg_b.write_bytes(b"\0")
        self.captures: list[_StubCapture] = []

        def factory(path: str) -> _StubCapture:
            cap = _StubCapture(path)
            self.captures.append(cap)
            return cap

        self.decoder = SegmentDecoder(
            "ndi_main", "Camera A", capture_factory=factory
        )

    def tearDown(self) -> None:
        self.decoder.close()
        self._temp_dir.cleanup()

    def test_first_decode_opens_capture_and_returns_frame(self) -> None:
        location = SegmentReplayLocation(
            segment=_seg(self.seg_a), offset_in_segment_ns=2_000_000_000
        )
        frame = self.decoder.decode(location)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.feed_id, "ndi_main")
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.captures[0].file_path, str(self.seg_a))
        # Decoder seeks via CAP_PROP_POS_MSEC; offset_ns / 1e6 = 2000ms.
        self.assertIsNotNone(self.captures[0].last_set)
        assert self.captures[0].last_set is not None
        self.assertAlmostEqual(self.captures[0].last_set[1], 2000.0)

    def test_second_decode_same_segment_reuses_capture(self) -> None:
        loc1 = SegmentReplayLocation(
            segment=_seg(self.seg_a), offset_in_segment_ns=1_000_000_000
        )
        loc2 = SegmentReplayLocation(
            segment=_seg(self.seg_a), offset_in_segment_ns=3_000_000_000
        )
        self.decoder.decode(loc1)
        self.decoder.decode(loc2)
        self.assertEqual(len(self.captures), 1)
        # Last seek should be at 3000ms.
        self.assertAlmostEqual(self.captures[0].last_set[1], 3000.0)

    def test_crossing_segment_boundary_closes_old_and_opens_new(self) -> None:
        loc_a = SegmentReplayLocation(
            segment=_seg(self.seg_a, fragment_index=0), offset_in_segment_ns=2_000_000_000
        )
        loc_b = SegmentReplayLocation(
            segment=_seg(self.seg_b, fragment_index=1), offset_in_segment_ns=500_000_000
        )
        self.decoder.decode(loc_a)
        self.decoder.decode(loc_b)
        self.assertEqual(len(self.captures), 2)
        # First capture was released when the new file took over.
        self.assertFalse(self.captures[0].isOpened())
        self.assertTrue(self.captures[1].isOpened())
        self.assertEqual(self.captures[1].file_path, str(self.seg_b))

    def test_missing_file_returns_none_and_records_failure(self) -> None:
        ghost = self.tmp / "does_not_exist.mkv"
        location = SegmentReplayLocation(
            segment=_seg(ghost), offset_in_segment_ns=0
        )
        self.assertIsNone(self.decoder.decode(location))
        # Subsequent calls for the same path skip the open attempt.
        self.assertIsNone(self.decoder.decode(location))
        self.assertEqual(len(self.captures), 0)

    def test_close_releases_open_capture_and_allows_reopen(self) -> None:
        loc = SegmentReplayLocation(
            segment=_seg(self.seg_a), offset_in_segment_ns=0
        )
        self.decoder.decode(loc)
        self.assertEqual(len(self.captures), 1)
        self.decoder.close()
        self.assertFalse(self.captures[0].isOpened())
        # A fresh decode after close opens a new capture.
        self.decoder.decode(loc)
        self.assertEqual(len(self.captures), 2)

    def test_negative_offset_is_clamped_to_zero(self) -> None:
        loc = SegmentReplayLocation(
            segment=_seg(self.seg_a), offset_in_segment_ns=-1_000_000_000
        )
        frame = self.decoder.decode(loc)
        self.assertIsNotNone(frame)
        self.assertAlmostEqual(self.captures[0].last_set[1], 0.0)


class SegmentDecoderRealCv2Tests(unittest.TestCase):
    """Integration check against a real cv2.VideoWriter-produced MJPEG-MKV.

    Skipped if cv2 isn't available or can't write/read MJPG. This is the
    one test that exercises the actual FFmpeg backend path; the stub
    tests above cover the SegmentDecoder logic itself.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("cv2 not available")

    def setUp(self) -> None:
        import cv2

        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.seg_path = self.tmp / "segment_00000.mkv"
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        writer = cv2.VideoWriter(str(self.seg_path), fourcc, 15.0, (32, 24))
        if not writer.isOpened():
            self._temp_dir.cleanup()
            raise unittest.SkipTest("cv2 cannot write MJPG; backend unavailable")
        # 30 frames at 15fps = 2 seconds. Pixel value carries the frame index.
        for i in range(30):
            frame = np.full((24, 32, 3), i * 8 % 255, dtype=np.uint8)
            writer.write(frame)
        writer.release()

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_real_decode_returns_bgr_frame(self) -> None:
        decoder = SegmentDecoder("ndi_main", "Camera A")
        try:
            location = SegmentReplayLocation(
                segment=_seg(self.seg_path), offset_in_segment_ns=500_000_000  # 0.5s
            )
            frame = decoder.decode(location)
            self.assertIsNotNone(frame)
            assert frame is not None
            self.assertEqual(frame.image.shape, (24, 32, 3))
            self.assertEqual(frame.image.dtype, np.uint8)
        finally:
            decoder.close()


if __name__ == "__main__":
    unittest.main()
