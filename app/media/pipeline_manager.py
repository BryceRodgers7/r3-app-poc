"""GStreamer-centered media graph orchestration for the replay application."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
import importlib
import threading
import time
from typing import Any

import numpy as np

from app.core.models import MediaFrame, SessionPaths
from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer
from app.media.source_interface import SourceInterface


@dataclass(slots=True)
class _FrameMetadata:
    """Tracks source-side metadata while buffers fan out through GStreamer."""

    timestamp: float
    source_name: str


class PipelineManager:
    """Owns the transitional GStreamer media graph for the PoC.

    The current source still comes from Python via `SourceInterface.read_frame()`,
    but frames now enter a real `appsrc -> tee -> branches` pipeline. This keeps
    tee/fan-out explicit today and makes it straightforward to swap in an NDI or
    other native GStreamer source bin later.
    """

    def __init__(
        self,
        source: SourceInterface,
        preview_output: PreviewOutput,
        recorder: Recorder,
        replay_buffer: ReplayBuffer,
    ) -> None:
        self._source = source
        self._preview_output = preview_output
        self._recorder = recorder
        self._replay_buffer = replay_buffer
        self._preview_running = False
        self._recording_running = False
        self._replay_running = False
        self._frame_callback: Callable[[MediaFrame], None] | None = None

        self._Gst: Any | None = None
        self._GstVideo: Any | None = None
        self._pipeline: Any | None = None
        self._appsrc: Any | None = None
        self._bus: Any | None = None
        self._replay_pipeline: Any | None = None
        self._replay_appsrc: Any | None = None
        self._replay_bus: Any | None = None
        self._tee_request_pads: list[Any] = []
        self._branch_valves: dict[str, Any] = {}
        self._preview_sink: Any | None = None
        self._preview_sink_factory_name: str | None = None
        self._replay_sink: Any | None = None
        self._replay_sink_factory_name: str | None = None
        self._preview_probe_pad: Any | None = None
        self._preview_probe_id: int | None = None
        self._video_window_handle: int | None = None
        self._replay_push_count = 0
        self._active_video_output = "live"
        self._replay_display_active = False
        self._replay_feed_thread: threading.Thread | None = None
        self._replay_frame_lock = threading.Lock()
        self._replay_frame_ready = threading.Event()
        self._replay_pending_frame: MediaFrame | None = None

        self._frame_feed_thread: threading.Thread | None = None
        self._bus_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pipeline_lock = threading.Lock()

        self._frame_duration_ns = 0
        self._stream_start_timestamp: float | None = None
        self._frame_metadata: OrderedDict[int, _FrameMetadata] = OrderedDict()
        self._metadata_lock = threading.Lock()
        self._live_sample_callback: Callable[[float, str], None] | None = None

    def describe_architecture(self) -> str:
        """Describe the current transitional tee/fan-out architecture."""
        return (
            "SourceInterface.read_frame -> appsrc -> videoconvert -> tee -> "
            "[preview embedded video sink, recording appsink, replay appsink] + "
            "replay buffer -> replay appsrc -> embedded replay sink"
        )

    def start_preview(self) -> None:
        """Start the preview branch without affecting recording or replay buffering."""
        self._preview_running = True
        self._preview_output.show_placeholder_message("Starting live preview...")
        if self._active_video_output == "live":
            self._set_branch_enabled("preview", True)

    def start_recording(self, session_paths: SessionPaths) -> None:
        """Start the full-session recording branch."""
        self._recorder.start(
            session_paths=session_paths,
            source_name=self._source.get_display_name(),
            fps_hint=self._source.get_nominal_fps(),
        )
        self._recording_running = True
        self._set_branch_enabled("record", True)

    def start_replay_buffer(self, session_paths: SessionPaths) -> None:
        """Start the rolling replay buffer branch."""
        self._replay_buffer.start(session_paths)
        self._replay_running = True
        self._set_branch_enabled("replay", True)

    def stop_preview(self) -> None:
        """Stop only the preview branch."""
        self._preview_running = False
        self._set_branch_enabled("preview", False)

    def stop_recording(self) -> None:
        """Stop only the recording branch."""
        self._recording_running = False
        self._set_branch_enabled("record", False)
        self._recorder.stop()

    def stop_replay_buffer(self) -> None:
        """Stop only the rolling replay buffer branch."""
        self._replay_running = False
        self._set_branch_enabled("replay", False)
        self._replay_buffer.stop()

    def stop_all(self) -> None:
        """Stop all branches, tear down the pipeline, and disconnect the source."""
        self._preview_running = False
        self._recording_running = False
        self._replay_running = False
        self._stop_event.set()

        for branch_name in ("preview", "record", "replay"):
            self._set_branch_enabled(branch_name, False)

        if self._appsrc is not None:
            try:
                self._appsrc.emit("end-of-stream")
            except Exception:
                pass

        if self._replay_appsrc is not None:
            try:
                self._replay_appsrc.emit("end-of-stream")
            except Exception:
                pass

        if self._frame_feed_thread is not None:
            self._frame_feed_thread.join(timeout=2.0)
            self._frame_feed_thread = None

        self._replay_frame_ready.set()
        if self._replay_feed_thread is not None:
            self._replay_feed_thread.join(timeout=2.0)
            self._replay_feed_thread = None

        if self._bus_thread is not None:
            self._bus_thread.join(timeout=2.0)
            self._bus_thread = None

        self._teardown_pipeline()
        self._recorder.stop()
        self._replay_buffer.stop()
        self._source.disconnect_source()

    def is_source_connected(self) -> bool:
        """Return whether the underlying ingest source is connected."""
        return self._source.is_connected()

    def connect_source(self) -> bool:
        """Connect the source and build the GStreamer pipeline."""
        if not self._source.connect_source():
            return False

        try:
            self._ensure_gstreamer_loaded()
            self._build_pipeline()
            self._start_pipeline_threads()
        except Exception:
            self._teardown_pipeline()
            self._source.disconnect_source()
            raise
        return True

    def set_frame_callback(self, callback: Callable[[MediaFrame], None]) -> None:
        """Register the legacy preview-frame callback."""
        self._frame_callback = callback

    def set_live_sample_callback(self, callback: Callable[[float, str], None]) -> None:
        """Register the controller callback for live-preview timestamps."""
        self._live_sample_callback = callback

    def set_video_window_handle(self, window_handle: int) -> None:
        """Attach the active embedded video sink to a Qt-owned native child window."""
        self._video_window_handle = int(window_handle)
        with self._pipeline_lock:
            self._bind_active_video_sink_locked()

    def set_preview_window_handle(self, window_handle: int) -> None:
        """Backward-compatible wrapper for the shared video surface handle."""
        self.set_video_window_handle(window_handle)

    def refresh_active_video_output(self) -> None:
        """Ask the active embedded video sink to redraw into the current window."""
        with self._pipeline_lock:
            self._bind_active_video_sink_locked(expose=True)

    def refresh_preview_overlay(self) -> None:
        """Backward-compatible wrapper for refreshing the active video sink."""
        self.refresh_active_video_output()

    def get_preview_sink_name(self) -> str | None:
        """Return the selected preview sink factory name."""
        return self._preview_sink_factory_name

    def get_replay_sink_name(self) -> str | None:
        """Return the selected replay sink factory name."""
        return self._replay_sink_factory_name

    def activate_live_output(self) -> None:
        """Route the shared video surface back to the live preview sink."""
        with self._pipeline_lock:
            if self._active_video_output == "live":
                self._replay_display_active = False
                with self._replay_frame_lock:
                    self._replay_pending_frame = None
                self._replay_frame_ready.clear()
                self._set_branch_enabled("preview", self._preview_running)
                self._bind_active_video_sink_locked()
                return

            self._active_video_output = "live"
            self._replay_display_active = False
            with self._replay_frame_lock:
                self._replay_pending_frame = None
            self._replay_frame_ready.clear()
            self._set_branch_enabled("preview", self._preview_running)
            if self._replay_pipeline is not None:
                self._replay_pipeline.set_state(self._Gst.State.PAUSED)
            self._bind_active_video_sink_locked()

    def show_replay_frame(self, frame: MediaFrame) -> None:
        """Display a replay frame through the dedicated replay playback pipeline."""
        with self._pipeline_lock:
            if self._replay_pipeline is None or self._replay_appsrc is None:
                raise RuntimeError("Replay playback pipeline is not available.")

            if self._active_video_output != "replay":
                self._active_video_output = "replay"
                self._set_branch_enabled("preview", False)
                self._replay_pipeline.set_state(self._Gst.State.PLAYING)
                self._bind_active_video_sink_locked()
            self._replay_display_active = True

        with self._replay_frame_lock:
            self._replay_pending_frame = frame
            self._replay_frame_ready.set()

    def get_source_name(self) -> str:
        """Return the current source display name."""
        return self._source.get_display_name()

    def _ensure_gstreamer_loaded(self) -> None:
        if self._Gst is not None:
            return

        try:
            gi = importlib.import_module("gi")
            gi.require_version("Gst", "1.0")
            gi.require_version("GstVideo", "1.0")
            gst_module = importlib.import_module("gi.repository.Gst")
            gst_video_module = importlib.import_module("gi.repository.GstVideo")
        except Exception as exc:
            raise RuntimeError(
                "GStreamer via PyGObject is required for the current PipelineManager implementation."
            ) from exc

        gst_module.init(None)
        self._Gst = gst_module
        self._GstVideo = gst_video_module

    def _build_pipeline(self) -> None:
        with self._pipeline_lock:
            if self._pipeline is not None:
                return

            Gst = self._Gst
            assert Gst is not None

            width, height = self._source.get_frame_size()
            fps_fraction = Fraction(str(self._source.get_nominal_fps())).limit_denominator(1000)
            self._frame_duration_ns = max(1, int(Gst.SECOND * fps_fraction.denominator / fps_fraction.numerator))

            pipeline = Gst.Pipeline.new("sports-replay-pipeline")
            appsrc = self._make_element("appsrc", "source_appsrc")
            source_convert = self._make_element("videoconvert", "source_convert")
            tee = self._make_element("tee", "source_tee")

            appsrc.set_property("is-live", True)
            appsrc.set_property("format", Gst.Format.TIME)
            appsrc.set_property("block", True)
            appsrc.set_property("do-timestamp", False)
            appsrc.set_property(
                "caps",
                Gst.Caps.from_string(
                    "video/x-raw,format=BGR,"
                    f"width={width},height={height},"
                    f"framerate={fps_fraction.numerator}/{fps_fraction.denominator}"
                ),
            )

            pipeline.add(appsrc)
            pipeline.add(source_convert)
            pipeline.add(tee)
            if not appsrc.link(source_convert) or not source_convert.link(tee):
                raise RuntimeError("Failed to link the GStreamer source path.")

            self._pipeline = pipeline
            self._appsrc = appsrc
            self._bus = pipeline.get_bus()
            if self._bus is not None:
                self._bus.enable_sync_message_emission()
                self._bus.connect("sync-message::element", self._on_bus_sync_message)
            self._tee_request_pads.clear()
            self._branch_valves.clear()
            self._preview_sink = None
            self._preview_sink_factory_name = None
            self._replay_sink = None
            self._replay_sink_factory_name = None
            self._preview_probe_pad = None
            self._preview_probe_id = None
            self._replay_push_count = 0
            self._active_video_output = "live"
            self._replay_display_active = False
            with self._replay_frame_lock:
                self._replay_pending_frame = None
            self._replay_frame_ready.clear()

            self._add_branch("preview", self._on_preview_sample)
            self._add_branch("record", self._on_record_sample)
            self._add_branch("replay", self._on_replay_sample)
            self._build_replay_pipeline(width=width, height=height, fps_fraction=fps_fraction)

            state_change = pipeline.set_state(Gst.State.PLAYING)
            if state_change == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Failed to move the GStreamer pipeline to PLAYING.")
            if self._replay_pipeline is not None:
                replay_state_change = self._replay_pipeline.set_state(Gst.State.PAUSED)
                if replay_state_change == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Failed to move the replay playback pipeline to PAUSED.")

    def _add_branch(self, branch_name: str, sample_handler: Callable[[Any], Any]) -> None:
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        if branch_name == "preview":
            self._add_preview_branch(branch_name)
            return

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        sink = self._make_element("appsink", f"{branch_name}_sink")

        valve.set_property("drop", True)
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 1 if branch_name == "preview" else 8)
        if branch_name == "preview":
            sink.set_property("drop", True)
        sink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGR"))
        sink.connect("new-sample", sample_handler)

        self._pipeline.add(queue)
        self._pipeline.add(valve)
        self._pipeline.add(convert)
        self._pipeline.add(sink)

        if not queue.link(valve) or not valve.link(convert) or not convert.link(sink):
            raise RuntimeError(f"Failed to link the {branch_name} branch.")

        tee_src_pad = self._request_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if tee_src_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Failed to link tee output to the {branch_name} branch.")

        queue.sync_state_with_parent()
        valve.sync_state_with_parent()
        convert.sync_state_with_parent()
        sink.sync_state_with_parent()

        self._branch_valves[branch_name] = valve

    def _add_preview_branch(self, branch_name: str) -> None:
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        sink, sink_factory_name = self._make_video_sink(f"{branch_name}_sink")

        valve.set_property("drop", True)
        self._set_property_if_supported(sink, "sync", False)
        self._set_property_if_supported(sink, "qos", True)
        self._set_property_if_supported(sink, "force-aspect-ratio", True)

        self._pipeline.add(queue)
        self._pipeline.add(valve)
        self._pipeline.add(convert)
        self._pipeline.add(sink)

        if not queue.link(valve) or not valve.link(convert) or not convert.link(sink):
            raise RuntimeError(f"Failed to link the {branch_name} branch.")

        tee_src_pad = self._request_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if tee_src_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Failed to link tee output to the {branch_name} branch.")

        preview_probe_pad = queue.get_static_pad("src")
        if preview_probe_pad is None:
            raise RuntimeError("Failed to access the preview branch source pad.")
        self._preview_probe_id = preview_probe_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_preview_buffer)
        self._preview_probe_pad = preview_probe_pad

        queue.sync_state_with_parent()
        valve.sync_state_with_parent()
        convert.sync_state_with_parent()
        sink.sync_state_with_parent()

        self._branch_valves[branch_name] = valve
        self._preview_sink = sink
        self._preview_sink_factory_name = sink_factory_name
        self._bind_active_video_sink_locked()

    def _build_replay_pipeline(self, width: int, height: int, fps_fraction: Fraction) -> None:
        assert self._Gst is not None

        replay_pipeline = self._Gst.Pipeline.new("sports-replay-display-pipeline")
        replay_appsrc = self._make_element("appsrc", "replay_appsrc")
        replay_convert = self._make_element("videoconvert", "replay_convert")
        replay_sink, replay_sink_factory_name = self._make_video_sink("replay_sink")

        replay_appsrc.set_property("is-live", False)
        replay_appsrc.set_property("format", self._Gst.Format.TIME)
        replay_appsrc.set_property("block", True)
        replay_appsrc.set_property("do-timestamp", False)
        replay_appsrc.set_property(
            "caps",
            self._Gst.Caps.from_string(
                "video/x-raw,format=BGR,"
                f"width={width},height={height},"
                f"framerate={fps_fraction.numerator}/{fps_fraction.denominator}"
            ),
        )
        self._set_property_if_supported(replay_sink, "sync", False)
        self._set_property_if_supported(replay_sink, "qos", True)
        self._set_property_if_supported(replay_sink, "force-aspect-ratio", True)

        replay_pipeline.add(replay_appsrc)
        replay_pipeline.add(replay_convert)
        replay_pipeline.add(replay_sink)
        if not replay_appsrc.link(replay_convert) or not replay_convert.link(replay_sink):
            raise RuntimeError("Failed to link the replay playback pipeline.")

        self._replay_pipeline = replay_pipeline
        self._replay_appsrc = replay_appsrc
        self._replay_bus = replay_pipeline.get_bus()
        if self._replay_bus is not None:
            self._replay_bus.enable_sync_message_emission()
            self._replay_bus.connect("sync-message::element", self._on_bus_sync_message)
        self._replay_sink = replay_sink
        self._replay_sink_factory_name = replay_sink_factory_name
        self._bind_active_video_sink_locked()

    def _request_tee_pad(self) -> Any:
        assert self._pipeline is not None
        tee = self._pipeline.get_by_name("source_tee")
        assert tee is not None

        request_pad = tee.request_pad_simple("src_%u")
        if request_pad is None:
            request_pad = tee.get_request_pad("src_%u")
        if request_pad is None:
            raise RuntimeError("Failed to request a tee source pad.")

        self._tee_request_pads.append(request_pad)
        return request_pad

    def _make_element(self, factory_name: str, element_name: str) -> Any:
        Gst = self._Gst
        assert Gst is not None

        element = Gst.ElementFactory.make(factory_name, element_name)
        if element is None:
            raise RuntimeError(f"Failed to create GStreamer element '{factory_name}'.")
        return element

    def _make_video_sink(self, element_name: str) -> tuple[Any, str]:
        Gst = self._Gst
        assert Gst is not None

        for factory_name in ("d3d11videosink", "glimagesink", "d3dvideosink"):
            if Gst.ElementFactory.find(factory_name) is None:
                continue
            sink = Gst.ElementFactory.make(factory_name, element_name)
            if sink is not None:
                return sink, factory_name

        raise RuntimeError(
            "Failed to create an embedded preview video sink. "
            "Tried d3d11videosink, glimagesink, and d3dvideosink."
        )

    def _set_property_if_supported(self, element: Any, property_name: str, value: Any) -> None:
        try:
            element.set_property(property_name, value)
        except Exception:
            pass

    def _start_pipeline_threads(self) -> None:
        with self._pipeline_lock:
            if self._frame_feed_thread is not None and self._frame_feed_thread.is_alive():
                return

            self._stop_event.clear()
            self._frame_feed_thread = threading.Thread(
                target=self._feed_appsrc_loop,
                name="gst-appsrc-feed",
                daemon=True,
            )
            self._frame_feed_thread.start()

            self._replay_feed_thread = threading.Thread(
                target=self._feed_replay_appsrc_loop,
                name="gst-replay-feed",
                daemon=True,
            )
            self._replay_feed_thread.start()

            self._bus_thread = threading.Thread(
                target=self._monitor_bus_loop,
                name="gst-bus-watch",
                daemon=True,
            )
            self._bus_thread.start()

    def _feed_appsrc_loop(self) -> None:
        Gst = self._Gst
        assert Gst is not None

        while not self._stop_event.is_set():
            frame = self._source.read_frame()
            if frame is None:
                continue

            if self._appsrc is None:
                break

            frame_array = np.ascontiguousarray(frame.image_bgr)
            gst_buffer = Gst.Buffer.new_allocate(None, frame_array.nbytes, None)
            gst_buffer.fill(0, frame_array.tobytes())
            gst_buffer.offset = frame.frame_id
            if self._stream_start_timestamp is None:
                self._stream_start_timestamp = frame.timestamp
            running_timestamp = max(0.0, frame.timestamp - self._stream_start_timestamp)
            gst_buffer.pts = int(running_timestamp * Gst.SECOND)
            gst_buffer.dts = gst_buffer.pts
            gst_buffer.duration = self._frame_duration_ns

            with self._metadata_lock:
                self._frame_metadata[frame.frame_id] = _FrameMetadata(
                    timestamp=frame.timestamp,
                    source_name=frame.source_name,
                )
                while len(self._frame_metadata) > 4096:
                    self._frame_metadata.popitem(last=False)

            flow_return = self._appsrc.emit("push-buffer", gst_buffer)
            if flow_return != Gst.FlowReturn.OK and not self._stop_event.is_set():
                self._preview_output.show_placeholder_message(
                    f"GStreamer source push failed: {flow_return}"
                )
                break

    def _feed_replay_appsrc_loop(self) -> None:
        frame_interval_seconds = self._frame_duration_ns / 1_000_000_000 if self._frame_duration_ns > 0 else 0.0
        last_push_monotonic: float | None = None

        while not self._stop_event.is_set():
            if not self._replay_frame_ready.wait(timeout=0.1):
                continue
            if self._stop_event.is_set():
                break

            with self._replay_frame_lock:
                frame = self._replay_pending_frame
                self._replay_pending_frame = None
                self._replay_frame_ready.clear()

            if frame is None:
                continue

            if frame_interval_seconds > 0.0 and last_push_monotonic is not None:
                elapsed = time.perf_counter() - last_push_monotonic
                remaining = frame_interval_seconds - elapsed
                if remaining > 0.0 and self._stop_event.wait(remaining):
                    break

            with self._replay_frame_lock:
                if self._replay_pending_frame is not None:
                    frame = self._replay_pending_frame
                    self._replay_pending_frame = None
                    self._replay_frame_ready.clear()

            with self._pipeline_lock:
                if (
                    not self._replay_display_active
                    or self._replay_pipeline is None
                    or self._replay_appsrc is None
                ):
                    continue
                self._push_replay_frame_locked(frame)

            last_push_monotonic = time.perf_counter()

    def _monitor_bus_loop(self) -> None:
        Gst = self._Gst
        assert Gst is not None

        interesting_messages = Gst.MessageType.ERROR | Gst.MessageType.EOS
        while not self._stop_event.is_set():
            if self._poll_bus_for_messages(self._bus, interesting_messages, int(Gst.SECOND / 20)):
                break
            if self._poll_bus_for_messages(self._replay_bus, interesting_messages, 0):
                break

    def _poll_bus_for_messages(self, bus: Any, interesting_messages: Any, timeout_ns: int) -> bool:
        if bus is None:
            return False

        message = bus.timed_pop_filtered(timeout_ns, interesting_messages)
        if message is None:
            return False

        if message.type == self._Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            details = debug or str(error)
            self._preview_output.show_placeholder_message(f"GStreamer error: {details}")
            self._stop_event.set()
            return True

        if message.type == self._Gst.MessageType.EOS:
            self._preview_output.show_placeholder_message("GStreamer pipeline reached EOS.")
            self._stop_event.set()
            return True

        return False

    def _on_bus_sync_message(self, _bus: Any, message: Any) -> None:
        structure = message.get_structure()
        if structure is None or structure.get_name() != "prepare-window-handle":
            return
        if message.src == self._preview_sink and self._active_video_output == "live":
            with self._pipeline_lock:
                self._bind_video_sink_locked(message.src)
            return
        if message.src == self._replay_sink and self._active_video_output == "replay":
            with self._pipeline_lock:
                self._bind_video_sink_locked(message.src)
            return

    def _set_branch_enabled(self, branch_name: str, enabled: bool) -> None:
        valve = self._branch_valves.get(branch_name)
        if valve is not None:
            valve.set_property("drop", not enabled)

    def _bind_active_video_sink_locked(self, expose: bool = True) -> None:
        self._bind_video_sink_locked(self._get_active_video_sink_locked(), expose=expose)

    def _get_active_video_sink_locked(self) -> Any | None:
        if self._active_video_output == "replay":
            return self._replay_sink
        return self._preview_sink

    def _bind_video_sink_locked(self, sink: Any | None, expose: bool = True) -> None:
        if sink is None or self._video_window_handle is None:
            return

        try:
            sink.set_window_handle(self._video_window_handle)
        except Exception:
            GstVideo = self._GstVideo
            if GstVideo is None:
                return
            try:
                GstVideo.VideoOverlay.set_window_handle(sink, self._video_window_handle)
            except Exception:
                return

        if expose and hasattr(sink, "expose"):
            try:
                sink.expose()
            except Exception:
                GstVideo = self._GstVideo
                if GstVideo is None:
                    return
                try:
                    GstVideo.VideoOverlay.expose(sink)
                except Exception:
                    pass

    def _push_replay_frame_locked(self, frame: MediaFrame) -> None:
        assert self._Gst is not None
        assert self._replay_appsrc is not None

        frame_array = np.ascontiguousarray(frame.image_bgr)
        gst_buffer = self._Gst.Buffer.new_allocate(None, frame_array.nbytes, None)
        gst_buffer.fill(0, frame_array.tobytes())
        gst_buffer.offset = frame.frame_id
        gst_buffer.pts = self._replay_push_count * self._frame_duration_ns
        gst_buffer.dts = gst_buffer.pts
        gst_buffer.duration = self._frame_duration_ns
        self._replay_push_count += 1

        flow_return = self._replay_appsrc.emit("push-buffer", gst_buffer)
        if flow_return != self._Gst.FlowReturn.OK and not self._stop_event.is_set():
            self._preview_output.show_placeholder_message(
                f"Replay playback push failed: {flow_return}"
            )

    def _on_preview_buffer(self, _pad: Any, info: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK

        frame_id = int(buffer.offset) if buffer.offset != Gst.BUFFER_OFFSET_NONE else None
        metadata: _FrameMetadata | None
        with self._metadata_lock:
            metadata = self._frame_metadata.get(frame_id) if frame_id is not None else None

        timestamp = metadata.timestamp if metadata is not None else time.time()
        source_name = metadata.source_name if metadata is not None else self._source.get_display_name()

        if self._preview_running and self._live_sample_callback is not None:
            self._live_sample_callback(timestamp, source_name)

        return Gst.PadProbeReturn.OK

    def _on_preview_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        sample = sink.emit("pull-sample")
        frame = self._sample_to_media_frame(sample)
        if frame is not None and self._preview_running and self._frame_callback is not None:
            self._frame_callback(frame)
        return Gst.FlowReturn.OK

    def _on_record_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        sample = sink.emit("pull-sample")
        frame = self._sample_to_media_frame(sample)
        if frame is not None and self._recording_running:
            self._recorder.write_frame(frame)
        return Gst.FlowReturn.OK

    def _on_replay_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        sample = sink.emit("pull-sample")
        frame = self._sample_to_media_frame(sample)
        if frame is not None and self._replay_running:
            self._replay_buffer.append_frame(frame)
        return Gst.FlowReturn.OK

    def _sample_to_media_frame(self, sample: Any) -> MediaFrame | None:
        if sample is None:
            return None

        Gst = self._Gst
        assert Gst is not None

        buffer = sample.get_buffer()
        caps = sample.get_caps()
        if buffer is None or caps is None or caps.get_size() == 0:
            return None

        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return None

        try:
            frame_array = np.frombuffer(map_info.data, dtype=np.uint8).reshape((height, width, 3)).copy()
        finally:
            buffer.unmap(map_info)

        frame_id = int(buffer.offset) if buffer.offset != Gst.BUFFER_OFFSET_NONE else 0
        with self._metadata_lock:
            metadata = self._frame_metadata.get(frame_id)

        timestamp = metadata.timestamp if metadata is not None else time.time()
        source_name = metadata.source_name if metadata is not None else self._source.get_display_name()

        return MediaFrame(
            frame_id=frame_id,
            timestamp=timestamp,
            image=frame_array,
            source_name=source_name,
        )

    def _teardown_pipeline(self) -> None:
        with self._pipeline_lock:
            if self._pipeline is None:
                if self._replay_pipeline is not None:
                    self._teardown_replay_pipeline_locked()
                return

            Gst = self._Gst
            assert Gst is not None

            tee = self._pipeline.get_by_name("source_tee")
            self._pipeline.set_state(Gst.State.NULL)

            if self._preview_probe_pad is not None and self._preview_probe_id is not None:
                self._preview_probe_pad.remove_probe(self._preview_probe_id)

            self._teardown_replay_pipeline_locked()

            if tee is not None:
                for request_pad in self._tee_request_pads:
                    tee.release_request_pad(request_pad)

            self._tee_request_pads.clear()
            self._branch_valves.clear()
            self._preview_sink = None
            self._preview_sink_factory_name = None
            self._preview_probe_pad = None
            self._preview_probe_id = None
            self._pipeline = None
            self._appsrc = None
            self._bus = None
            self._stream_start_timestamp = None
            with self._metadata_lock:
                self._frame_metadata.clear()

    def _teardown_replay_pipeline_locked(self) -> None:
        if self._replay_pipeline is None:
            return

        self._replay_pipeline.set_state(self._Gst.State.NULL)
        self._replay_pipeline = None
        self._replay_appsrc = None
        self._replay_bus = None
        self._replay_sink = None
        self._replay_sink_factory_name = None
        self._replay_push_count = 0
        self._replay_display_active = False
        with self._replay_frame_lock:
            self._replay_pending_frame = None
        self._replay_frame_ready.clear()
