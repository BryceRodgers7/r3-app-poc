"""Segment validation waits for splitmuxsink to close the file.

With `async-finalize`, `format-location` for fragment N+1 fires while
fragment N's muxer is still writing its trailer. Validating at that
point failed on good files and quarantined them, leaving holes in
replay. `PipelineManager` now holds each finalized segment until
`splitmuxsink-fragment-closed` arrives for its file. These tests pin
that hand-off, the bus routing, and (with real GStreamer) the message
contract it relies on.
"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest

from app.core.models import SessionPaths
from app.media.pipeline_manager import PipelineManager
from app.storage.session_recovery import _default_segment_validator


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
    pm._pending_segment = None
    pm._metadata_db = None
    pm._segment_index = None
    pm._recording_running = True
    pm._feed_metrics = None
    pm._session_clock = None
    pm._segment_validator_worker = worker
    pm._segment_close_lock = threading.Lock()
    pm._segments_awaiting_close = {}
    pm._closed_segment_paths = set()
    pm._source = SimpleNamespace(get_feed_id=lambda: "ndi_1")
    pm._Gst = _GST
    return pm


def _open_segment(pm: PipelineManager, fragment_id: int) -> str:
    """Run format-location and fake the buffers the encoder probe would count."""
    path = PipelineManager._on_splitmuxsink_format_location(pm, None, fragment_id)
    pending = pm._pending_segment
    pending["first_pts_ns"] = fragment_id * 4_000_000_000
    pending["last_pts_ns"] = fragment_id * 4_000_000_000 + 3_966_000_000
    pending["frame_count"] = 120
    return path


class SegmentCloseGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.worker = _RecordingWorker()
        self.pm = _stub_pm(_session_paths(Path(self._tmp.name)), self.worker)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finalize_waits_for_close_message(self) -> None:
        path = _open_segment(self.pm, 0)
        PipelineManager._finalize_pending_segment_locked(self.pm)
        self.assertEqual(self.worker.submitted, [])

        self.pm._on_segment_file_closed(path)
        self.assertEqual(self.worker.submitted, [path])
        self.assertEqual(self.pm._segments_awaiting_close, {})

    def test_next_format_location_finalizes_but_does_not_submit(self) -> None:
        # Mirrors splitmuxsink's rotation: opening segment 1 finalizes
        # segment 0 while its file may still be mid-trailer.
        path0 = _open_segment(self.pm, 0)
        _open_segment(self.pm, 1)
        self.assertEqual(self.worker.submitted, [])
        self.pm._on_segment_file_closed(path0)
        self.assertEqual(self.worker.submitted, [path0])

    def test_close_before_finalize_submits_at_finalize(self) -> None:
        path = _open_segment(self.pm, 0)
        self.pm._on_segment_file_closed(path)
        self.assertEqual(self.worker.submitted, [])

        PipelineManager._finalize_pending_segment_locked(self.pm)
        self.assertEqual(self.worker.submitted, [path])
        self.assertEqual(self.pm._closed_segment_paths, set())

    def test_duplicate_close_message_submits_once(self) -> None:
        path = _open_segment(self.pm, 0)
        PipelineManager._finalize_pending_segment_locked(self.pm)
        self.pm._on_segment_file_closed(path)
        self.pm._on_segment_file_closed(path)
        self.assertEqual(self.worker.submitted, [path])

    def test_location_spelling_differences_still_match(self) -> None:
        path = _open_segment(self.pm, 0)
        PipelineManager._finalize_pending_segment_locked(self.pm)
        directory, name = os.path.split(path)
        location = os.path.join(directory, ".", name).replace("\\", "/")
        self.pm._on_segment_file_closed(location)
        self.assertEqual(self.worker.submitted, [path])

    def test_drain_submits_waiting_and_forgets_unmatched_closes(self) -> None:
        path0 = _open_segment(self.pm, 0)
        path1 = _open_segment(self.pm, 1)
        PipelineManager._finalize_pending_segment_locked(self.pm)
        self.pm._on_segment_file_closed(str(Path(self._tmp.name) / "throwaway.mkv"))

        self.pm._submit_segments_awaiting_close()
        self.assertCountEqual(self.worker.submitted, [path0, path1])
        self.assertEqual(self.pm._segments_awaiting_close, {})
        self.assertEqual(self.pm._closed_segment_paths, set())

    def test_drain_on_uninitialized_stub_is_noop(self) -> None:
        # `disable_file_recording` is driven on bare `__new__` stubs in
        # other test modules; the drain must tolerate that.
        PipelineManager._submit_segments_awaiting_close(PipelineManager.__new__(PipelineManager))


@unittest.skipIf(_GST is None, "GStreamer (gi) not available")
class BusFragmentClosedRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.worker = _RecordingWorker()
        self.pm = _stub_pm(_session_paths(Path(self._tmp.name)), self.worker)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _poll(self, structure) -> bool:
        Gst = _GST
        bus = Gst.Bus.new()
        bus.post(Gst.Message.new_element(None, structure))
        return self.pm._poll_bus_for_messages(
            bus, Gst.MessageType.ELEMENT, 0, pipeline_role="live", fatal=True
        )

    def test_fragment_closed_message_releases_validation(self) -> None:
        path = _open_segment(self.pm, 0)
        PipelineManager._finalize_pending_segment_locked(self.pm)
        structure = _GST.Structure.new_empty("splitmuxsink-fragment-closed")
        structure.set_value("location", path)
        self.assertFalse(self._poll(structure))
        self.assertEqual(self.worker.submitted, [path])

    def test_other_element_messages_are_ignored(self) -> None:
        path = _open_segment(self.pm, 0)
        PipelineManager._finalize_pending_segment_locked(self.pm)
        structure = _GST.Structure.new_empty("splitmuxsink-fragment-opened")
        structure.set_value("location", path)
        self.assertFalse(self._poll(structure))
        self.assertEqual(self.worker.submitted, [])
        self.assertEqual(self.pm._closed_segment_paths, set())


@unittest.skipUnless(_HAS_RECORD_ELEMENTS, "GStreamer recording elements not available")
class SplitmuxsinkCloseContractTests(unittest.TestCase):
    """Real splitmuxsink with the app's settings (async-finalize, matroskamux)."""

    def test_every_segment_validated_once_after_close(self) -> None:
        Gst = _GST
        with TemporaryDirectory() as tmp:
            worker = _RecordingWorker(validator=_default_segment_validator)
            pm = _stub_pm(_session_paths(Path(tmp)), worker)
            pipeline = Gst.parse_launch(
                "videotestsrc is-live=true pattern=ball ! "
                "video/x-raw,width=640,height=360,framerate=30/1 ! videoconvert ! "
                "video/x-raw,format=I420 ! jpegenc name=enc ! "
                "splitmuxsink name=s max-size-time=400000000 "
                "muxer-factory=matroskamux async-finalize=true"
            )
            pipeline.get_by_name("s").connect(
                "format-location", pm._on_splitmuxsink_format_location
            )
            pipeline.get_by_name("enc").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, pm._on_record_encoder_buffer_probe, None
            )
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
                pump(1.6)
                pipeline.send_event(Gst.Event.new_eos())
                pump(0.6)
                # What `disable_file_recording` does after its split:
                # finalize the last segment, go to NULL, drain.
                PipelineManager._finalize_pending_segment_locked(pm)
            finally:
                pipeline.set_state(Gst.State.NULL)
            released_by_close_message = len(worker.submitted)
            pm._submit_segments_awaiting_close()

            # The close message, not the teardown drain, should release
            # (nearly) every segment.
            self.assertGreaterEqual(released_by_close_message, 3)
            self.assertLessEqual(len(worker.submitted) - released_by_close_message, 1)
            self.assertEqual(len(worker.submitted), len(set(worker.submitted)))
            self.assertEqual(
                [name for name, ok in worker.results if not ok], [],
                "segments failed validation at submit time",
            )


if __name__ == "__main__":
    unittest.main()
