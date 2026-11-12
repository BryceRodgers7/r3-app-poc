"""A segment is indexed only once splitmuxsink has closed its file, with exact bounds.

`PipelineManager` takes each segment's start from the first buffer that
`format-location-full` hands over, ends it where the next one starts, and
writes it to the DB / index / validator when `splitmuxsink-fragment-closed`
arrives for its file (or when splitmuxsink goes to NULL).
docs/REPLAY_SEAM_GLITCH.md §7.4 step 1. These tests pin that hand-off, the
bus routing and, with real GStreamer, the contract it relies on: consecutive
segments meet exactly and each row's frame count matches its file.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest

from app.core.models import SessionPaths
from app.core.session_clock import SessionClock
from app.media.pipeline_manager import PipelineManager
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex
from app.storage.session_recovery import _default_segment_validator

FRAME_NS = 33_333_333


def _load_gst() -> object | None:
    """Return an initialized `Gst` module, or None when PyGObject is missing."""
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return None
    Gst.init(None)
    return Gst


_GST = _load_gst()
_HAS_RECORD_ELEMENTS = _GST is not None and all(
    _GST.ElementFactory.find(name) is not None
    for name in ("videotestsrc", "jpegenc", "splitmuxsink", "matroskamux")
)
# Just what `_sample_running_time` reads when the sample carries no segment.
_FAKE_GST = SimpleNamespace(CLOCK_TIME_NONE=2**64 - 1, Format=SimpleNamespace(TIME=3))


class _RecordingWorker:
    """Stands in for `SegmentValidatorWorker`; optionally validates on submit."""

    def __init__(self, validator=None) -> None:
        self.submitted: list[str] = []
        self.results: list[tuple[str, bool]] = []
        self._validator = validator
        self._lock = threading.Lock()

    def submit(self, segment, *, quarantine_dir, metadata_db, segment_index) -> None:
        with self._lock:
            self.submitted.append(segment.file_path)
        if self._validator is not None:
            ok = self._validator(Path(segment.file_path)).is_valid
            with self._lock:
                self.results.append((Path(segment.file_path).name, ok))


class _FakeClock:
    """A pipeline clock running 7 ms behind `time.monotonic_ns()`."""

    def get_time(self) -> int:
        return time.monotonic_ns() - 7_000_000


def _sample(pts_ns: int) -> SimpleNamespace:
    return SimpleNamespace(
        get_buffer=lambda: SimpleNamespace(pts=pts_ns), get_segment=lambda: None
    )


def _session_paths(root: Path) -> SessionPaths:
    recording_dir = root / "recording"
    recording_dir.mkdir(parents=True, exist_ok=True)
    return SessionPaths(session_id="session_001", root_dir=root, recording_dir=recording_dir)


def _stub_pm(session_paths: SessionPaths, worker: _RecordingWorker) -> PipelineManager:
    pm = PipelineManager.__new__(PipelineManager)
    pm._recording_session_paths = session_paths
    pm._recording_feed_id = "ndi_1"
    pm._recording_codec = "mjpeg"
    pm._recording_container = "mkv"
    pm._recording_segment_counter = 0
    pm._recording_game_subdir = None
    pm._recording_segment_duration_seconds = 0.4
    pm._metadata_db = None
    pm._segment_index = SegmentIndex()
    pm._recording_running = True
    pm._feed_metrics = None
    pm._session_clock = None
    pm._segment_validator_worker = worker
    pm._source = SimpleNamespace(get_feed_id=lambda: "ndi_1")
    pm._pipeline = None
    pm._pipeline_generation = 1
    pm._frame_duration_ns = FRAME_NS
    pm._Gst = _FAKE_GST
    return pm


def _open(pm: PipelineManager, frame: int) -> str:
    """Run format-location-full for a segment whose first buffer is `frame`."""
    path = PipelineManager._on_splitmuxsink_format_location(
        pm, None, frame, _sample(frame * FRAME_NS)
    )
    Path(path).write_bytes(b"segment")
    return path


def _feed(pm: PipelineManager, first: int, last: int) -> None:
    keeper = pm._segment_bookkeeper_or_new()
    for k in range(first, last + 1):
        keeper.note_buffer(k * FRAME_NS)


class SegmentCloseGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.worker = _RecordingWorker()
        self.pm = _stub_pm(_session_paths(Path(self._tmp.name)), self.worker)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_segment_is_indexed_and_validated_only_after_its_close(self) -> None:
        path0 = _open(self.pm, 0)
        _feed(self.pm, 0, 121)  # the probe runs ahead of the cut
        _open(self.pm, 120)
        self.assertEqual(self.worker.submitted, [])
        self.assertEqual(self.pm._segment_index.all_for_feed("ndi_1"), [])

        self.pm._on_segment_file_closed(path0, 120 * FRAME_NS)
        self.assertEqual(self.worker.submitted, [path0])
        (segment,) = self.pm._segment_index.all_for_feed("ndi_1")
        self.assertTrue(segment.end_exclusive)
        self.assertEqual(segment.start_pts_ns, 0)
        self.assertEqual(segment.end_pts_ns, 120 * FRAME_NS)
        self.assertEqual(segment.duration_ns, 120 * FRAME_NS)
        self.assertEqual(segment.frame_count_estimate, 120)

    def test_format_location_does_no_db_or_index_work(self) -> None:
        _open(self.pm, 0)
        _feed(self.pm, 0, 10)
        _open(self.pm, 8)
        _open(self.pm, 16)
        self.assertEqual(self.pm._segment_index.all_for_feed("ndi_1"), [])
        self.assertEqual(self.worker.submitted, [])

    def test_session_times_use_one_pipeline_constant(self) -> None:
        clock_ns = 50_000_000_000
        self.pm._session_clock = SessionClock(clock_ns=lambda: clock_ns)
        base_time = 40_000_000_000
        self.pm._pipeline = SimpleNamespace(
            get_clock=lambda: _FakeClock(), get_base_time=lambda: base_time
        )
        paths = [_open(self.pm, 0)]
        _feed(self.pm, 0, 250)
        paths.append(_open(self.pm, 120))
        paths.append(_open(self.pm, 240))
        for path in paths[:2]:
            self.pm._on_segment_file_closed(path)
        first, second = self.pm._segment_index.all_for_feed("ndi_1")
        # running time + base time + clock offset (7 ms) - session start
        expected_start = 0 + base_time + 7_000_000 - clock_ns
        self.assertAlmostEqual(first.start_session_time_ns, expected_start, delta=200_000)
        self.assertEqual(first.end_session_time_ns, second.start_session_time_ns)
        self.assertEqual(first.pts_to_session_offset_ns, second.pts_to_session_offset_ns)
        self.assertEqual(
            second.end_session_time_ns - second.start_session_time_ns, 120 * FRAME_NS
        )

    def test_no_session_clock_leaves_session_times_empty(self) -> None:
        path0 = _open(self.pm, 0)
        _feed(self.pm, 0, 30)
        _open(self.pm, 30)
        self.pm._on_segment_file_closed(path0)
        (segment,) = self.pm._segment_index.all_for_feed("ndi_1")
        self.assertIsNone(segment.start_session_time_ns)
        self.assertIsNone(segment.end_session_time_ns)
        self.assertIsNone(segment.pts_to_session_offset_ns)

    def test_rows_round_trip_through_the_db(self) -> None:
        db = MetadataDb(Path(self._tmp.name) / "metadata.db")
        db.create_session("session_001", "Test", "2026-11-11T00:00:00+00:00")
        self.pm._metadata_db = db
        path0 = _open(self.pm, 0)
        _feed(self.pm, 0, 130)
        _open(self.pm, 119)
        self.pm._on_segment_file_closed(path0)
        (row,) = db.segments_for_session("session_001")
        self.assertTrue(row.end_exclusive)
        self.assertEqual(row.frame_count_estimate, 119)
        self.assertEqual(row.end_pts_ns - row.start_pts_ns, 119 * FRAME_NS)
        db.close()

    def test_null_drain_indexes_segments_still_waiting_and_the_open_one(self) -> None:
        path0 = _open(self.pm, 0)
        _feed(self.pm, 0, 70)
        path1 = _open(self.pm, 60)
        self.pm._finalize_closed_fragments(include_current=True)
        self.assertEqual(self.worker.submitted, [path0, path1])
        last = self.pm._segment_index.all_for_feed("ndi_1")[-1]
        # No next segment: ends one frame after its last buffer.
        self.assertEqual(last.end_pts_ns, 71 * FRAME_NS)
        self.assertEqual(last.frame_count_estimate, 11)

    def test_drain_without_current_drops_nothing_else(self) -> None:
        _open(self.pm, 0)
        _feed(self.pm, 0, 40)
        _open(self.pm, 30)
        self.pm._segment_bookkeeper_or_new().drop_current()
        self.pm._finalize_closed_fragments(include_current=False)
        self.assertEqual(len(self.worker.submitted), 1)

    def test_format_location_without_a_first_sample_indexes_nothing(self) -> None:
        _open(self.pm, 0)
        _feed(self.pm, 0, 20)
        with self.assertLogs("app.media.pipeline_manager", level="WARNING"):
            PipelineManager._on_splitmuxsink_format_location(self.pm, None, 1)
        self.pm._finalize_closed_fragments(include_current=True)
        self.assertEqual(self.worker.submitted, [])


@unittest.skipIf(_GST is None, "GStreamer (gi) not available")
class BusFragmentClosedRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.worker = _RecordingWorker()
        self.pm = _stub_pm(_session_paths(Path(self._tmp.name)), self.worker)
        self.pm._Gst = _GST

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _poll(self, structure) -> bool:
        Gst = _GST
        bus = Gst.Bus.new()
        bus.post(Gst.Message.new_element(None, structure))
        return self.pm._poll_bus_for_messages(
            bus, Gst.MessageType.ELEMENT, 0, pipeline_role="live", fatal=True
        )

    def test_fragment_closed_message_indexes_the_segment(self) -> None:
        path0 = _open(self.pm, 0)
        _feed(self.pm, 0, 12)
        _open(self.pm, 12)
        structure = _GST.Structure.new_empty("splitmuxsink-fragment-closed")
        structure.set_value("location", path0)
        structure.set_value("running-time", 12 * FRAME_NS)
        self.assertFalse(self._poll(structure))
        self.assertEqual(self.worker.submitted, [path0])

    def test_other_element_messages_are_ignored(self) -> None:
        path0 = _open(self.pm, 0)
        _feed(self.pm, 0, 12)
        _open(self.pm, 12)
        structure = _GST.Structure.new_empty("splitmuxsink-fragment-opened")
        structure.set_value("location", path0)
        self.assertFalse(self._poll(structure))
        self.assertEqual(self.worker.submitted, [])


def _count_frames(path: str) -> int:
    import cv2

    capture = cv2.VideoCapture(path)
    count = 0
    while capture.grab():
        count += 1
    capture.release()
    return count


@unittest.skipUnless(_HAS_RECORD_ELEMENTS, "GStreamer recording elements not available")
class SplitmuxsinkBoundsContractTests(unittest.TestCase):
    """Real splitmuxsink built by `_build_splitmuxsink_element` (app settings)."""

    def test_segments_meet_exactly_and_counts_match_the_files(self) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("cv2 not available")
        Gst = _GST
        with TemporaryDirectory() as tmp:
            worker = _RecordingWorker(validator=_default_segment_validator)
            pm = _stub_pm(_session_paths(Path(tmp)), worker)
            pm._Gst = Gst
            pm._session_clock = SessionClock()
            pipeline = Gst.parse_launch(
                "videotestsrc is-live=true pattern=ball ! "
                "video/x-raw,width=320,height=180,framerate=30000/1001 ! videoconvert ! "
                "video/x-raw,format=I420 ! jpegenc name=enc"
            )
            sink = PipelineManager._build_splitmuxsink_element(pm, "record")
            pipeline.add(sink)
            self.assertTrue(pipeline.get_by_name("enc").link(sink))
            pipeline.get_by_name("enc").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                pm._on_record_encoder_buffer_probe,
                None,
            )
            pm._pipeline = pipeline
            bus = pipeline.get_bus()

            def pump(seconds: float) -> None:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    pm._poll_bus_for_messages(
                        bus,
                        Gst.MessageType.ELEMENT,
                        int(Gst.SECOND / 20),
                        pipeline_role="test",
                        fatal=False,
                    )

            try:
                pipeline.set_state(Gst.State.PLAYING)
                pump(2.0)
                # What `disable_file_recording` does: split, let the split
                # happen, drop the throwaway, NULL, drain.
                sink.emit("split-now")
                pump(0.3)
                pm._segment_bookkeeper_or_new().drop_current()
            finally:
                pipeline.set_state(Gst.State.NULL)
            released_by_close_message = len(worker.submitted)
            pm._finalize_closed_fragments(include_current=False)

            segments = sorted(
                pm._segment_index.all_for_feed("ndi_1"), key=lambda s: s.start_pts_ns
            )
            self.assertGreaterEqual(len(segments), 4)
            self.assertGreaterEqual(released_by_close_message, len(segments) - 1)
            self.assertEqual(len(worker.submitted), len(set(worker.submitted)))
            self.assertEqual([n for n, ok in worker.results if not ok], [])
            for earlier, later in zip(segments, segments[1:]):
                self.assertEqual(earlier.end_pts_ns, later.start_pts_ns)
                self.assertEqual(earlier.end_session_time_ns, later.start_session_time_ns)
            self.assertEqual(len({s.pts_to_session_offset_ns for s in segments}), 1)
            for segment in segments:
                self.assertTrue(segment.end_exclusive)
                self.assertEqual(
                    segment.frame_count_estimate, _count_frames(segment.file_path),
                    f"{Path(segment.file_path).name}: DB frame count vs file",
                )
                period = segment.duration_ns / segment.frame_count_estimate
                self.assertAlmostEqual(period, 1e9 * 1001 / 30000, delta=1_000)
            # The decoder lands on the requested frame going backwards and
            # at random, as replay stepping does (pictures move: "ball").
            self._check_decoder_positions(segments[1].file_path)

    def _check_decoder_positions(self, path: str) -> None:
        import hashlib
        import random

        import cv2

        from app.media.segment_decoder import SegmentDecoder
        from app.storage.segment_replay_store import SegmentReplayLocation

        capture = cv2.VideoCapture(path)
        truth = []
        while True:
            ok, image = capture.read()
            if not ok:
                break
            truth.append(hashlib.md5(image.tobytes()).hexdigest())
        capture.release()
        order = list(range(len(truth) - 1, -1, -1)) + random.Random(7).sample(range(len(truth)), len(truth))
        decoder = SegmentDecoder("ndi_1", "ndi_1")
        segment = SimpleNamespace(file_path=path)
        try:
            wrong = [
                k for k in order
                if (frame := decoder.decode(
                    SegmentReplayLocation(segment=segment, offset_in_segment_ns=0, frame_index=k)
                )) is None or hashlib.md5(frame.image.tobytes()).hexdigest() != truth[k]
            ]
        finally:
            decoder.close()
        self.assertEqual(wrong, [], "frames the decoder got wrong")


if __name__ == "__main__":
    unittest.main()
