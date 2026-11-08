"""Mid-game rebuild recovery and the game-1 start path.

Two bugs found while debugging the Begin Game freeze (docs/NDI_OPEN_BUGS.md):

1. After `FeedRuntime.rebuild` (stop_all + connect_source) nothing re-armed
   recording, so the rebuilt pipeline's segment files were never indexed.
2. `_teardown_pipeline` left `_audio_record_encoder` pointing at the dead
   pipeline's opusenc, so the next Start's splitmuxsink rebuild raised
   `WRONG_HIERARCHY` and that feed recorded nothing in the following game.

And the first Start of a session now takes the same fresh-splitmuxsink path
as every later game instead of splicing an audio pad onto a running one.

All GStreamer objects are stubbed, matching the other PipelineManager tests.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from app.media.pipeline_manager import PipelineManager


def _bare_pm() -> PipelineManager:
    pm = PipelineManager.__new__(PipelineManager)
    pm._source = SimpleNamespace(
        get_feed_id=lambda: "ndi_1",
        disconnect_source=lambda: None,
        connect_source=lambda: True,
    )
    pm._stop_event = threading.Event()
    pm._preview_running = False
    pm._recording_running = False
    pm._recording_resume_after_rebuild = False
    pm._recording_disable_count = 0
    pm._pipeline_generation = 1
    pm._ingest_frame_count = 0
    pm._pending_segment = None
    pm._segment_validator_worker = None
    pm._appsrc = None
    pm._frame_feed_thread = None
    pm._audio_feed_thread = None
    pm._bus_thread = None
    pm._recording_feed_id = "ndi_1"
    pm._recording_session_paths = None
    pm._recording_game_subdir = None
    pm._recording_segment_counter = 0
    pm._recording_audio_enabled = True
    pm._audio_format = object()
    pm._audio_present_observed = False
    return pm


class StopAllRemembersRecordingTests(unittest.TestCase):
    def _stoppable(self, *, recording: bool) -> tuple[PipelineManager, list[str]]:
        pm = _bare_pm()
        pm._recording_running = recording
        calls: list[str] = []
        pm._set_branch_enabled = lambda *_a, **_k: None
        pm._teardown_pipeline = lambda: calls.append("teardown")
        pm._finalize_pending_segment_locked = lambda: calls.append("finalize")
        pm._submit_segments_awaiting_close = lambda: calls.append("submit")
        return pm, calls

    def test_stop_all_mid_game_flags_a_resume_and_indexes_the_partial_segment(self) -> None:
        pm, calls = self._stoppable(recording=True)
        pm.stop_all()
        self.assertTrue(pm._recording_resume_after_rebuild)
        self.assertFalse(pm._recording_running)
        # Indexed after teardown (file closed) and before validation is
        # submitted; it must not survive into the new pipeline.
        self.assertEqual(calls, ["teardown", "finalize", "submit"])

    def test_stop_all_when_not_recording_does_not_flag_a_resume(self) -> None:
        pm, calls = self._stoppable(recording=False)
        pm._pending_segment = {"stale": True}
        pm.stop_all()
        self.assertFalse(pm._recording_resume_after_rebuild)
        self.assertNotIn("finalize", calls)
        self.assertIsNone(pm._pending_segment)


class ConnectSourceTests(unittest.TestCase):
    def _connectable(self) -> tuple[PipelineManager, list[tuple]]:
        pm = _bare_pm()
        enabled: list[tuple] = []
        pm._ensure_gstreamer_loaded = lambda: None
        pm._build_pipeline = lambda: None
        pm._start_pipeline_threads = lambda: None
        pm._set_branch_enabled = lambda *a: enabled.append(a)
        pm._schedule_recording_resume = lambda: enabled.append(("resume",))
        return pm, enabled

    def test_connect_never_opens_the_record_valve(self) -> None:
        # The valve used to be opened here, so splitmuxsink wrote throwaway
        # files all session and Begin Game had to splice audio onto it.
        pm, calls = self._connectable()
        self.assertTrue(pm.connect_source())
        self.assertNotIn(("record", True), calls)

    def test_connect_after_a_mid_game_stop_schedules_the_resume(self) -> None:
        pm, calls = self._connectable()
        pm._recording_resume_after_rebuild = True
        pm.connect_source()
        self.assertIn(("resume",), calls)

    def test_plain_connect_does_not_schedule_a_resume(self) -> None:
        pm, calls = self._connectable()
        pm.connect_source()
        self.assertNotIn(("resume",), calls)


class ResumeWorkerTests(unittest.TestCase):
    def _worker_pm(self) -> tuple[PipelineManager, list[dict]]:
        pm = _bare_pm()
        pm.RESUME_VIDEO_FRAMES = 3
        pm.RESUME_AUDIO_GRACE_SECONDS = 0.05
        pm.RESUME_GIVE_UP_SECONDS = 1.0
        pm._recording_resume_after_rebuild = True
        pm._recording_session_paths = SimpleNamespace(session_id="session_001")
        pm._recording_game_subdir = "game_001"
        pm._recording_segment_counter = 7
        pm._ingest_frame_count = 10
        pm._audio_present_observed = True
        enables: list[dict] = []

        def fake_enable(paths, feed_id, **kwargs):
            enables.append({"paths": paths, "feed_id": feed_id, **kwargs})
            pm._recording_resume_after_rebuild = False  # as the real one does

        pm.enable_file_recording = fake_enable
        pm.disable_file_recording = lambda: enables.append({"disabled": True})
        return pm, enables

    def _run(self, pm: PipelineManager, generation: int = 1) -> None:
        with mock.patch("app.media.pipeline_manager.record_health_event") as events:
            pm._resume_recording_worker(generation)
            self.events = events

    def test_rearms_with_the_counter_and_game_folder_of_the_old_pipeline(self) -> None:
        pm, enables = self._worker_pm()
        self._run(pm)
        self.assertEqual(len(enables), 1)
        self.assertEqual(enables[0]["start_fragment_index"], 7)  # no filename reuse
        self.assertEqual(enables[0]["game_subdir"], "game_001")
        self.assertEqual(enables[0]["feed_id"], "ndi_1")
        categories = [c.kwargs["category"] for c in self.events.call_args_list]
        self.assertEqual(categories, ["recording_resumed"])

    def test_waits_for_video_before_rearming(self) -> None:
        pm, enables = self._worker_pm()
        pm._ingest_frame_count = 0  # nothing from the new pipeline yet
        pm.RESUME_GIVE_UP_SECONDS = 0.3
        self._run(pm)
        self.assertEqual(enables, [])
        categories = [c.kwargs["category"] for c in self.events.call_args_list]
        self.assertEqual(categories, ["recording_resume_failed"])

    def test_gives_audio_a_short_grace_then_rearms_video_only(self) -> None:
        pm, enables = self._worker_pm()
        pm._audio_present_observed = False  # camera has no audio
        self._run(pm)
        self.assertEqual(len(enables), 1)

    def test_does_not_wait_for_audio_when_it_is_not_being_recorded(self) -> None:
        pm, enables = self._worker_pm()
        pm._audio_present_observed = False
        pm._recording_audio_enabled = False
        pm.RESUME_AUDIO_GRACE_SECONDS = 60.0  # would time the test out
        self._run(pm)
        self.assertEqual(len(enables), 1)

    def test_a_newer_rebuild_supersedes_the_worker(self) -> None:
        pm, enables = self._worker_pm()
        pm._pipeline_generation = 2  # the worker was started for generation 1
        self._run(pm, generation=1)
        self.assertEqual(enables, [])

    def test_an_operator_stop_cancels_the_resume(self) -> None:
        pm, enables = self._worker_pm()
        pm._recording_resume_after_rebuild = False  # disable_file_recording cleared it
        self._run(pm)
        self.assertEqual(enables, [])

    def test_a_stop_that_lands_during_the_rearm_is_undone(self) -> None:
        pm, enables = self._worker_pm()
        real_fake_enable = pm.enable_file_recording

        def enable_then_stop(paths, feed_id, **kwargs):
            real_fake_enable(paths, feed_id, **kwargs)
            pm._recording_disable_count += 1  # operator pressed Stop meanwhile

        pm.enable_file_recording = enable_then_stop
        self._run(pm)
        self.assertEqual(enables[-1], {"disabled": True})


class EnableWhileFeedIsDownTests(unittest.TestCase):
    def test_start_during_a_rebuild_is_remembered_for_the_reconnect(self) -> None:
        pm = _bare_pm()
        pm._splitmuxsink = None  # torn down, not yet reconnected
        paths = SimpleNamespace(session_id="session_001")
        pm.enable_file_recording(
            paths, "ndi_1", start_fragment_index=3, game_subdir="game_002"
        )
        self.assertTrue(pm._recording_resume_after_rebuild)
        self.assertIs(pm._recording_session_paths, paths)
        self.assertEqual(pm._recording_game_subdir, "game_002")
        self.assertEqual(pm._recording_segment_counter, 3)
        self.assertFalse(pm._recording_running)


class TeardownResetsPerPipelineStateTests(unittest.TestCase):
    def test_teardown_drops_references_to_the_dead_pipelines_elements(self) -> None:
        pm = _bare_pm()
        pm._Gst = SimpleNamespace(State=SimpleNamespace(NULL="NULL"))
        pm._pipeline_lock = threading.Lock()
        pm._metadata_lock = threading.Lock()
        pm._frame_metadata = OrderedDict()
        pm._pipeline = mock.MagicMock()
        pm._pipeline.get_by_name.return_value = None
        for name in (
            "_preview_probe_pad", "_preview_probe_id", "_preview_sink",
            "_preview_sink_factory_name", "_ingest_rate_element",
            "_audio_appsrc", "_bus", "_stream_start_timestamp",
            "_audio_stream_start_timestamp",
        ):
            setattr(pm, name, None)
        pm._tee_request_pads = []
        pm._audio_tee_request_pads = []
        pm._branch_valves = {}
        pm._audio_branch_valves = {}
        # State left over from the pipeline being torn down:
        pm._audio_record_encoder = object()
        pm._audio_present_observed = True
        pm._audio_record_first_buffer_at_ns = 123
        pm._audio_stale_pts_drop_floor_ns = 456
        pm._splitmuxsink = object()
        pm._record_branch_encoder = object()

        pm._teardown_pipeline()

        self.assertIsNone(pm._audio_record_encoder)
        self.assertFalse(pm._audio_present_observed)
        self.assertIsNone(pm._audio_record_first_buffer_at_ns)
        self.assertIsNone(pm._audio_stale_pts_drop_floor_ns)
        self.assertIsNone(pm._splitmuxsink)
        self.assertIsNone(pm._record_branch_encoder)


class SplitmuxsinkConfigTests(unittest.TestCase):
    def test_splitmuxsink_is_built_with_async_handling(self) -> None:
        # With the record valve closed until Start, splitmuxsink never
        # prerolls; without async-handling the pipeline's PAUSED->PLAYING
        # change stays pending and a live (async) preview sink gets no frames.
        pm = _bare_pm()
        pm._Gst = SimpleNamespace(SECOND=1_000_000_000)
        pm._recording_segment_duration_seconds = 4.0
        pm._recording_container = "mkv"
        props: dict[str, object] = {}

        class _Element:
            def set_property(self, name: str, value: object) -> None:
                props[name] = value

            def connect(self, *_args: object) -> None:
                pass

        pm._make_element = lambda _factory, _name: _Element()
        pm._recording_muxer_factory = lambda: "matroskamux"
        pm._build_splitmuxsink_element("record")
        self.assertIs(props.get("async-handling"), True)
        self.assertIs(props.get("async-finalize"), True)
        self.assertEqual(props.get("max-size-time"), 4_000_000_000)


class _Pad:
    def __init__(self) -> None:
        self.peer = None

    def get_peer(self):
        return self.peer


class _Encoder:
    def __init__(self) -> None:
        self.src = _Pad()

    def get_static_pad(self, _name: str) -> _Pad:
        return self.src


class _Sink:
    def __init__(self) -> None:
        self.states: list[object] = []

    def set_state(self, state: object) -> None:
        self.states.append(state)

    def sync_state_with_parent(self) -> bool:
        return True


class FirstStartRebuildTests(unittest.TestCase):
    """The first Start now goes through `_rebuild_splitmuxsink_locked`."""

    def _rebuildable(self, *, chain_existed: bool):
        pm = _bare_pm()
        pm._Gst = SimpleNamespace(State=SimpleNamespace(NULL="NULL"))
        pm._pipeline = SimpleNamespace(
            remove=lambda _e: True, add=lambda _e: True
        )
        pm._record_branch_encoder = SimpleNamespace(
            unlink=lambda _o: None, link=lambda _o: True,
            get_name=lambda: "record_jpegenc",
        )
        pm._record_branch_name = "record"
        pm._splitmuxsink = _Sink()
        new_sink = _Sink()
        pm._build_splitmuxsink_element = lambda _b: new_sink
        pm._audio_record_encoder = _Encoder() if chain_existed else None
        built: list[str] = []
        linked: list[object] = []

        def fake_ensure() -> None:
            built.append("built")
            pm._audio_record_encoder = _Encoder()  # build wires its own pad

        pm._ensure_audio_record_branch_built_locked = fake_ensure
        pm._link_audio_encoder_to_splitmuxsink_locked = lambda s: linked.append(s)
        return pm, new_sink, built, linked

    def test_audio_chain_built_during_the_rebuild_gets_exactly_one_pad(self) -> None:
        # A second audio_%u request would leave an empty audio stream, and
        # splitmuxsink holds the video back until every stream has data.
        pm, _new_sink, built, linked = self._rebuildable(chain_existed=False)
        pm._rebuild_splitmuxsink_locked()
        self.assertEqual(built, ["built"])
        self.assertEqual(linked, [])

    def test_existing_audio_chain_is_relinked_to_the_new_sink(self) -> None:
        pm, new_sink, _built, linked = self._rebuildable(chain_existed=True)
        pm._ensure_audio_record_branch_built_locked = lambda: None
        pm._rebuild_splitmuxsink_locked()
        self.assertEqual(linked, [new_sink])

    def test_first_start_rebuilds_a_pipeline_that_was_never_enabled(self) -> None:
        pm = _bare_pm()
        pm._splitmuxsink = object()
        pm._recording_was_disabled = True  # what `_build_pipeline` now sets
        pm._audio_record_encoder = None
        rebuilt: list[bool] = []
        pm._rebuild_splitmuxsink_locked = lambda: rebuilt.append(True)
        pm._ensure_audio_record_branch_built_locked = lambda: None
        pm._arm_audio_stale_pts_drop_locked = lambda: None
        opened: list[tuple] = []
        pm._set_branch_enabled = lambda *a: opened.append(a)
        with tempfile.TemporaryDirectory() as tmp:
            paths = SimpleNamespace(
                session_id="session_001", recording_dir=Path(tmp)
            )
            pm.enable_file_recording(
                paths, "ndi_1", start_fragment_index=0, game_subdir="game_001"
            )
        self.assertEqual(rebuilt, [True])
        self.assertEqual(opened, [("record", True)])
        self.assertTrue(pm._recording_running)
        self.assertFalse(pm._recording_was_disabled)


if __name__ == "__main__":
    unittest.main()
