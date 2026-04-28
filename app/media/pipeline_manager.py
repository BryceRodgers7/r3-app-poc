"""GStreamer-centered media graph orchestration for the replay application."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
import importlib
import logging
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np

from app.core.feed_state import FeedState
from app.core.models import AudioChunk, AudioFormat, FrameOverlayInfo, IngestTelemetry, MediaFrame, SessionPaths
from app.core.state_machine import StateMachine
from app.core.telemetry import FeedMetrics
from app.media.frame_overlay import render_frame_overlay
from app.media.gst_bus_log import log_bus_message
from app.media.source_interface import PipelineMode
from app.media.preview_output import PreviewOutput
from app.media.recorder import Recorder
from app.media.replay_buffer import ReplayBuffer, ReplayFrameRef, ReplayStore
from app.media.source_interface import SourceInterface

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _FrameMetadata:
    """Tracks source-side metadata while buffers fan out through GStreamer."""

    frame_overlay: FrameOverlayInfo


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
        replay_buffer: ReplayStore,
        audio_enabled: bool = True,
        live_audio_monitor_enabled: bool = True,
        recording_enabled: bool = True,
        recording_segment_duration_seconds: float = 4.0,
        recording_codec: str = "mjpeg",
        recording_container: str = "mkv",
    ) -> None:
        self._source = source
        self._preview_output = preview_output
        self._recorder = recorder
        self._replay_buffer = replay_buffer
        self._audio_enabled = audio_enabled
        self._live_audio_monitor_enabled = live_audio_monitor_enabled
        self._recording_enabled = recording_enabled
        self._preview_running = False
        self._recording_running = False
        self._replay_running = False
        self._frame_callback: Callable[[MediaFrame], None] | None = None
        self._feed_metrics: FeedMetrics | None = None
        self._feed_state: StateMachine[FeedState] | None = None

        self._Gst: Any | None = None
        self._GstVideo: Any | None = None
        self._pipeline: Any | None = None
        self._appsrc: Any | None = None
        self._audio_appsrc: Any | None = None
        self._native_audio_src_pad: Any | None = None
        self._bus: Any | None = None
        self._replay_pipeline: Any | None = None
        self._replay_source: Any | None = None
        self._replay_bus: Any | None = None
        self._replay_audio_pipeline: Any | None = None
        self._replay_audio_source: Any | None = None
        self._replay_audio_bus: Any | None = None
        self._tee_request_pads: list[Any] = []
        self._audio_tee_request_pads: list[Any] = []
        self._branch_valves: dict[str, Any] = {}
        self._audio_branch_valves: dict[str, Any] = {}
        self._preview_sink: Any | None = None
        self._preview_sink_factory_name: str | None = None
        self._operator_preview_sink: Any | None = None
        self._program_preview_sink: Any | None = None
        self._operator_window_handle: int | None = None
        self._program_window_handle: int | None = None
        self._replay_sink: Any | None = None
        self._replay_sink_factory_name: str | None = None
        self._preview_probe_pad: Any | None = None
        self._preview_probe_id: int | None = None
        self._video_window_handle: int | None = None
        self._active_video_output = "live"

        self._frame_feed_thread: threading.Thread | None = None
        self._audio_feed_thread: threading.Thread | None = None
        self._bus_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pipeline_lock = threading.Lock()

        self._frame_duration_ns = 0
        self._audio_format: AudioFormat | None = None
        self._audio_stream_start_timestamp: float | None = None
        self._stream_start_timestamp: float | None = None
        self._frame_metadata: OrderedDict[int, _FrameMetadata] = OrderedDict()
        self._metadata_lock = threading.Lock()
        self._live_sample_callback: Callable[[FrameOverlayInfo], None] | None = None
        # Phase 4.A: native segmented recording state.
        self._splitmuxsink: Any | None = None
        self._recording_session_paths: SessionPaths | None = None
        self._recording_feed_id: str | None = None
        # Per-recording-session segment counter. Reset to 0 each time the
        # operator clicks Start, so segments land as
        # segment_00000.mkv, segment_00001.mkv, …. Splitmuxsink's own
        # `fragment_id` parameter is unreliable for this — it increments
        # during startup state transitions before any buffer is written.
        self._recording_segment_counter: int = 0
        self._recording_segment_duration_seconds: float = max(
            0.5, float(recording_segment_duration_seconds)
        )
        self._recording_codec: str = recording_codec.strip().lower() or "mjpeg"
        self._recording_container: str = recording_container.strip().lower() or "mkv"
        if self._recording_codec != "mjpeg" or self._recording_container != "mkv":
            raise RuntimeError(
                f"Phase 4.A only supports recording codec='mjpeg' container='mkv'. "
                f"Got codec={self._recording_codec!r} container={self._recording_container!r}. "
                f"ProRes/DNxHR support is deferred."
            )

    def describe_architecture(self) -> str:
        """Describe the current transitional tee/fan-out architecture."""
        return (
            "SourceInterface.read_frame -> video appsrc -> videoconvert -> tee -> "
            "[preview embedded video sink, recording appsink, replay appsink] + "
            "SourceInterface.read_audio_chunk -> audio appsrc -> tee -> "
            "[live audio sink, recording appsink, replay appsink] + "
            "rolling replay store -> native replay source -> embedded replay sink"
        )

    def start_preview(self) -> None:
        """Start the preview branch without affecting recording or replay buffering."""
        self._preview_running = True
        self._preview_output.show_placeholder_message("Starting live preview...")
        if self._active_video_output == "live":
            self._set_branch_enabled("preview", True)

    def enable_file_recording(self, session_paths: SessionPaths, feed_id: str | None = None) -> None:
        """Open the record branch's valve so segmented recording starts (Phase 4.A).

        The actual writing is done by `splitmuxsink` downstream of the
        valve; we just need to let buffers through and tell the
        `format-location` callback which session/feed to write under. The
        legacy `Recorder` / `MuxedMediaWriter` path is bypassed entirely
        — they remain in the codebase only until 4.D's cleanup.
        """
        if self._splitmuxsink is None:
            LOGGER.warning(
                "enable_file_recording called but recording branch is not "
                "configured; ignoring."
            )
            return
        self._recording_session_paths = session_paths
        self._recording_feed_id = feed_id or self._source.get_feed_id()
        # Reset our private segment counter so each recording session
        # starts at `segment_00000.mkv` regardless of splitmuxsink's
        # internal fragment_id (which may already be non-zero from
        # startup state transitions).
        self._recording_segment_counter = 0
        feed_paths = session_paths.get_feed_paths(self._recording_feed_id)
        feed_paths.recording_dir.mkdir(parents=True, exist_ok=True)
        self._set_branch_enabled("record", True)
        self._recording_running = True
        LOGGER.info(
            "Recording started for feed_id=%s, segments under %s",
            self._recording_feed_id,
            feed_paths.recording_dir,
        )

    def disable_file_recording(self) -> None:
        """Close the record branch's valve so segmented recording stops (Phase 4.A).

        The current splitmuxsink segment may be slightly truncated (no
        explicit EOS); 4.E's startup scan quarantines or repairs incomplete
        segments. The valve stays closed until the operator clicks Start
        again, at which point splitmuxsink continues with the next
        sequential fragment id under the same session/feed.
        """
        self._recording_running = False
        self._set_branch_enabled("record", False)
        LOGGER.info(
            "Recording stopped for feed_id=%s",
            self._recording_feed_id,
        )

    def start_replay_buffer(self, session_paths: SessionPaths, feed_id: str | None = None) -> None:
        # temporary disable for diagnostic purposes
        return
        """Start the rolling replay buffer branch."""
        self._replay_buffer.start(session_paths, feed_id=feed_id or self._source.get_feed_id())
        self._replay_running = True
        self._configure_replay_source()
        self._set_branch_enabled("replay", True)
        LOGGER.info("Replay capture started with rolling storage in %s", session_paths.rolling_dir)

    def stop_preview(self) -> None:
        """Stop only the preview branch."""
        self._preview_running = False
        self._set_branch_enabled("preview", False)

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

        if self._frame_feed_thread is not None:
            self._frame_feed_thread.join(timeout=2.0)
            self._frame_feed_thread = None

        if self._audio_feed_thread is not None:
            self._audio_feed_thread.join(timeout=2.0)
            self._audio_feed_thread = None

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
            # Keep the record branch draining the tee even when not writing to disk; closing
            # it can block preroll and stall preview/replay on multi-branch pipelines.
            self._set_branch_enabled("record", True)
        except Exception:
            self._teardown_pipeline()
            self._source.disconnect_source()
            raise
        return True

    def set_frame_callback(self, callback: Callable[[MediaFrame], None]) -> None:
        """Register the legacy preview-frame callback."""
        self._frame_callback = callback

    def set_live_sample_callback(self, callback: Callable[[FrameOverlayInfo], None]) -> None:
        """Register the controller callback for live-preview frame metadata."""
        self._live_sample_callback = callback

    def set_feed_metrics(self, metrics: FeedMetrics | None) -> None:
        """Attach a `FeedMetrics` instance for per-branch FPS counting."""
        self._feed_metrics = metrics

    def set_feed_state(self, state_machine: StateMachine[FeedState] | None) -> None:
        """Attach the per-feed state machine driven by bus events."""
        self._feed_state = state_machine

    def set_video_window_handle(self, window_handle: int) -> None:
        """Attach the active embedded video sink to a Qt-owned native child window."""
        self._video_window_handle = int(window_handle)
        with self._pipeline_lock:
            self._bind_active_video_sink_locked()

    def set_preview_window_handle(self, window_handle: int) -> None:
        """Backward-compatible wrapper for the shared video surface handle."""
        self.set_video_window_handle(window_handle)

    def set_native_preview_window_handle(self, role: str, window_handle: int) -> None:
        """Bind one of the per-window native preview sinks to a Qt window handle.

        `role` must be `"operator"` or `"program"`. No-op for python_push
        sources (their preview path uses an appsink and renders via QImage).
        """
        if role not in {"operator", "program"}:
            raise ValueError(f"Unknown native preview window role: {role!r}")
        with self._pipeline_lock:
            if role == "operator":
                self._operator_window_handle = int(window_handle)
                sink = self._operator_preview_sink
            else:
                self._program_window_handle = int(window_handle)
                sink = self._program_preview_sink
            if sink is None:
                # Pipeline not built yet (or not native) — handle stored
                # for later; the binding happens once native preview
                # branches exist. For native sources the binding is also
                # performed at the end of `_build_pipeline`.
                return
            self._bind_named_video_sink_locked(sink, int(window_handle))

    def _bind_named_video_sink_locked(self, sink: Any, window_handle: int) -> None:
        """Bind one specific sink to a window handle via GstVideoOverlay."""
        if sink is None or window_handle is None:
            return
        try:
            sink.set_window_handle(window_handle)
            return
        except Exception:
            pass
        GstVideo = self._GstVideo
        if GstVideo is None:
            return
        try:
            GstVideo.VideoOverlay.set_window_handle(sink, window_handle)
        except Exception:
            LOGGER.exception("Failed to bind video sink to window handle")

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
                self._set_branch_enabled("preview", self._preview_running)
                self._bind_active_video_sink_locked()
                return

            self._active_video_output = "live"
            self._set_branch_enabled("preview", self._preview_running)
            if self._replay_pipeline is not None:
                self._replay_pipeline.set_state(self._Gst.State.PAUSED)
            self._bind_active_video_sink_locked()
        LOGGER.info("Activated live preview output")

    def activate_replay_output(self, frame_ref: ReplayFrameRef, paused: bool) -> None:
        """Display replay output starting at the requested replay frame."""
        with self._pipeline_lock:
            if self._replay_pipeline is None or self._replay_source is None:
                raise RuntimeError("Replay playback pipeline is not available.")
            segment = self._replay_buffer.get_media_segment_at_or_before(frame_ref.timestamp)
            if segment is None:
                raise RuntimeError("Replay storage is not ready for native playback.")

            self._active_video_output = "replay"
            self._set_branch_enabled("preview", False)
            self._configure_replay_source_locked(
                media_path=segment.media_path,
                segment_start_timestamp=segment.start_timestamp,
                playback_timestamp=frame_ref.timestamp,
                paused=paused,
            )
            self._bind_active_video_sink_locked()
        LOGGER.info(
            "Activated replay output at sequence=%s timestamp=%.3f paused=%s",
            frame_ref.sequence_index,
            frame_ref.timestamp,
            paused,
        )

    def get_source_name(self) -> str:
        """Return the current source display name."""
        return self._source.get_display_name()

    def get_source_feed_id(self) -> str:
        """Return the current source feed identifier."""
        return self._source.get_feed_id()

    def get_source_status_message(self) -> str | None:
        """Return the current non-fatal source status message."""
        return self._source.get_status_message()

    def get_ingest_telemetry(self) -> IngestTelemetry | None:
        """Return raw vs target ingest resolution/FPS from the live source."""
        return self._source.get_ingest_telemetry()

    def _configure_replay_source(self) -> None:
        """Replay source is selected per requested timestamp for muxed segments."""
        return

    def _configure_replay_source_locked(
        self,
        media_path: Any,
        segment_start_timestamp: float,
        playback_timestamp: float,
        paused: bool,
    ) -> None:
        if self._replay_pipeline is None or self._replay_source is None:
            return

        self._replay_pipeline.set_state(self._Gst.State.READY)
        uri = Path(media_path).resolve().as_uri()
        self._set_property_if_supported(self._replay_source, "uri", uri)
        target_state = self._Gst.State.PAUSED if paused else self._Gst.State.PLAYING
        self._replay_pipeline.set_state(target_state)

        offset_seconds = max(0.0, playback_timestamp - segment_start_timestamp)
        if offset_seconds > 0.0:
            try:
                self._replay_pipeline.seek_simple(
                    self._Gst.Format.TIME,
                    self._Gst.SeekFlags.FLUSH | self._Gst.SeekFlags.KEY_UNIT,
                    int(offset_seconds * self._Gst.SECOND),
                )
            except Exception:
                LOGGER.debug("Replay segment seek failed for %s", media_path, exc_info=True)
        LOGGER.info(
            "Replay source configured for %s target_state=%s",
            media_path,
            target_state.value_nick if hasattr(target_state, "value_nick") else target_state,
        )

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
            tee = self._make_element("tee", "source_tee")
            pipeline.add(tee)

            if self._source.pipeline_mode == PipelineMode.NATIVE:
                chain = self._source.build_native_chain(Gst)
                if chain is None:
                    raise RuntimeError(
                        f"Native source {self._source.get_feed_id()!r} did not "
                        f"build an element chain; check `connect_source()` "
                        f"succeeded and required plugins are installed."
                    )
                # Add each native source element to the parent pipeline,
                # then run the source's static-link step now that they share
                # a parent.
                for element in chain["elements"]:
                    pipeline.add(element)
                if not self._source.link_native_chain_static():
                    raise RuntimeError(
                        "Native source failed to link its static element chain."
                    )
                video_src = chain["video_src_pad"]
                tee_sink = tee.get_static_pad("sink")
                if (
                    video_src is None
                    or tee_sink is None
                    or video_src.link(tee_sink) != Gst.PadLinkReturn.OK
                ):
                    raise RuntimeError(
                        "Failed to link the native video src pad into the video tee."
                    )
                self._native_audio_src_pad = chain["audio_src_pad"]
                self._appsrc = None
            else:
                appsrc = self._make_element("appsrc", "source_appsrc")
                source_convert = self._make_element("videoconvert", "source_convert")
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
                if not appsrc.link(source_convert) or not source_convert.link(tee):
                    raise RuntimeError("Failed to link the GStreamer source path.")
                self._native_source_bin = None
                self._appsrc = appsrc

            self._pipeline = pipeline
            self._bus = pipeline.get_bus()
            if self._bus is not None:
                self._bus.enable_sync_message_emission()
                self._bus.connect("sync-message::element", self._on_bus_sync_message)
            self._tee_request_pads.clear()
            self._audio_tee_request_pads.clear()
            self._branch_valves.clear()
            self._audio_branch_valves.clear()
            self._preview_sink = None
            self._preview_sink_factory_name = None
            self._replay_sink = None
            self._replay_sink_factory_name = None
            self._preview_probe_pad = None
            self._preview_probe_id = None
            self._active_video_output = "live"

            self._add_branch("preview", self._on_preview_sample)
            self._add_branch("record", self._on_record_sample)
            # self._add_branch("replay", self._on_replay_sample)
            self._build_audio_path_locked()
            self._build_replay_pipeline(width=width, height=height, fps_fraction=fps_fraction)
            self._build_replay_audio_pipeline()

            # If window handles were registered before the pipeline was
            # built, bind them to the freshly-created native preview sinks.
            if self._operator_preview_sink is not None and self._operator_window_handle is not None:
                self._bind_named_video_sink_locked(
                    self._operator_preview_sink, self._operator_window_handle
                )
            if self._program_preview_sink is not None and self._program_window_handle is not None:
                self._bind_named_video_sink_locked(
                    self._program_preview_sink, self._program_window_handle
                )

            state_change = pipeline.set_state(Gst.State.PLAYING)
            if state_change == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Failed to move the GStreamer pipeline to PLAYING.")
            if self._replay_pipeline is not None:
                replay_state_change = self._replay_pipeline.set_state(Gst.State.READY)
                if replay_state_change == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Failed to move the replay playback pipeline to READY.")
            if self._replay_audio_pipeline is not None:
                replay_audio_state_change = self._replay_audio_pipeline.set_state(Gst.State.READY)
                if replay_audio_state_change == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Failed to move the replay audio pipeline to READY.")

    def _add_branch(self, branch_name: str, sample_handler: Callable[[Any], Any]) -> None:
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        if branch_name == "preview":
            self._add_preview_branch(branch_name)
            return
        if branch_name == "record":
            self._add_record_branch_via_splitmuxsink(branch_name)
            return

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        sink = self._make_element("appsink", f"{branch_name}_sink")

        valve.set_property("drop", True)
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("async", False)
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
        # 3.A.3 attempted to use d3d11videosink + per-window handle binding
        # for native sources. On Windows + PySide6 + gst-plugins-bad the
        # binding was unreliable: a third "Direct3D11 renderer" top-level
        # window kept appearing despite preemptive `set_window_handle()`
        # calls and a `prepare-window-handle` sync-bus-message handler, and
        # the preview branch's tee fan-out froze within seconds. Reverted
        # to the python_push appsink path until a successful native-preview
        # approach is found (see Phase 3 plan in
        # `docs/r3_app_architecture.md`). The native preview branch
        # (`_add_native_preview_branch`) and surrounding contracts
        # (`set_render_mode`, `bind_native_preview_window_handle`,
        # `set_native_preview_window_handle`, the
        # `_promote_feed_state_on_arrival` helper, the `pipeline_mode`
        # diagnostics) remain in place so the next attempt can flip this
        # dispatch back without redoing the wiring.
        self._add_python_push_preview_branch(branch_name)

    def _add_python_push_preview_branch(self, branch_name: str) -> None:
        """Legacy preview branch used by python_push (synthetic) sources.

        Frames flow `tee → queue → valve → videoconvert → appsink`; the
        appsink emits `new-sample` and `_on_preview_sample` reshapes the
        buffer into a NumPy `MediaFrame` for QImage rendering.
        """
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        sink = self._make_element("appsink", f"{branch_name}_sink")

        valve.set_property("drop", True)
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        # async=False prevents this sink from gating the pipeline's
        # PAUSED→PLAYING transition on preroll.
        sink.set_property("async", False)
        sink.set_property("drop", True)
        sink.set_property("max-buffers", 2)
        sink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGR"))
        sink.connect("new-sample", self._on_preview_sample)

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
        self._preview_sink = sink
        self._preview_sink_factory_name = "appsink-preview"

    def _add_native_preview_branch(self, branch_name: str) -> None:
        """Native preview branch (slice 3.A.3).

        Frames flow `tee → queue → valve → videoconvert → preview_tee →
        [operator d3d11videosink, program d3d11videosink]`. There is no
        appsink on the preview path — frames never enter Python on the hot
        path. A buffer pad probe on `videoconvert.src` ticks per-feed
        metrics and synthesizes a `FrameOverlayInfo` so the operator's
        `PlaybackController` state machine still sees frame-arrival events.
        """
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        preview_tee = self._make_element("tee", f"{branch_name}_window_tee")
        operator_sink, operator_factory = self._make_video_sink(
            f"{branch_name}_operator_sink"
        )
        program_sink, program_factory = self._make_video_sink(
            f"{branch_name}_program_sink"
        )

        valve.set_property("drop", True)
        for sink in (operator_sink, program_sink):
            self._set_property_if_supported(sink, "sync", False)
            self._set_property_if_supported(sink, "async", False)
            self._set_property_if_supported(sink, "force-aspect-ratio", True)

        for element in (queue, valve, convert, preview_tee, operator_sink, program_sink):
            self._pipeline.add(element)

        if not (queue.link(valve) and valve.link(convert) and convert.link(preview_tee)):
            raise RuntimeError(f"Failed to link the {branch_name} branch head.")

        # Each window's sink consumes its own request-pad off the per-branch tee.
        for sink, label in (
            (operator_sink, "operator"),
            (program_sink, "program"),
        ):
            sink_queue = self._make_element("queue", f"{branch_name}_{label}_queue")
            self._pipeline.add(sink_queue)
            if not sink_queue.link(sink):
                raise RuntimeError(
                    f"Failed to link the {label} preview queue to its video sink."
                )
            tee_pad = preview_tee.request_pad_simple("src_%u")
            if tee_pad is None:
                tee_pad = preview_tee.get_request_pad("src_%u")
            sink_queue_sink_pad = sink_queue.get_static_pad("sink")
            if (
                tee_pad is None
                or sink_queue_sink_pad is None
                or tee_pad.link(sink_queue_sink_pad) != Gst.PadLinkReturn.OK
            ):
                raise RuntimeError(
                    f"Failed to fan {label} preview branch off the per-branch tee."
                )
            sink_queue.sync_state_with_parent()
            sink.sync_state_with_parent()

        # Link the source tee → branch head.
        source_tee_pad = self._request_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if source_tee_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(
                f"Failed to link source tee output to the {branch_name} branch."
            )

        queue.sync_state_with_parent()
        valve.sync_state_with_parent()
        convert.sync_state_with_parent()
        preview_tee.sync_state_with_parent()

        # Buffer probe for metrics + state tracking. Runs on the streaming
        # thread but does no pixel-data work — just a pts read and metric
        # tick — so it doesn't block the GIL.
        convert_src_pad = convert.get_static_pad("src")
        if convert_src_pad is not None:
            convert_src_pad.add_probe(
                Gst.PadProbeType.BUFFER,
                self._on_native_preview_buffer_probe,
                None,
            )

        self._branch_valves[branch_name] = valve
        # `_preview_sink` is the operator-window sink — that's the one the
        # legacy `_video_window_handle` setter binds to, mirroring the
        # replay-sink convention.
        self._preview_sink = operator_sink
        self._preview_sink_factory_name = operator_factory
        self._operator_preview_sink = operator_sink
        self._program_preview_sink = program_sink

    def _on_native_preview_buffer_probe(self, _pad: Any, info: Any, _user: Any) -> Any:
        """Buffer probe for native preview: ticks metrics + frame-overlay state.

        Runs on the GStreamer streaming thread for every buffer that crosses
        videoconvert.src. Deliberately does NO pixel-data work — pulls only
        the buffer's PTS and feeds the existing `_live_sample_callback` so
        the operator's `PlaybackController` keeps its state machine in sync.
        """
        Gst = self._Gst
        if Gst is None:
            return Gst.PadProbeReturn.OK if Gst is not None else 0
        if self._feed_metrics is not None:
            self._feed_metrics.tick_source()
            if self._preview_running:
                self._feed_metrics.tick_preview()
        if self._preview_running and self._live_sample_callback is not None:
            buffer = info.get_buffer()
            pts_seconds: float | None = None
            frame_id: int | None = None
            if buffer is not None:
                if buffer.pts != Gst.CLOCK_TIME_NONE:
                    pts_seconds = float(buffer.pts) / float(Gst.SECOND)
                if buffer.offset != Gst.BUFFER_OFFSET_NONE:
                    frame_id = int(buffer.offset)
            overlay = FrameOverlayInfo(
                feed_id=self._source.get_feed_id(),
                source_name=self._source.get_display_name(),
                frame_id=frame_id,
                capture_timestamp=pts_seconds if pts_seconds is not None else time.time(),
            )
            try:
                self._live_sample_callback(overlay)
            except Exception:
                LOGGER.exception("native preview live_sample_callback raised")
        return Gst.PadProbeReturn.OK

    def _add_record_branch_via_splitmuxsink(self, branch_name: str) -> None:
        """Native segmented recording branch (Phase 4.A).

        Replaces the old `appsink → MuxedMediaWriter` path. Element shape::

            tee → queue → valve → videoconvert → jpegenc → splitmuxsink

        `splitmuxsink` writes one MKV file per segment under
        `<session>/recording/<feed_id>/segment_NNNNN.mkv`. Filename is
        chosen by the `format-location` callback so we can interpolate
        session paths and feed ids that aren't known until the operator
        clicks "Start game recording". The valve stays closed (drop=True)
        until `enable_file_recording` opens it; closing it again on
        `disable_file_recording` stops feeding the muxer (the current
        segment's tail may be slightly truncated, acceptable per §6.5
        — slice 4.E quarantines/recovers incomplete segments).

        A buffer probe on `videoconvert.src` ticks `tick_recording`
        whenever frames are flowing, so the diagnostics widget continues
        to show recording activity.
        """
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        # Force the input to jpegenc to be BT.601 I420. JPEG has no
        # standardized colorimetry tag; players decode assuming BT.601.
        # If we let the BT.709 source flow straight to jpegenc, players
        # will use the wrong matrix to convert YUV→RGB and colors will
        # shift visibly. Pinning caps here pushes videoconvert to do the
        # 709→601 conversion before encoding, so the JPEG content
        # actually matches what decoders expect.
        encode_caps = self._make_element("capsfilter", f"{branch_name}_encode_caps")
        encode_caps.set_property(
            "caps",
            Gst.Caps.from_string(
                "video/x-raw,format=I420,colorimetry=bt601"
            ),
        )
        jpegenc = self._make_element("jpegenc", f"{branch_name}_jpegenc")
        splitmuxsink = self._make_element("splitmuxsink", f"{branch_name}_splitmuxsink")

        valve.set_property("drop", True)
        # `quality` is jpegenc's default 85; can be exposed as a setting later.
        max_size_time_ns = int(self._recording_segment_duration_seconds * Gst.SECOND)
        splitmuxsink.set_property("max-size-time", max_size_time_ns)
        splitmuxsink.set_property("muxer-factory", "matroskamux")
        # `async-finalize=True` finalizes closed segments on a worker thread
        # so the streaming thread never blocks on disk flush.
        self._set_property_if_supported(splitmuxsink, "async-finalize", True)
        splitmuxsink.connect("format-location", self._on_splitmuxsink_format_location)

        for element in (queue, valve, convert, encode_caps, jpegenc, splitmuxsink):
            self._pipeline.add(element)

        if not (
            queue.link(valve)
            and valve.link(convert)
            and convert.link(encode_caps)
            and encode_caps.link(jpegenc)
            and jpegenc.link(splitmuxsink)
        ):
            raise RuntimeError(f"Failed to link the {branch_name} branch.")

        tee_src_pad = self._request_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if tee_src_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(
                f"Failed to link tee output to the {branch_name} branch."
            )

        # Buffer probe for the recording-fps metric. Runs on the streaming
        # thread; ticks a counter and returns. No pixel-data work.
        convert_src_pad = convert.get_static_pad("src")
        if convert_src_pad is not None:
            convert_src_pad.add_probe(
                Gst.PadProbeType.BUFFER,
                self._on_record_branch_buffer_probe,
                None,
            )

        for element in (queue, valve, convert, encode_caps, jpegenc, splitmuxsink):
            element.sync_state_with_parent()

        self._branch_valves[branch_name] = valve
        self._splitmuxsink = splitmuxsink

    def _on_splitmuxsink_format_location(self, _splitmuxsink: Any, fragment_id: int) -> str:
        """Pick a per-segment filename based on the active recording session.

        Uses our own `_recording_segment_counter` rather than splitmuxsink's
        `fragment_id` argument — splitmuxsink's counter increments during
        pipeline startup events (caps propagation, muxer reset, state
        changes) before any buffer is actually written, which would make
        the first real segment land as `segment_00003.mkv` or similar.
        Resetting our counter on each `enable_file_recording` call keeps
        filenames predictable: every new recording session starts at
        `segment_00000.mkv`.

        If the callback fires without an active session (defensive — the
        upstream valve should be closed in that case), fall back to a temp
        path so the caller doesn't crash.
        """
        if self._recording_session_paths is None or self._recording_feed_id is None:
            tmp = Path(f"./_unrouted_segment_{fragment_id:05d}.mkv").resolve()
            LOGGER.warning(
                "splitmuxsink requested format-location without an active "
                "recording session; writing to %s",
                tmp,
            )
            return str(tmp)
        feed_paths = self._recording_session_paths.get_feed_paths(self._recording_feed_id)
        feed_paths.recording_dir.mkdir(parents=True, exist_ok=True)
        index = self._recording_segment_counter
        self._recording_segment_counter += 1
        path = feed_paths.recording_dir / f"segment_{index:05d}.mkv"
        LOGGER.info(
            "splitmuxsink opening segment index=%d (gst fragment_id=%d) "
            "feed_id=%s path=%s",
            index,
            fragment_id,
            self._recording_feed_id,
            path,
        )
        return str(path)

    def _on_record_branch_buffer_probe(self, _pad: Any, _info: Any, _user: Any) -> Any:
        """Tick the recording-fps metric when the operator is actively recording."""
        Gst = self._Gst
        if Gst is None:
            return 0  # Gst.PadProbeReturn.OK
        if self._recording_running and self._feed_metrics is not None:
            self._feed_metrics.tick_recording()
        return Gst.PadProbeReturn.OK

    def _build_audio_path_locked(self) -> None:
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        audio_format = self._source.get_audio_format()
        if not self._audio_enabled or not self._source.supports_embedded_audio() or audio_format is None:
            self._audio_format = None
            self._audio_appsrc = None
            return

        self._audio_format = audio_format
        tee = self._make_element("tee", "audio_tee")
        self._pipeline.add(tee)

        if self._source.pipeline_mode == PipelineMode.NATIVE:
            audio_src = self._native_audio_src_pad
            audio_tee_sink = tee.get_static_pad("sink")
            if (
                audio_src is None
                or audio_tee_sink is None
                or audio_src.link(audio_tee_sink) != Gst.PadLinkReturn.OK
            ):
                raise RuntimeError(
                    "Failed to link the native audio src pad into the audio tee."
                )
            self._audio_appsrc = None
        else:
            appsrc = self._make_element("appsrc", "audio_appsrc")
            convert = self._make_element("audioconvert", "audio_convert")
            resample = self._make_element("audioresample", "audio_resample")

            appsrc.set_property("is-live", True)
            appsrc.set_property("format", Gst.Format.TIME)
            appsrc.set_property("block", True)
            appsrc.set_property("do-timestamp", False)
            appsrc.set_property(
                "caps",
                Gst.Caps.from_string(
                    "audio/x-raw,"
                    f"format={audio_format.sample_format},"
                    f"rate={audio_format.sample_rate},"
                    f"channels={audio_format.channels},"
                    "layout=interleaved"
                ),
            )

            self._pipeline.add(appsrc)
            self._pipeline.add(convert)
            self._pipeline.add(resample)
            if not appsrc.link(convert) or not convert.link(resample) or not resample.link(tee):
                raise RuntimeError("Failed to link the GStreamer audio source path.")
            self._audio_appsrc = appsrc

        self._add_live_audio_branch()
        # Audio recording (audio muxed into the splitmuxsink segments) was
        # attempted as 4.A.bis but caused the video pipeline to freeze on
        # this hardware. Reverted; audio recording is fully deferred to a
        # later slice with its own scoping. For now the audio record
        # branch drains via a no-op appsink and the segment files are
        # video-only.
        self._add_audio_appsink_branch("record", self._on_record_audio_sample)
        self._add_audio_appsink_branch("replay", self._on_replay_audio_sample)

    def _add_live_audio_branch(self) -> None:
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", "audio_preview_queue")
        valve = self._make_element("valve", "audio_preview_valve")
        convert = self._make_element("audioconvert", "audio_preview_convert")
        resample = self._make_element("audioresample", "audio_preview_resample")
        sink, _sink_name = self._make_audio_sink("audio_preview_sink")

        valve.set_property("drop", True)
        self._set_property_if_supported(sink, "sync", False)
        # async=False: don't gate the parent pipeline's PAUSED→PLAYING
        # transition on this audio sink prerolling. With NDI sources that
        # have no audio (e.g. Screen Capture), the audio chain has no
        # upstream data and would otherwise hold the pipeline in PAUSED
        # forever.
        self._set_property_if_supported(sink, "async", False)

        self._pipeline.add(queue)
        self._pipeline.add(valve)
        self._pipeline.add(convert)
        self._pipeline.add(resample)
        self._pipeline.add(sink)
        if not queue.link(valve) or not valve.link(convert) or not convert.link(resample) or not resample.link(sink):
            raise RuntimeError("Failed to link the live audio branch.")

        tee_src_pad = self._request_audio_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if tee_src_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link audio tee output to the live audio branch.")

        for element in (queue, valve, convert, resample, sink):
            element.sync_state_with_parent()
        self._audio_branch_valves["preview"] = valve

    def _add_audio_appsink_branch(self, branch_name: str, sample_handler: Callable[[Any], Any]) -> None:
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", f"audio_{branch_name}_queue")
        valve = self._make_element("valve", f"audio_{branch_name}_valve")
        sink = self._make_element("appsink", f"audio_{branch_name}_sink")

        valve.set_property("drop", True)
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("max-buffers", 32)
        sink.connect("new-sample", sample_handler)

        self._pipeline.add(queue)
        self._pipeline.add(valve)
        self._pipeline.add(sink)
        if not queue.link(valve) or not valve.link(sink):
            raise RuntimeError(f"Failed to link the {branch_name} audio branch.")

        tee_src_pad = self._request_audio_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if tee_src_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"Failed to link audio tee output to the {branch_name} branch.")

        queue.sync_state_with_parent()
        valve.sync_state_with_parent()
        sink.sync_state_with_parent()
        self._audio_branch_valves[branch_name] = valve

    def _build_replay_pipeline(self, width: int, height: int, fps_fraction: Fraction) -> None:
        assert self._Gst is not None

        replay_pipeline = self._Gst.Pipeline.new("sports-replay-display-pipeline")
        replay_source = self._make_element("uridecodebin", "replay_source")
        replay_video_queue = self._make_element("queue", "replay_video_queue")
        replay_convert = self._make_element("videoconvert", "replay_convert")
        replay_audio_queue = self._make_element("queue", "replay_audio_queue")
        replay_audio_convert = self._make_element("audioconvert", "replay_audio_convert")
        replay_audio_resample = self._make_element("audioresample", "replay_audio_resample")
        replay_sink, replay_sink_factory_name = self._make_video_sink("replay_sink")
        replay_audio_sink, _replay_audio_sink_name = self._make_audio_sink("replay_audio_sink")

        self._set_property_if_supported(replay_sink, "sync", False)
        self._set_property_if_supported(replay_sink, "qos", True)
        self._set_property_if_supported(replay_sink, "force-aspect-ratio", True)
        self._set_property_if_supported(replay_audio_sink, "sync", False)

        replay_pipeline.add(replay_source)
        replay_pipeline.add(replay_video_queue)
        replay_pipeline.add(replay_convert)
        replay_pipeline.add(replay_audio_queue)
        replay_pipeline.add(replay_audio_convert)
        replay_pipeline.add(replay_audio_resample)
        replay_pipeline.add(replay_sink)
        replay_pipeline.add(replay_audio_sink)
        if not replay_video_queue.link(replay_convert) or not replay_convert.link(replay_sink):
            raise RuntimeError("Failed to link the replay playback pipeline.")
        if (
            not replay_audio_queue.link(replay_audio_convert)
            or not replay_audio_convert.link(replay_audio_resample)
            or not replay_audio_resample.link(replay_audio_sink)
        ):
            raise RuntimeError("Failed to link the replay audio path.")
        replay_source.connect(
            "pad-added",
            self._on_replay_decodebin_pad_added,
            (replay_video_queue, replay_audio_queue),
        )

        self._replay_pipeline = replay_pipeline
        self._replay_source = replay_source
        self._replay_bus = replay_pipeline.get_bus()
        if self._replay_bus is not None:
            self._replay_bus.enable_sync_message_emission()
            self._replay_bus.connect("sync-message::element", self._on_bus_sync_message)
        self._replay_sink = replay_sink
        self._replay_sink_factory_name = replay_sink_factory_name
        self._bind_active_video_sink_locked()

    def _build_replay_audio_pipeline(self) -> None:
        """Muxed replay uses the main replay decode pipeline for audio and video."""
        return

    def _on_replay_decodebin_pad_added(self, _decodebin: Any, pad: Any, targets: tuple[Any, Any]) -> None:
        video_queue, audio_queue = targets
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_name = ""
        if caps is not None and caps.get_size() > 0:
            caps_name = caps.get_structure(0).get_name()
        target = audio_queue if caps_name.startswith("audio/") else video_queue
        sink_pad = target.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        pad.link(sink_pad)

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

    def _request_audio_tee_pad(self) -> Any:
        assert self._pipeline is not None
        tee = self._pipeline.get_by_name("audio_tee")
        assert tee is not None

        request_pad = tee.request_pad_simple("src_%u")
        if request_pad is None:
            request_pad = tee.get_request_pad("src_%u")
        if request_pad is None:
            raise RuntimeError("Failed to request an audio tee source pad.")

        self._audio_tee_request_pads.append(request_pad)
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
                LOGGER.info("Selected video sink %s for %s", factory_name, element_name)
                return sink, factory_name

        raise RuntimeError(
            "Failed to create an embedded preview video sink. "
            "Tried d3d11videosink, glimagesink, and d3dvideosink."
        )

    def _make_audio_sink(self, element_name: str) -> tuple[Any, str]:
        Gst = self._Gst
        assert Gst is not None

        for factory_name in ("wasapisink", "directsoundsink", "autoaudiosink", "fakesink"):
            if Gst.ElementFactory.find(factory_name) is None:
                continue
            sink = Gst.ElementFactory.make(factory_name, element_name)
            if sink is not None:
                LOGGER.info("Selected audio sink %s for %s", factory_name, element_name)
                return sink, factory_name

        raise RuntimeError(
            "Failed to create an audio sink. Tried wasapisink, directsoundsink, autoaudiosink, and fakesink."
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

            # Native sources stream frames directly through GStreamer; the
            # appsrc feed loop is only needed for python_push sources.
            if self._source.pipeline_mode == PipelineMode.PYTHON_PUSH:
                self._frame_feed_thread = threading.Thread(
                    target=self._feed_appsrc_loop,
                    name="gst-appsrc-feed",
                    daemon=True,
                )
                self._frame_feed_thread.start()

                if self._audio_appsrc is not None and self._audio_feed_thread is None:
                    self._audio_feed_thread = threading.Thread(
                        target=self._feed_audio_appsrc_loop,
                        name="gst-audio-appsrc-feed",
                        daemon=True,
                    )
                    self._audio_feed_thread.start()

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

            if self._feed_metrics is not None:
                # Each iteration of this loop is one frame round-tripping
                # through Python — count it for the §3.A diagnostics readout.
                self._feed_metrics.tick_python_frame()

            if self._appsrc is None:
                break

            frame_overlay = FrameOverlayInfo.from_media_frame(frame, feed_id=frame.feed_id)
            # Burn immutable frame metadata once before tee fan-out so live preview,
            # rolling replay storage, and recorded output stay visually consistent.
            frame_array = np.ascontiguousarray(render_frame_overlay(frame.image_bgr, frame_overlay))
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
                    frame_overlay=frame_overlay,
                )
                while len(self._frame_metadata) > 4096:
                    self._frame_metadata.popitem(last=False)

            flow_return = self._appsrc.emit("push-buffer", gst_buffer)
            if flow_return != Gst.FlowReturn.OK and not self._stop_event.is_set():
                self._preview_output.show_placeholder_message(
                    f"GStreamer source push failed: {flow_return}"
                )
                break

    def _feed_audio_appsrc_loop(self) -> None:
        Gst = self._Gst
        assert Gst is not None

        while not self._stop_event.is_set():
            if self._audio_appsrc is None:
                break
            chunk = self._source.read_audio_chunk()
            if chunk is None:
                time.sleep(0.005)
                continue

            gst_buffer = Gst.Buffer.new_allocate(None, len(chunk.data), None)
            gst_buffer.fill(0, chunk.data)
            if self._audio_stream_start_timestamp is None:
                self._audio_stream_start_timestamp = chunk.timestamp
            running_timestamp = max(0.0, chunk.timestamp - self._audio_stream_start_timestamp)
            gst_buffer.pts = int(running_timestamp * Gst.SECOND)
            gst_buffer.dts = gst_buffer.pts
            gst_buffer.duration = int(chunk.duration_seconds * Gst.SECOND)

            flow_return = self._audio_appsrc.emit("push-buffer", gst_buffer)
            if flow_return != Gst.FlowReturn.OK and not self._stop_event.is_set():
                LOGGER.warning("GStreamer audio source push failed: %s", flow_return)
                break

    def _monitor_bus_loop(self) -> None:
        Gst = self._Gst
        assert Gst is not None

        interesting_messages = (
            Gst.MessageType.ERROR
            | Gst.MessageType.WARNING
            | Gst.MessageType.INFO
            | Gst.MessageType.EOS
            | Gst.MessageType.QOS
        )
        while not self._stop_event.is_set():
            if self._poll_bus_for_messages(
                self._bus,
                interesting_messages,
                int(Gst.SECOND / 20),
                pipeline_role="live",
                fatal=True,
            ):
                break
            if self._poll_bus_for_messages(
                self._replay_bus,
                interesting_messages,
                0,
                pipeline_role="replay",
                fatal=False,
            ):
                break
            if self._poll_bus_for_messages(
                self._replay_audio_bus,
                interesting_messages,
                0,
                pipeline_role="replay-audio",
                fatal=False,
            ):
                break

    def _poll_bus_for_messages(
        self,
        bus: Any,
        interesting_messages: Any,
        timeout_ns: int,
        *,
        pipeline_role: str,
        fatal: bool,
    ) -> bool:
        if bus is None:
            return False

        message = bus.timed_pop_filtered(timeout_ns, interesting_messages)
        if message is None:
            return False

        Gst = self._Gst
        feed_id = self._source.get_feed_id()
        mtype = message.type

        if mtype == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            summary = str(error)
            details = debug or ""
            log_bus_message(
                feed_id=feed_id,
                pipeline_role=pipeline_role,
                type_name="ERROR",
                summary=summary,
                details=details,
            )
            if self._feed_state is not None and pipeline_role == "live":
                # ERROR on the live ingest pipeline means we lost the source.
                # Replay/replay-audio errors are non-fatal for the feed itself.
                self._feed_state.transition_to(FeedState.DISCONNECTED)
            if fatal:
                placeholder = details or summary
                self._preview_output.show_placeholder_message(f"GStreamer error: {placeholder}")
                self._stop_event.set()
                return True
            return False

        if mtype == Gst.MessageType.QOS:
            # QOS messages indicate dropped buffers somewhere downstream.
            # Count them on the feed metrics; sustained drops drive the state
            # machine to DEGRADED via the telemetry hub's evaluation path.
            if self._feed_metrics is not None and pipeline_role == "live":
                self._feed_metrics.tick_dropped()
            log_bus_message(
                feed_id=feed_id,
                pipeline_role=pipeline_role,
                type_name="QOS",
            )
            return False

        if mtype == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            log_bus_message(
                feed_id=feed_id,
                pipeline_role=pipeline_role,
                type_name="WARNING",
                summary=str(warn),
                details=debug or "",
            )
            return False

        if mtype == Gst.MessageType.INFO:
            info, debug = message.parse_info()
            log_bus_message(
                feed_id=feed_id,
                pipeline_role=pipeline_role,
                type_name="INFO",
                summary=str(info),
                details=debug or "",
            )
            return False

        if mtype == Gst.MessageType.EOS:
            log_bus_message(
                feed_id=feed_id,
                pipeline_role=pipeline_role,
                type_name="EOS",
            )
            if fatal:
                self._preview_output.show_placeholder_message("GStreamer pipeline reached EOS.")
                self._stop_event.set()
                return True
            return False

        return False

    def _on_bus_sync_message(self, _bus: Any, message: Any) -> None:
        structure = message.get_structure()
        if structure is None or structure.get_name() != "prepare-window-handle":
            return
        # Native preview (slice 3.A.3): bind whichever per-window sink is
        # asking for a window handle, matched against the stored per-role
        # handles. This is the canonical GstVideoOverlay protocol — without
        # it d3d11videosink creates its own top-level window.
        if (
            self._operator_preview_sink is not None
            and message.src == self._operator_preview_sink
            and self._operator_window_handle is not None
        ):
            with self._pipeline_lock:
                self._bind_named_video_sink_locked(
                    self._operator_preview_sink, self._operator_window_handle
                )
            return
        if (
            self._program_preview_sink is not None
            and message.src == self._program_preview_sink
            and self._program_window_handle is not None
        ):
            with self._pipeline_lock:
                self._bind_named_video_sink_locked(
                    self._program_preview_sink, self._program_window_handle
                )
            return
        # Legacy / replay paths.
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
        audio_valve = self._audio_branch_valves.get(branch_name)
        if audio_valve is not None:
            audio_enabled = enabled
            if branch_name == "preview":
                audio_enabled = enabled and self._live_audio_monitor_enabled and self._active_video_output == "live"
            audio_valve.set_property("drop", not audio_enabled)

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

        return Gst.PadProbeReturn.OK

    def _on_preview_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        sample = sink.emit("pull-sample")
        frame = self._sample_to_media_frame(sample)
        if frame is not None:
            if self._feed_metrics is not None:
                self._feed_metrics.tick_source()
                if self._preview_running:
                    self._feed_metrics.tick_preview()
            if self._preview_running:
                if self._frame_callback is not None:
                    self._frame_callback(frame)
                if self._live_sample_callback is not None:
                    self._live_sample_callback(
                        FrameOverlayInfo.from_media_frame(frame, feed_id=frame.feed_id)
                    )
        return Gst.FlowReturn.OK

    def _on_record_sample(self, sink: Any) -> Any:
        # Phase 4.A: the record branch is now driven by splitmuxsink, not an
        # appsink — this method is no longer wired to a `new-sample` signal
        # in the new branch shape. Kept as a stub so existing references in
        # tests and `_add_branch` don't break; will be deleted in 4.D.
        Gst = self._Gst
        assert Gst is not None
        return Gst.FlowReturn.OK

    def _on_replay_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        sample = sink.emit("pull-sample")
        frame = self._sample_to_media_frame(sample)
        if frame is not None and self._replay_running:
            self._replay_buffer.append_frame(frame)
        return Gst.FlowReturn.OK

    def _on_record_audio_sample(self, sink: Any) -> Any:
        # Phase 4.A: audio recording is deferred to a 4.A.bis follow-up
        # (when audio is wired into the same `splitmuxsink` for muxed
        # video+audio segments). For now drain the appsink so it doesn't
        # back-pressure the audio tee, but don't call the legacy Recorder
        # path (which would deadlock via MuxedMediaWriter).
        Gst = self._Gst
        assert Gst is not None
        sink.emit("pull-sample")
        return Gst.FlowReturn.OK

    def _on_replay_audio_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        assert Gst is not None

        sample = sink.emit("pull-sample")
        chunk = self._sample_to_audio_chunk(sample)
        if chunk is not None and self._replay_running:
            self._replay_buffer.append_audio_chunk(chunk)
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

        frame_overlay = self._build_frame_overlay(frame_id=frame_id, metadata=metadata)

        return MediaFrame(
            frame_id=frame_id,
            timestamp=frame_overlay.capture_timestamp or time.time(),
            image=frame_array,
            source_name=frame_overlay.source_name or self._source.get_display_name(),
            feed_id=frame_overlay.feed_id or self._source.get_feed_id(),
        )

    def _sample_to_audio_chunk(self, sample: Any) -> AudioChunk | None:
        if sample is None or self._audio_format is None:
            return None

        Gst = self._Gst
        assert Gst is not None

        buffer = sample.get_buffer()
        if buffer is None:
            return None

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return None

        try:
            data = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        timestamp = time.time()
        if self._audio_stream_start_timestamp is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
            timestamp = self._audio_stream_start_timestamp + (buffer.pts / Gst.SECOND)
        return AudioChunk(
            timestamp=timestamp,
            data=data,
            format=self._audio_format,
            source_name=self._source.get_display_name(),
            feed_id=self._source.get_feed_id(),
        )

    def _build_frame_overlay(
        self,
        *,
        frame_id: int | None,
        metadata: _FrameMetadata | None,
    ) -> FrameOverlayInfo:
        if metadata is not None:
            return metadata.frame_overlay
        return FrameOverlayInfo(
            feed_id=self._source.get_feed_id(),
            source_name=self._source.get_display_name(),
            frame_id=frame_id,
            capture_timestamp=time.time(),
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
            audio_tee = self._pipeline.get_by_name("audio_tee")
            self._pipeline.set_state(Gst.State.NULL)

            if self._preview_probe_pad is not None and self._preview_probe_id is not None:
                self._preview_probe_pad.remove_probe(self._preview_probe_id)

            self._teardown_replay_pipeline_locked()

            if tee is not None:
                for request_pad in self._tee_request_pads:
                    tee.release_request_pad(request_pad)
            if audio_tee is not None:
                for request_pad in self._audio_tee_request_pads:
                    audio_tee.release_request_pad(request_pad)

            self._tee_request_pads.clear()
            self._audio_tee_request_pads.clear()
            self._branch_valves.clear()
            self._audio_branch_valves.clear()
            self._preview_sink = None
            self._preview_sink_factory_name = None
            self._preview_probe_pad = None
            self._preview_probe_id = None
            self._pipeline = None
            self._appsrc = None
            self._audio_appsrc = None
            self._bus = None
            self._stream_start_timestamp = None
            self._audio_stream_start_timestamp = None
            self._audio_format = None
            with self._metadata_lock:
                self._frame_metadata.clear()

    def _teardown_replay_pipeline_locked(self) -> None:
        if self._replay_pipeline is None and self._replay_audio_pipeline is None:
            return

        if self._replay_pipeline is not None:
            self._replay_pipeline.set_state(self._Gst.State.NULL)
        self._replay_pipeline = None
        self._replay_source = None
        self._replay_bus = None
        self._replay_sink = None
        self._replay_sink_factory_name = None
        if self._replay_audio_pipeline is not None:
            self._replay_audio_pipeline.set_state(self._Gst.State.NULL)
        self._replay_audio_pipeline = None
        self._replay_audio_source = None
        self._replay_audio_bus = None
