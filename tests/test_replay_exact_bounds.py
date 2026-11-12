"""Replay over exact-bounds segments (docs/REPLAY_SEAM_GLITCH.md §7.4 step 2).

Segments written from splitmuxsink's reports (`Segment.end_exclusive`)
cover `[start, end)`, meet exactly, and carry their real frame count. The
store resolves a session time to the *nearest* frame and hands the decoder
a frame index, so stepping one frame period at a time shows every frame
exactly once, across seams too. Older rows keep resolving the old way.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    FeedDefinition,
    MediaFrame,
    Segment,
    SessionPaths,
)
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.media.feed_runtime import FeedRuntime
from app.media.output_renderer import OutputRenderer
from app.media.recording_manager import RecordingManager
from app.media.segment_decoder import SegmentDecoder
from app.media.source_interface import SourceInterface
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import (
    RecordingSegmentReplayStore,
    SegmentReplayLocation,
)

PERIOD_NS = 100_000_000_000 / 2997  # 29.97 fps, as videorate spaces frames
FRAMES = 119
RECORDING = RecordingState.RECORDING


def _exact(feed_id: str, index: int, *, first_frame: int, phase_ns: int = 0) -> Segment:
    start = phase_ns + round(first_frame * PERIOD_NS)
    end = phase_ns + round((first_frame + FRAMES) * PERIOD_NS)
    return Segment(
        session_id="s1",
        feed_id=feed_id,
        fragment_index=index,
        file_path=f"/tmp/{feed_id}/segment_{index:05d}.mkv",
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start,
        end_pts_ns=end,
        duration_ns=end - start,
        frame_count_estimate=FRAMES,
        size_bytes=1,
        state=SEGMENT_STATE_COMPLETE,
        created_at="2026-11-11T00:00:00+00:00",
        finalized_at="2026-11-11T00:00:04+00:00",
        start_session_time_ns=start + 1_000_000_000,
        end_session_time_ns=end + 1_000_000_000,
        pts_to_session_offset_ns=1_000_000_000,
        end_exclusive=True,
    )


def _store(*segments: Segment) -> RecordingSegmentReplayStore:
    index = SegmentIndex()
    for segment in segments:
        index.add(segment)
    return RecordingSegmentReplayStore(index)


def _frame_shown(store, feed_id: str, session_time_ns: int) -> tuple[int, int]:
    loc = store.nearest_frame_location(
        feed_id=feed_id, session_time_ns=session_time_ns, recording_state=RECORDING
    )
    assert loc is not None and loc.frame_index is not None
    return loc.segment.fragment_index, loc.frame_index


class ExactBoundsStepTests(unittest.TestCase):
    def test_stepping_across_seams_shows_every_frame_once(self) -> None:
        segments = [_exact("a", i, first_frame=i * FRAMES) for i in range(4)]
        store = _store(*segments)
        # Step from an arbitrary phase within the first frame, by the true
        # frame period (the controller's snapping keeps it on that grid).
        # Phases stay a few ns clear of the exact half-way point between
        # two frames, where neither is nearer.
        for phase in (0, 1, int(PERIOD_NS * 0.49), int(PERIOD_NS * 0.51), int(PERIOD_NS) - 3):
            with self.subTest(phase=phase):
                t0 = segments[0].start_session_time_ns + phase - int(PERIOD_NS / 2) + 1
                shown = [
                    _frame_shown(store, "a", t0 + round(n * PERIOD_NS))
                    for n in range(4 * FRAMES - 2)
                ]
                flat = [seg * FRAMES + k for seg, k in shown]
                self.assertEqual(flat, list(range(flat[0], flat[0] + len(flat))))

    def test_two_feeds_with_different_phase_both_advance_one_frame_per_step(self) -> None:
        a = [_exact("a", i, first_frame=i * FRAMES) for i in range(3)]
        b = [_exact("b", i, first_frame=i * FRAMES, phase_ns=int(PERIOD_NS * 0.37)) for i in range(3)]
        store = _store(*a, *b)
        t = a[0].start_session_time_ns + round(PERIOD_NS * 2)
        last = {"a": None, "b": None}
        for _ in range(3 * FRAMES - 6):
            for feed in ("a", "b"):
                seg, k = _frame_shown(store, feed, t)
                flat = seg * FRAMES + k
                if last[feed] is not None:
                    self.assertEqual(flat, last[feed] + 1, f"feed {feed} at t={t}")
                last[feed] = flat
            t += round(PERIOD_NS)

    def test_last_half_frame_of_a_segment_shows_the_next_first_frame(self) -> None:
        s0, s1 = _exact("a", 0, first_frame=0), _exact("a", 1, first_frame=FRAMES)
        store = _store(s0, s1)
        self.assertEqual(_frame_shown(store, "a", s1.start_session_time_ns - 1_000), (1, 0))
        self.assertEqual(
            _frame_shown(store, "a", s1.start_session_time_ns - int(PERIOD_NS * 0.6)),
            (0, FRAMES - 1),
        )

    def test_live_edge_is_the_last_frame_and_not_a_freeze(self) -> None:
        s0, s1 = _exact("a", 0, first_frame=0), _exact("a", 1, first_frame=FRAMES)
        store = _store(s0, s1)
        _, latest = store.available_session_time_range()
        self.assertEqual(latest, s1.end_session_time_ns - round(PERIOD_NS))
        self.assertEqual(store.latest_replayable_session_time("a"), latest)
        loc = store.nearest_frame_location(
            feed_id="a", session_time_ns=latest, recording_state=RECORDING
        )
        assert loc is not None
        self.assertFalse(loc.is_freeze)
        self.assertEqual((loc.segment.fragment_index, loc.frame_index), (1, FRAMES - 1))

    def test_past_the_end_freezes_on_the_last_frame_not_beyond_it(self) -> None:
        s0 = _exact("a", 0, first_frame=0)
        store = _store(s0)
        loc = store.nearest_frame_location(
            feed_id="a",
            session_time_ns=s0.end_session_time_ns + 5_000_000_000,
            recording_state=RECORDING,
        )
        assert loc is not None
        self.assertTrue(loc.is_freeze)
        self.assertEqual(loc.frame_index, FRAMES - 1)
        self.assertEqual(loc.offset_in_segment_ns, round((FRAMES - 1) * PERIOD_NS))

    def test_before_the_start_freezes_on_the_first_frame(self) -> None:
        s0 = _exact("a", 0, first_frame=0)
        store = _store(s0)
        loc = store.nearest_frame_location(
            feed_id="a",
            session_time_ns=s0.start_session_time_ns - 1_000_000_000,
            recording_state=RECORDING,
        )
        assert loc is not None
        self.assertTrue(loc.is_freeze)
        self.assertEqual(loc.frame_index, 0)

    def test_segments_are_ordered_by_session_time_not_pts(self) -> None:
        # A mid-game rebuild restarts PTS at 0; session time keeps going.
        early = _exact("a", 0, first_frame=500)
        late = _exact("a", 1, first_frame=0)
        late = Segment(**{**{f: getattr(late, f) for f in Segment.__slots__},
                          "start_session_time_ns": early.end_session_time_ns + 2_000_000_000,
                          "end_session_time_ns": early.end_session_time_ns + 2_000_000_000 + late.duration_ns})
        store = _store(early, late)
        loc = store.nearest_frame_location(
            feed_id="a",
            session_time_ns=early.start_session_time_ns - 1_000_000_000,
            recording_state=RECORDING,
        )
        assert loc is not None
        self.assertEqual(loc.segment.fragment_index, 0)

    def test_strict_resolver_gives_an_exclusive_end_to_the_next_segment(self) -> None:
        s0, s1 = _exact("a", 0, first_frame=0), _exact("a", 1, first_frame=FRAMES)
        store = _store(s0, s1)
        loc = store.resolve_session_time(
            feed_id="a",
            target_session_time_ns=s0.end_session_time_ns,
            recording_state=RECORDING,
        )
        assert loc is not None
        self.assertEqual(loc.segment.fragment_index, 1)
        self.assertEqual(loc.offset_in_segment_ns, 0)


class _Source(SourceInterface):
    def __init__(self, feed_id: str) -> None:
        self._feed_id = feed_id

    def connect_source(self) -> bool:
        return True

    def disconnect_source(self) -> None:
        return

    def is_connected(self) -> bool:
        return True

    def get_display_name(self) -> str:
        return self._feed_id

    def get_feed_id(self) -> str:
        return self._feed_id

    def create_pipeline_fragment(self) -> str:
        return "fake"

    def read_frame(self) -> MediaFrame | None:
        return None

    def get_frame_size(self) -> tuple[int, int]:
        return 32, 24

    def get_nominal_fps(self) -> float:
        return 29.97


class _Pipeline:
    def set_frame_callback(self, callback) -> None:
        return

    def set_live_sample_callback(self, callback) -> None:
        return

    def connect_source(self) -> bool:
        return True

    def start_preview(self) -> None:
        return

    def stop_all(self) -> None:
        return

    def is_source_connected(self) -> bool:
        return True

    def get_source_name(self) -> str:
        return "fake"

    def get_source_status_message(self) -> str | None:
        return None

    def get_ingest_telemetry(self) -> None:
        return None


class _FrameRecordingDecoder(SegmentDecoder):
    """Records which (segment, frame) each decode asked for."""

    def __init__(self, feed_id: str) -> None:
        super().__init__(feed_id, feed_id)
        self.shown: list[int] = []

    def decode(self, location):  # type: ignore[override]
        self.shown.append(location.segment.fragment_index * FRAMES + location.frame_index)
        return MediaFrame(
            frame_id=0,
            timestamp=0.0,
            image=np.zeros((24, 32, 3), dtype=np.uint8),
            source_name=self._feed_id,
            feed_id=self._feed_id,
        )

    def close(self) -> None:  # type: ignore[override]
        return


class StepFramesOverExactBoundsTests(unittest.TestCase):
    """`step_frames` with the whole-nanosecond period the app derives from
    `target_fps` (33,366,700 ns for 29.97): without snapping, ~30 steps of
    0.03 ns drift tip a frame boundary and one frame shows twice."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtCore import QCoreApplication

        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name) / "session_001"
        (root / "recording").mkdir(parents=True)
        paths = SessionPaths(session_id="session_001", root_dir=root, recording_dir=root / "recording")
        index = SegmentIndex()
        for i in range(4):
            index.add(_exact("a", i, first_frame=i * FRAMES))
            index.add(_exact("b", i, first_frame=i * FRAMES, phase_ns=int(PERIOD_NS * 0.62)))
        self.decoders = {f: _FrameRecordingDecoder(f) for f in ("a", "b")}
        runtimes = []
        for feed_id in ("a", "b"):
            runtime = FeedRuntime(
                feed=FeedDefinition(feed_id=feed_id, display_name=feed_id),
                source=_Source(feed_id),
                pipeline_manager=_Pipeline(),
            )
            runtime.start(paths)
            runtimes.append(runtime)
        self.recording_manager = RecordingManager()
        self.controller = PlaybackController(
            feed_runtimes=runtimes,
            output_renderer=OutputRenderer(),
            recording_manager=self.recording_manager,
            replay_store=RecordingSegmentReplayStore(index),
            default_source_name="a",
            session_role="referee",
            live_only=False,
            decoder_factory=lambda feed_id, *_: self.decoders[feed_id],
            frame_period_ns=round(PERIOD_NS),
            rewind_seconds=10,
        )
        self.controller.initialize("session_001")
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()
        self.start = index.all_for_feed("a")[0].start_session_time_ns

    def tearDown(self) -> None:
        self.controller.shutdown()
        self._tmp.cleanup()

    def _pause_at(self, session_time_ns: int) -> None:
        self.controller.rewind_configured_seconds()
        with self.controller._lock:
            self.controller._playback_session_time_ns = session_time_ns
        self.controller.pause_playback()

    def test_every_step_shows_the_next_frame_on_every_feed_across_seams(self) -> None:
        # Start 1 ns after a frame boundary: the worst case for drift.
        self._pause_at(self.start + int(PERIOD_NS / 2) + 1)
        for decoder in self.decoders.values():
            decoder.shown.clear()
        for _ in range(3 * FRAMES):
            self.controller.step_frames(1)
        for feed_id, decoder in self.decoders.items():
            with self.subTest(feed=feed_id):
                shown = decoder.shown
                self.assertEqual(len(shown), 3 * FRAMES)
                self.assertEqual(
                    [b - a for a, b in zip(shown, shown[1:])], [1] * (len(shown) - 1)
                )

    def test_stepping_back_from_live_lands_on_recorded_frames(self) -> None:
        for decoder in self.decoders.values():
            decoder.shown.clear()
        for _ in range(FRAMES + 5):
            self.controller.step_frames(-1)
        # The live edge is feed b's last frame (b runs 0.62 frame behind a).
        # One step back is b's second-to-last frame; a's nearest is its last.
        expected_first = {"a": 4 * FRAMES - 1, "b": 4 * FRAMES - 2}
        for feed_id, decoder in self.decoders.items():
            with self.subTest(feed=feed_id):
                shown = decoder.shown
                self.assertEqual(shown[0], expected_first[feed_id])
                self.assertEqual(
                    [a - b for a, b in zip(shown, shown[1:])], [1] * (len(shown) - 1)
                )


class _IndexedCapture:
    """cv2-like capture: pixel value == frame index; `frames` frames long."""

    def __init__(self, frames: int, fps: float = 29.97) -> None:
        self.frames = frames
        self.fps = fps
        self.pos = 0
        self.seeks: list[int] = []

    def get(self, prop):
        import cv2

        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self.pos)
        return 0.0

    def set(self, _prop, offset_ms) -> bool:
        self.pos = int(offset_ms * self.fps / 1000.0 + 0.5)
        self.seeks.append(self.pos)
        return True

    def grab(self) -> bool:
        if self.pos >= self.frames:
            return False
        self.pos += 1
        return True

    def read(self):
        if self.pos >= self.frames:
            return False, None
        index = self.pos
        self.pos += 1
        return True, np.full((2, 2, 3), index, dtype=np.uint8)

    def isOpened(self) -> bool:
        return True

    def release(self) -> None:
        pass


class DecoderFrameIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("cv2 not available")

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "segment_00000.mkv"
        self.path.write_bytes(b"\0")
        self.capture = _IndexedCapture(frames=118)
        self.decoder = SegmentDecoder("a", "A", capture_factory=lambda _p: self.capture)
        segment = _exact("a", 0, first_frame=0)
        self.segment = Segment(
            **{**{f: getattr(segment, f) for f in Segment.__slots__}, "file_path": str(self.path)}
        )

    def tearDown(self) -> None:
        self.decoder.close()
        self._tmp.cleanup()

    def _shown(self, frame_index: int, offset_ns: int = 0) -> int:
        frame = self.decoder.decode(
            SegmentReplayLocation(
                segment=self.segment, offset_in_segment_ns=offset_ns, frame_index=frame_index
            )
        )
        assert frame is not None
        return int(frame.image[0, 0, 0])

    def test_frame_index_wins_over_the_offset(self) -> None:
        self.assertEqual(self._shown(40, offset_ns=0), 40)

    def test_stepping_off_the_end_returns_the_real_last_frame(self) -> None:
        # The file is one frame shorter than the row says (118 vs 119).
        self.assertEqual(self._shown(117), 117)
        self.assertEqual(self._shown(118), 117)
        # Learned: no seek is spent on the missing frame again.
        seeks = len(self.capture.seeks)
        self.assertEqual(self._shown(118), 117)
        self.assertEqual(len(self.capture.seeks), seeks)

    def test_a_seek_past_the_end_backs_up_to_the_last_frame(self) -> None:
        self.assertEqual(self._shown(119), 117)


class _OpenCvLikeCapture:
    """Behaves like OpenCV's FFmpeg capture on an mkv whose frames are not
    evenly spaced: a seek finds frame `ms * fps / 1000` by *declared* fps
    and lands `overshoot` frames late; `CAP_PROP_POS_MSEC` is the frame just
    read, except after the capture's first `read()`; with `seekable=False`
    (a file cut short by a crash) a seek does nothing."""

    def __init__(self, stamps: list[float], *, overshoot: int = 2, seekable: bool = True) -> None:
        self.stamps = stamps
        self.overshoot = overshoot
        self.seekable = seekable
        self.pos = 0
        self.last = None
        self.reads = 0

    def get(self, prop):
        import cv2

        if prop == cv2.CAP_PROP_FPS:
            return 29.97
        if prop == cv2.CAP_PROP_POS_MSEC:
            if self.reads == 1 or self.last is None:
                return -42_549.1
            return self.stamps[self.last]
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(round(self.stamps[self.last] * 29.97 / 1000) + 1) if self.last is not None else 0.0
        return 0.0

    def set(self, _prop, offset_ms) -> bool:
        if self.seekable:
            by_rate = round(offset_ms * 29.97 / 1000) * 1000 / 29.97
            first = next((i for i, t in enumerate(self.stamps) if t >= by_rate - 0.5), len(self.stamps) - 1)
            self.pos = min(len(self.stamps) - 1, first + self.overshoot)
        return True

    def grab(self) -> bool:
        if self.pos >= len(self.stamps):
            return False
        self.last = self.pos
        self.pos += 1
        return True

    def read(self):
        if not self.grab():
            return False, None
        self.reads += 1
        return True, np.full((2, 2, 3), self.last, dtype=np.uint8)

    def isOpened(self) -> bool:
        return True

    def release(self) -> None:
        pass


class DecoderTimestampPositioningTests(unittest.TestCase):
    """With the file's own timestamps, every request returns the right frame."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("cv2 not available")

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "segment_00000.mkv"
        self.path.write_bytes(b"\0")
        # ~26.5 fps with jitter, like the synthetic source.
        self.stamps = [round(k * 37.7 + (5.0 if k % 7 == 3 else 0.0), 1) for k in range(105)]
        segment = _exact("a", 0, first_frame=0)
        self.segment = Segment(
            **{**{f: getattr(segment, f) for f in Segment.__slots__}, "file_path": str(self.path)}
        )
        self.opened: list[_OpenCvLikeCapture] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _decoder(self, **capture_kwargs) -> SegmentDecoder:
        def factory(_path):
            capture = _OpenCvLikeCapture(self.stamps, **capture_kwargs)
            self.opened.append(capture)
            return capture

        return SegmentDecoder(
            "a", "A", capture_factory=factory, timestamp_reader=lambda _p: list(self.stamps)
        )

    def _shown(self, decoder: SegmentDecoder, frame_index: int | None, offset_ms: float = 0.0) -> int:
        frame = decoder.decode(
            SegmentReplayLocation(
                segment=self.segment,
                offset_in_segment_ns=int(offset_ms * 1_000_000),
                frame_index=frame_index,
            )
        )
        assert frame is not None
        return int(frame.image[0, 0, 0])

    def test_backward_and_random_access_land_on_the_requested_frame(self) -> None:
        decoder = self._decoder()
        order = list(range(104, -1, -1)) + [50, 3, 99, 12, 60, 61, 62, 7]
        self.assertEqual([self._shown(decoder, k) for k in order], order)

    def test_the_first_request_after_opening_is_right_too(self) -> None:
        for k in (17, 51, 104):
            with self.subTest(frame=k):
                self.assertEqual(self._shown(self._decoder(), k), k)

    def test_an_offset_without_a_frame_index_picks_the_nearest_timestamp(self) -> None:
        decoder = self._decoder()
        self.assertEqual(self._shown(decoder, None, offset_ms=self.stamps[40] + 10.0), 40)
        self.assertEqual(self._shown(decoder, None, offset_ms=self.stamps[40] + 25.0), 41)

    def test_a_file_whose_seeks_do_nothing_is_read_from_the_start(self) -> None:
        decoder = self._decoder(seekable=False)
        self.assertEqual(self._shown(decoder, 90), 90)
        self.assertEqual(self._shown(decoder, 30), 30)
        self.assertGreaterEqual(len(self.opened), 2)

    def test_requests_past_the_end_return_the_last_frame(self) -> None:
        self.assertEqual(self._shown(self._decoder(), 500), 104)


class EndExclusiveColumnTests(unittest.TestCase):
    def test_column_is_added_to_an_existing_table_and_defaults_to_old_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.db"
            db = MetadataDb(db_path)
            db.create_session("s1", "T", "2026-11-11T00:00:00+00:00")
            db.close()
            # Simulate a DB from before the column existed.
            raw = sqlite3.connect(db_path)
            raw.execute("ALTER TABLE segments DROP COLUMN end_exclusive")
            raw.execute(
                "INSERT INTO segments (session_id, feed_id, fragment_index, file_path, codec, "
                "container, start_pts_ns, end_pts_ns, duration_ns, frame_count_estimate, "
                "size_bytes, state, created_at) VALUES "
                "('s1','a',0,'/old.mkv','mjpeg','mkv',0,1,1,1,1,'complete','x')"
            )
            raw.commit()
            raw.close()

            db = MetadataDb(db_path)
            db.insert_segment(_exact("a", 1, first_frame=FRAMES))
            rows = {Path(s.file_path).name: s for s in db.segments_for_session("s1")}
            db.close()
        self.assertFalse(rows["old.mkv"].end_exclusive)
        self.assertTrue(rows["segment_00001.mkv"].end_exclusive)


if __name__ == "__main__":
    unittest.main()
