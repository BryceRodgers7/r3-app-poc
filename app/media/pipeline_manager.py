"""GStreamer-centered media graph orchestration for the replay application."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
import importlib
import logging
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np

from datetime import datetime, timezone

from app.core.feed_state import FeedState
from app.core.models import (
    AudioFormat,
    FrameOverlayInfo,
    IngestTelemetry,
    MediaFrame,
    SEGMENT_STATE_COMPLETE,
    Segment,
    SessionPaths,
)
from app.core.session_clock import SessionClock
from app.core.state_machine import StateMachine
from app.core.telemetry import FeedMetrics
from app.media.frame_overlay import render_frame_overlay
from app.media.gst_bus_log import log_bus_message
from app.media.source_interface import PipelineMode
from app.media.preview_output import PreviewOutput
from app.media.source_interface import SourceInterface
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex

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
        audio_enabled: bool = True,
        live_audio_monitor_enabled: bool = True,
        recording_enabled: bool = True,
        recording_segment_duration_seconds: float = 4.0,
        recording_codec: str = "mjpeg",
        recording_container: str = "mkv",
        recording_audio_enabled: bool = True,
        audio_bitrate: int = 128_000,
        force_python_push_preview: bool = False,
    ) -> None:
        self._source = source
        self._preview_output = preview_output
        self._audio_enabled = audio_enabled
        self._live_audio_monitor_enabled = live_audio_monitor_enabled
        self._recording_enabled = recording_enabled
        # Slice 4.F: only mux audio into segments when the operator has
        # explicitly opted in AND the source has audio capability.
        self._recording_audio_enabled = recording_audio_enabled
        self._audio_bitrate = max(8_000, int(audio_bitrate))
        # Slice 3.A.3 escape hatch — when True, force the appsink/QImage
        # path even on NATIVE-mode sources.
        self._force_python_push_preview = force_python_push_preview
        self._preview_running = False
        self._recording_running = False
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
        self._tee_request_pads: list[Any] = []
        self._audio_tee_request_pads: list[Any] = []
        self._branch_valves: dict[str, Any] = {}
        self._audio_branch_valves: dict[str, Any] = {}
        # Slice 3.B: queue elements stored per-branch so the telemetry
        # hub can sample `current-level-buffers` periodically. Keys
        # match the branch names ("preview", "record").
        self._branch_queues: dict[str, Any] = {}
        # Configured caps per branch — populated alongside the queue
        # element so saturation calc has both the level and the limit.
        self._branch_queue_caps: dict[str, dict[str, int]] = {}
        self._preview_sink: Any | None = None
        self._preview_sink_factory_name: str | None = None
        self._operator_preview_sink: Any | None = None
        self._program_preview_sink: Any | None = None
        self._operator_window_handle: int | None = None
        self._program_window_handle: int | None = None
        self._preview_probe_pad: Any | None = None
        self._preview_probe_id: int | None = None
        self._video_window_handle: int | None = None

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
        # Phase 4.B: per-segment metadata tracking. The buffer probe on
        # jpegenc.src updates `_pending_segment` for the currently-writing
        # segment; format-location and disable_file_recording finalize it
        # into the metadata DB + segment index.
        self._pending_segment: dict[str, Any] | None = None
        self._metadata_db: MetadataDb | None = None
        self._segment_index: SegmentIndex | None = None
        # Slice 5.A: session-clock reference (per-app monotonic origin).
        # Captured at first-buffer of each segment for `pts_to_session_offset_ns`.
        self._session_clock: SessionClock | None = None
        # Bug-fix flag: set on `disable_file_recording`. The next
        # `enable_file_recording` checks it and rebuilds the
        # splitmuxsink element entirely. State-cycling the existing
        # splitmuxsink (NULL → PLAYING) doesn't fully reset its
        # internal "current file" pointer — buffers after re-enable
        # appended to the previous game's file. A fresh element
        # guarantees a fresh `format-location` call for the new game.
        self._recording_was_disabled: bool = False
        # Refs needed to rebuild the splitmuxsink in place: jpegenc's
        # src pad links into splitmuxsink, and the branch name
        # determines the element's GST name. Captured during
        # `_add_record_branch_via_splitmuxsink`.
        self._record_branch_jpegenc: Any | None = None
        self._record_branch_name: str | None = None
        # Each press of "Start game recording" gets its own subfolder
        # under `<session>/recording/`. Captured here at enable time;
        # `_on_splitmuxsink_format_location` builds the per-segment
        # path as `<session>/recording/<game_subdir>/<feed_id>/segment_NNNNN.mkv`.
        # When `None`, segments fall back to the legacy
        # `<session>/recording/<feed_id>/segment_NNNNN.mkv` layout —
        # kept for the format-location fallback path and tests.
        self._recording_game_subdir: str | None = None
        self._recording_codec: str = recording_codec.strip().lower() or "mjpeg"
        self._recording_container: str = recording_container.strip().lower() or "mkv"
        if self._recording_codec != "mjpeg" or self._recording_container != "mkv":
            raise RuntimeError(
                f"Phase 4.A only supports recording codec='mjpeg' container='mkv'. "
                f"Got codec={self._recording_codec!r} container={self._recording_container!r}. "
                f"ProRes/DNxHR support is deferred."
            )

    def describe_architecture(self) -> str:
        """Describe the current tee/fan-out architecture."""
        return (
            "SourceInterface (native or appsrc) -> tee -> "
            "[preview branch, splitmuxsink-driven recording branch] + "
            "audio tee -> [live audio sink (optional)]"
        )

    def start_preview(self) -> None:
        """Start the preview branch without affecting recording."""
        self._preview_running = True
        self._preview_output.show_placeholder_message("Starting live preview...")
        self._set_branch_enabled("preview", True)

    def enable_file_recording(
        self,
        session_paths: SessionPaths,
        feed_id: str | None = None,
        *,
        start_fragment_index: int = 0,
        game_subdir: str | None = None,
    ) -> None:
        """Open the record branch's valve so segmented recording starts (Phase 4.A).

        The actual writing is done by `splitmuxsink` downstream of the
        valve; we just need to let buffers through and tell the
        `format-location` callback which session/feed to write under.

        `start_fragment_index` lets the §11.4 Resume path seed the
        counter past the highest pre-crash segment file so the resumed
        recording doesn't overwrite or reuse a filename. Default 0
        (fresh session) preserves the original behavior.
        """
        if self._splitmuxsink is None:
            LOGGER.warning(
                "enable_file_recording called but recording branch is not "
                "configured; ignoring."
            )
            return
        self._recording_session_paths = session_paths
        self._recording_feed_id = feed_id or self._source.get_feed_id()
        self._recording_game_subdir = game_subdir
        # Reset our private segment counter so each recording session
        # starts at the requested fragment index. Splitmuxsink's
        # internal `fragment_id` is unreliable (it increments during
        # startup state transitions before any buffer is written), so
        # we always pick the filename ourselves via the
        # `format-location` callback.
        self._recording_segment_counter = max(0, int(start_fragment_index))
        feed_paths = session_paths.get_feed_paths(self._recording_feed_id)
        feed_paths.recording_dir.mkdir(parents=True, exist_ok=True)
        # If a prior `disable_file_recording` shut down splitmuxsink
        # (NULL state, file got its trailer), rebuild it as a fresh
        # element. The state-cycle approach (NULL → PLAYING) didn't
        # actually clear splitmuxsink's internal "current file"
        # pointer, so buffers after re-enable kept appending to the
        # previous game's last file. A fresh element guarantees
        # `format-location` fires for the new game's first segment.
        if self._recording_was_disabled and self._splitmuxsink is not None:
            try:
                self._rebuild_splitmuxsink_locked()
            except Exception:
                LOGGER.exception(
                    "splitmuxsink rebuild failed on re-enable for feed_id=%s",
                    self._recording_feed_id,
                )
        self._set_branch_enabled("record", True)
        self._recording_running = True
        self._recording_was_disabled = False
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

        Slice 4.B: flush the in-progress segment's metadata into the DB
        + index. The on-disk file may still be finalizing asynchronously
        (matroskamux + async-finalize=True), so file size is read with
        best-effort accuracy.
        """
        # Close the valve first so no more buffers reach splitmuxsink
        # while we transition it to NULL.
        self._recording_running = False
        self._set_branch_enabled("record", False)
        self._finalize_pending_segment_locked()
        # Force splitmuxsink to finalize the current file. The
        # PLAYING → NULL state transition triggers matroskamux's
        # cleanup path which writes the MKV trailer; until that
        # happens the on-disk file has no end marker and players
        # treat it as truncated. `split-now` was insufficient — it
        # only schedules a rotation on the next buffer, which never
        # arrives once the valve is closed, so the trailer isn't
        # written until the entire pipeline shuts down on app exit.
        # We bring splitmuxsink back to PLAYING in `enable_file_recording`.
        if self._splitmuxsink is not None and self._Gst is not None:
            try:
                state_change = self._splitmuxsink.set_state(self._Gst.State.NULL)
                LOGGER.info(
                    "splitmuxsink → NULL on disable for feed_id=%s, result=%s",
                    self._recording_feed_id,
                    state_change.value_nick if hasattr(state_change, "value_nick") else state_change,
                )
            except Exception:
                LOGGER.exception(
                    "splitmuxsink NULL transition failed on disable for feed_id=%s",
                    self._recording_feed_id,
                )
        # Mark for state-restore on the next enable. See the flag
        # docstring in __init__.
        self._recording_was_disabled = True
        LOGGER.info(
            "Recording stopped for feed_id=%s",
            self._recording_feed_id,
        )

    def stop_preview(self) -> None:
        """Stop only the preview branch."""
        self._preview_running = False
        self._set_branch_enabled("preview", False)

    def stop_all(self) -> None:
        """Stop all branches, tear down the pipeline, and disconnect the source."""
        self._preview_running = False
        self._recording_running = False
        self._stop_event.set()

        for branch_name in ("preview", "record"):
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

    def set_metadata_db(self, db: MetadataDb | None) -> None:
        """Attach the SQLite metadata-db that segment rows are written to (slice 4.B)."""
        self._metadata_db = db

    def set_segment_index(self, index: SegmentIndex | None) -> None:
        """Attach the in-memory segment index that finalized segments are added to (slice 4.B)."""
        self._segment_index = index

    def set_session_clock(self, clock: SessionClock | None) -> None:
        """Attach the per-app monotonic session clock (slice 5.A).

        Used to capture `pts_to_session_offset_ns` at the first buffer
        of each segment so replay queries can resolve session-time to
        a `(segment, offset_in_segment_ns)` pair without rerunning a
        per-feed PTS-mapping query (§8.3 / §6.3).
        """
        self._session_clock = clock

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
        """Bind one specific sink to a window handle via GstVideoOverlay.

        Slice 3.A.3 retry: logs the sink name + handle + binding path
        taken so when something goes sideways on hardware we can see
        which sink actually got a usable handle. The two paths are:

        - direct `sink.set_window_handle(handle)` — works on some
          PyGObject builds where the GstVideoOverlay introspection
          attaches the method directly.
        - `GstVideo.VideoOverlay.set_window_handle(sink, handle)` —
          the canonical interface call; works everywhere but only if
          GstVideo was loaded.
        """
        sink_name = sink.get_name() if sink is not None else "<None>"
        if sink is None or window_handle is None:
            LOGGER.warning(
                "video-sink bind skipped: sink=%s handle=%r",
                sink_name,
                window_handle,
            )
            return
        try:
            sink.set_window_handle(window_handle)
            LOGGER.info(
                "video-sink bound via direct: sink=%s handle=0x%x",
                sink_name,
                int(window_handle),
            )
            return
        except Exception as exc:
            LOGGER.debug(
                "video-sink direct set_window_handle failed for %s: %s",
                sink_name,
                exc,
            )
        GstVideo = self._GstVideo
        if GstVideo is None:
            LOGGER.warning(
                "video-sink bind skipped: GstVideo unavailable, sink=%s",
                sink_name,
            )
            return
        try:
            GstVideo.VideoOverlay.set_window_handle(sink, window_handle)
            LOGGER.info(
                "video-sink bound via GstVideoOverlay: sink=%s handle=0x%x",
                sink_name,
                int(window_handle),
            )
        except Exception:
            LOGGER.exception(
                "Failed to bind video sink %s to window handle 0x%x",
                sink_name,
                int(window_handle),
            )

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
            self._preview_probe_pad = None
            self._preview_probe_id = None

            self._add_branch("preview")
            self._add_branch("record")
            self._build_audio_path_locked()

            # If window handles were registered before the pipeline was
            # built, bind them to the freshly-created native preview sinks.
            # Slice 3.A.3 retry: log pre-bind state so it's clear from
            # the run log whether both sinks got handles before the
            # pipeline transitions to PLAYING.
            LOGGER.info(
                "native-preview pre-bind: operator_sink=%s operator_handle=%r "
                "program_sink=%s program_handle=%r",
                self._operator_preview_sink.get_name() if self._operator_preview_sink else None,
                self._operator_window_handle,
                self._program_preview_sink.get_name() if self._program_preview_sink else None,
                self._program_window_handle,
            )
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

    def _configure_preview_queue(self, queue: Any, branch_name: str) -> None:
        """Apply the §4.3 preview queue policy.

        Leaky-downstream so a slow preview drops frames rather than
        pushing back on the source tee, time-bounded at 200ms, with a
        small buffer-count cap as a secondary guard.
        """
        # `leaky=2` is GST_QUEUE_LEAK_DOWNSTREAM — drop the oldest
        # buffer when the queue is full, never block upstream.
        max_size_buffers = 4
        max_size_time_ns = 200_000_000  # 200 ms
        try:
            queue.set_property("leaky", 2)
            queue.set_property("max-size-buffers", max_size_buffers)
            queue.set_property("max-size-time", max_size_time_ns)
            queue.set_property("max-size-bytes", 0)
        except Exception:
            LOGGER.exception("Failed to configure preview queue policy")
        self._branch_queues[branch_name] = queue
        self._branch_queue_caps[branch_name] = {
            "max_size_buffers": max_size_buffers,
            "max_size_time_ns": max_size_time_ns,
        }

    def _configure_record_queue(self, queue: Any, branch_name: str) -> None:
        """Apply the §4.4 record queue policy.

        Non-leaky: pressure on the recording branch must surface as a
        `RECORDING_ERROR` health event rather than silently dropping
        buffers. Time-bounded at one segment's worth of frames so a
        wedged disk eventually stops the pipeline rather than ballooning
        memory unbounded.
        """
        Gst = self._Gst
        assert Gst is not None
        max_size_buffers = 256
        max_size_time_ns = int(self._recording_segment_duration_seconds * Gst.SECOND)
        try:
            queue.set_property("leaky", 0)  # GST_QUEUE_LEAK_NO
            queue.set_property("max-size-buffers", max_size_buffers)
            queue.set_property("max-size-time", max_size_time_ns)
            queue.set_property("max-size-bytes", 0)
        except Exception:
            LOGGER.exception("Failed to configure record queue policy")
        self._branch_queues[branch_name] = queue
        self._branch_queue_caps[branch_name] = {
            "max_size_buffers": max_size_buffers,
            "max_size_time_ns": max_size_time_ns,
        }

    def sample_queue_depths(self) -> dict[str, dict[str, int]]:
        """Read `current-level-*` from each tracked branch queue.

        Returns a mapping from branch name to a dict with `buffers`,
        `time_ns`, and the configured max for each. Used by the
        telemetry hub's periodic sampler. Errors are swallowed and
        return zero — a flaky element shouldn't break the diagnostics
        widget.
        """
        out: dict[str, dict[str, int]] = {}
        for name, queue in self._branch_queues.items():
            try:
                buffers = int(queue.get_property("current-level-buffers") or 0)
                time_ns = int(queue.get_property("current-level-time") or 0)
            except Exception:
                buffers = 0
                time_ns = 0
            caps = self._branch_queue_caps.get(name, {})
            out[name] = {
                "buffers": buffers,
                "time_ns": time_ns,
                "max_buffers": int(caps.get("max_size_buffers", 0)),
                "max_time_ns": int(caps.get("max_size_time_ns", 0)),
            }
        return out

    def _add_branch(self, branch_name: str) -> None:
        if branch_name == "preview":
            self._add_preview_branch(branch_name)
            return
        if branch_name == "record":
            self._add_record_branch_via_splitmuxsink(branch_name)
            return
        raise RuntimeError(f"Unknown branch_name {branch_name!r}")

    def _add_preview_branch(self, branch_name: str) -> None:
        """Pick the preview branch shape based on the source's pipeline mode.

        Slice 3.A.3 (retry): NATIVE sources go through
        `_add_native_preview_branch` (d3d11videosink rendering directly
        into the window's child surface — no Python pixel hop). The
        original 3.A.3 attempt was reverted because of a "third
        Direct3D11 renderer window" symptom; the most likely cause was
        the legacy replay pipeline's d3d11videosink (which 4.D removed
        entirely), so this retry should not reproduce that failure.

        `force_python_push_preview` is the operator-level escape hatch
        — flip it on in `app_settings.toml` if the d3d11 path
        misbehaves on the local hardware.

        `python_push` sources (synthetic test source) always take the
        appsink path; they have no native GStreamer source to feed
        d3d11videosink anyway.
        """
        if (
            self._source.pipeline_mode == PipelineMode.NATIVE
            and not self._force_python_push_preview
        ):
            self._add_native_preview_branch(branch_name)
            return
        self._add_python_push_preview_branch(branch_name)

    def _add_python_push_preview_branch(self, branch_name: str) -> None:
        """Legacy preview branch used by python_push (synthetic) sources.

        Frames flow `tee → queue → valve → videoconvert → appsink`; the
        appsink emits `new-sample` and `_on_preview_sample` reshapes the
        buffer into a NumPy `MediaFrame` for QImage rendering.

        Slice 3.B: explicit §4.3 queue policy — leaky downstream so a
        slow preview consumer drops frames rather than back-pressuring
        the source tee, and time-bounded at 200 ms so the preview never
        falls more than that behind live. The policy is codified here
        rather than left as queue defaults so 3.A.3 / NDI hardware
        retries can't accidentally regress it.
        """
        assert self._pipeline is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", f"{branch_name}_queue")
        valve = self._make_element("valve", f"{branch_name}_valve")
        convert = self._make_element("videoconvert", f"{branch_name}_convert")
        sink = self._make_element("appsink", f"{branch_name}_sink")

        self._configure_preview_queue(queue, branch_name)
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

        self._configure_preview_queue(queue, branch_name)
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
        # Each per-window queue is leaky-downstream + time-bounded so a
        # blocked window (minimized, occluded, dead d3d11 device) drops
        # frames rather than back-pressuring the source tee and freezing
        # the whole pipeline. This is the §4.3 policy applied to the
        # native-preview window queues — the head queue already has it
        # via _configure_preview_queue.
        for sink, label in (
            (operator_sink, "operator"),
            (program_sink, "program"),
        ):
            sink_queue = self._make_element("queue", f"{branch_name}_{label}_queue")
            try:
                sink_queue.set_property("leaky", 2)  # GST_QUEUE_LEAK_DOWNSTREAM
                sink_queue.set_property("max-size-buffers", 4)
                sink_queue.set_property("max-size-time", 200_000_000)  # 200 ms
                sink_queue.set_property("max-size-bytes", 0)
            except Exception:
                LOGGER.exception(
                    "Failed to configure %s native-preview queue policy", label
                )
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
        self._configure_record_queue(queue, branch_name)
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
        splitmuxsink = self._build_splitmuxsink_element(branch_name)

        valve.set_property("drop", True)

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

        # Slice 4.B: probe jpegenc's src so we capture per-segment first/last
        # PTS and frame counts of the *encoded* stream (matches what
        # splitmuxsink actually writes to disk).
        jpegenc_src_pad = jpegenc.get_static_pad("src")
        if jpegenc_src_pad is not None:
            jpegenc_src_pad.add_probe(
                Gst.PadProbeType.BUFFER,
                self._on_jpegenc_buffer_probe,
                None,
            )

        for element in (queue, valve, convert, encode_caps, jpegenc, splitmuxsink):
            element.sync_state_with_parent()

        self._branch_valves[branch_name] = valve
        self._splitmuxsink = splitmuxsink
        # Capture refs needed to rebuild the splitmuxsink on the next
        # disable/enable cycle.
        self._record_branch_jpegenc = jpegenc
        self._record_branch_name = branch_name

    def _build_splitmuxsink_element(self, branch_name: str) -> Any:
        """Create + configure a `splitmuxsink` element.

        Factored out so `_rebuild_splitmuxsink_locked` (called on the
        second-and-subsequent Start) can reuse identical configuration
        without duplicating the property/signal wiring.
        """
        Gst = self._Gst
        assert Gst is not None
        sink = self._make_element("splitmuxsink", f"{branch_name}_splitmuxsink")
        max_size_time_ns = int(self._recording_segment_duration_seconds * Gst.SECOND)
        sink.set_property("max-size-time", max_size_time_ns)
        sink.set_property("muxer-factory", "matroskamux")
        # `async-finalize=True` finalizes closed segments on a worker thread
        # so the streaming thread never blocks on disk flush.
        self._set_property_if_supported(sink, "async-finalize", True)
        sink.connect("format-location", self._on_splitmuxsink_format_location)
        return sink

    def _rebuild_splitmuxsink_locked(self) -> None:
        """Replace the existing splitmuxsink with a fresh instance.

        Called on Start after a previous Stop. State-cycling the
        existing splitmuxsink (NULL → PLAYING) wasn't enough — its
        internal "current file" pointer survived the cycle, so
        post-Start buffers appended to the previous game's last file
        instead of triggering a fresh `format-location` call. A new
        element instance has no such state.

        Steps:
          1. Unlink jpegenc → old splitmuxsink.
          2. Set old splitmuxsink to NULL (defensive — disable should
             have done this already, but a no-op repeat is safe).
          3. Remove old splitmuxsink from the pipeline.
          4. Build a fresh splitmuxsink with the same config.
          5. Add to pipeline, link jpegenc → new splitmuxsink.
          6. `sync_state_with_parent` to bring it to PLAYING.
          7. Update `self._splitmuxsink` to point at the new instance.
        """
        if (
            self._splitmuxsink is None
            or self._record_branch_jpegenc is None
            or self._record_branch_name is None
            or self._pipeline is None
            or self._Gst is None
        ):
            LOGGER.warning(
                "rebuild_splitmuxsink: prerequisites missing; skipping rebuild"
            )
            return
        old = self._splitmuxsink
        jpegenc = self._record_branch_jpegenc
        branch_name = self._record_branch_name
        try:
            jpegenc.unlink(old)
        except Exception:
            LOGGER.exception("rebuild_splitmuxsink: unlink jpegenc → old failed")
        try:
            old.set_state(self._Gst.State.NULL)
        except Exception:
            LOGGER.debug("rebuild_splitmuxsink: old set_state(NULL) raised", exc_info=True)
        try:
            self._pipeline.remove(old)
        except Exception:
            LOGGER.exception("rebuild_splitmuxsink: pipeline.remove(old) failed")
        new_sink = self._build_splitmuxsink_element(branch_name)
        self._pipeline.add(new_sink)
        if not jpegenc.link(new_sink):
            raise RuntimeError(
                "rebuild_splitmuxsink: failed to link jpegenc → new splitmuxsink"
            )
        new_sink.sync_state_with_parent()
        self._splitmuxsink = new_sink
        LOGGER.info(
            "splitmuxsink rebuilt for feed_id=%s (fresh format-location callback ready)",
            self._recording_feed_id,
        )

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

        Slice 4.B: also drives segment-metadata lifecycle. By the time
        this callback fires for fragment N, fragment N-1 has been closed
        by splitmuxsink's rotation (modulo `async-finalize` flushing). So
        finalize the previous pending segment (if any) into the metadata
        DB + index, then reset state for the new segment.

        If the callback fires without an active session (defensive — the
        upstream valve should be closed in that case), fall back to a temp
        path so the caller doesn't crash.
        """
        # Finalize the previous segment (if any) before opening the next.
        # This is where we transition state="writing" → "complete" and
        # capture first/last PTS, frame count, file size.
        self._finalize_pending_segment_locked()

        if self._recording_session_paths is None or self._recording_feed_id is None:
            tmp = Path(f"./_unrouted_segment_{fragment_id:05d}.mkv").resolve()
            LOGGER.warning(
                "splitmuxsink requested format-location without an active "
                "recording session; writing to %s",
                tmp,
            )
            return str(tmp)
        # Per-game folder layout: each Start press of "Start game
        # recording" gets its own subfolder under `recording/`. The
        # operator can copy a single game's footage off the system by
        # grabbing one folder. Falls back to the legacy flat layout
        # when no game_subdir was specified (tests, ad-hoc tooling).
        if self._recording_game_subdir:
            base_dir = (
                self._recording_session_paths.recording_dir
                / self._recording_game_subdir
                / self._recording_feed_id
            )
        else:
            feed_paths = self._recording_session_paths.get_feed_paths(
                self._recording_feed_id
            )
            base_dir = feed_paths.recording_dir
        base_dir.mkdir(parents=True, exist_ok=True)
        index = self._recording_segment_counter
        self._recording_segment_counter += 1
        path = base_dir / f"segment_{index:05d}.mkv"
        LOGGER.info(
            "splitmuxsink opening segment index=%d (gst fragment_id=%d) "
            "feed_id=%s path=%s",
            index,
            fragment_id,
            self._recording_feed_id,
            path,
        )
        # Begin tracking the new pending segment. The buffer probe will
        # populate first/last PTS and frame count as buffers flow.
        self._pending_segment = {
            "session_id": self._recording_session_paths.session_id,
            "feed_id": self._recording_feed_id,
            "fragment_index": index,
            "file_path": str(path),
            "first_pts_ns": None,
            "last_pts_ns": None,
            "frame_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            # Slice 5.A — captured by `_on_jpegenc_buffer_probe` on
            # first buffer; None means "no session_clock attached or
            # first buffer never arrived". Stays None for sessions
            # initialized in tests via `__new__` without a clock.
            "first_session_time_ns": None,
        }
        return str(path)

    def _on_jpegenc_buffer_probe(self, _pad: Any, info: Any, _user: Any) -> Any:
        """Track per-segment first/last PTS and frame count (slice 4.B + 5.A).

        Runs on the streaming thread once per encoded JPEG buffer. Reads
        only buffer.pts and increments a counter — no pixel work. The
        captured stats are flushed into a `Segment` row by
        `_finalize_pending_segment_locked` when the segment closes.

        Slice 5.A: also captures `session_time_ns` at the moment of the
        first buffer of the segment. `_finalize_pending_segment_locked`
        uses that to compute `pts_to_session_offset_ns` and the segment's
        `start/end_session_time_ns` (§6.3).
        """
        Gst = self._Gst
        if Gst is None:
            return 0
        pending = self._pending_segment
        if pending is None or not self._recording_running:
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        if buf.pts != Gst.CLOCK_TIME_NONE:
            pts_ns = int(buf.pts)
            if pending["first_pts_ns"] is None:
                pending["first_pts_ns"] = pts_ns
                if self._session_clock is not None:
                    pending["first_session_time_ns"] = (
                        self._session_clock.now_session_time_ns()
                    )
            pending["last_pts_ns"] = pts_ns
        pending["frame_count"] += 1
        return Gst.PadProbeReturn.OK

    def _finalize_pending_segment_locked(self) -> None:
        """Convert the pending-segment state into a `Segment` row + index entry.

        Called from `_on_splitmuxsink_format_location` (when the next
        segment is about to open) and from `disable_file_recording` (when
        the operator clicks Stop). No-op if there's no pending segment or
        the segment had no buffers (e.g. valve closed before any frame
        flowed through).
        """
        pending = self._pending_segment
        self._pending_segment = None
        if pending is None:
            return
        if pending["first_pts_ns"] is None:
            # Empty segment — no buffers ever flowed through. Nothing to
            # record. The on-disk file may exist as zero bytes; let 4.E's
            # cleanup handle it.
            LOGGER.debug(
                "skipping finalize for empty segment %s",
                pending.get("file_path"),
            )
            return
        file_path = Path(pending["file_path"])
        try:
            size_bytes = file_path.stat().st_size if file_path.exists() else 0
        except OSError:
            size_bytes = 0
        first_pts = int(pending["first_pts_ns"])
        last_pts = int(pending["last_pts_ns"]) if pending["last_pts_ns"] is not None else first_pts
        # Slice 5.A: derive session-time fields from the first buffer's
        # session-time reading (captured at the same moment first_pts
        # was observed). `pts_to_session_offset_ns` is the additive
        # offset that maps the segment's PTS-ns timeline onto session
        # time (§6.3 / §8.3). All three fields stay None when no
        # session_clock was attached — keeps test stubs that bypass
        # __init__ working without surprises.
        first_session_time_ns: int | None = pending.get("first_session_time_ns")
        if first_session_time_ns is not None:
            pts_to_session_offset_ns: int | None = first_session_time_ns - first_pts
            start_session_time_ns: int | None = first_session_time_ns
            end_session_time_ns: int | None = last_pts + pts_to_session_offset_ns
        else:
            pts_to_session_offset_ns = None
            start_session_time_ns = None
            end_session_time_ns = None
        segment = Segment(
            session_id=pending["session_id"],
            feed_id=pending["feed_id"],
            fragment_index=int(pending["fragment_index"]),
            file_path=str(file_path),
            codec=self._recording_codec,
            container=self._recording_container,
            start_pts_ns=first_pts,
            end_pts_ns=last_pts,
            duration_ns=max(0, last_pts - first_pts),
            frame_count_estimate=int(pending["frame_count"]),
            size_bytes=size_bytes,
            state=SEGMENT_STATE_COMPLETE,
            created_at=str(pending["created_at"]),
            finalized_at=datetime.now(timezone.utc).isoformat(),
            start_session_time_ns=start_session_time_ns,
            end_session_time_ns=end_session_time_ns,
            pts_to_session_offset_ns=pts_to_session_offset_ns,
        )
        if self._metadata_db is not None:
            try:
                seg_id = self._metadata_db.insert_segment(segment)
                segment = replace(segment, segment_id=seg_id)
            except Exception as exc:
                LOGGER.exception(
                    "Failed to insert segment metadata for %s: %s",
                    file_path,
                    exc,
                )
        if self._segment_index is not None:
            self._segment_index.add(segment)
        LOGGER.info(
            "finalized segment %s: frames=%d bytes=%d duration_ms=%d",
            file_path.name,
            segment.frame_count_estimate,
            segment.size_bytes,
            segment.duration_ns // 1_000_000,
        )

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
        # Slice 4.F: when audio recording is enabled, route the audio
        # tee into the same splitmuxsink the video record branch is
        # using so segments are muxed video+audio. Otherwise drain
        # via a no-op appsink so the audio tee doesn't back-pressure.
        if self._recording_audio_enabled and self._splitmuxsink is not None:
            try:
                self._add_audio_record_branch_to_splitmuxsink()
            except Exception:
                LOGGER.exception(
                    "Failed to wire audio into splitmuxsink; falling back to "
                    "video-only segments. Set [recording] audio_enabled=false "
                    "in app_settings.toml to silence this warning."
                )
                self._add_audio_appsink_branch("record", self._on_record_audio_sample)
        else:
            self._add_audio_appsink_branch("record", self._on_record_audio_sample)

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

    def _add_audio_record_branch_to_splitmuxsink(self) -> None:
        """Wire audio into the same splitmuxsink the video record branch uses (slice 4.F).

        Element shape::

            audio_tee → queue → valve → audioconvert → audioresample
                      → opusenc → splitmuxsink.audio_%u

        Implementation notes:

        - **Pad request order matters.** We request the `audio_%u` pad
          on `splitmuxsink` *before* linking the encoder so the
          internal muxer (`matroskamux`) sees the audio pad during caps
          negotiation. Requesting it after the muxer has already
          committed to a video-only configuration is what most likely
          caused the 4.A.bis freeze.
        - **opusenc** is in `gst-plugins-base` (universally available
          on UCRT64). Bitrate comes from `audio_bitrate` (matches the
          live-audio chain).
        - The valve starts closed (`drop=True`) and is opened in
          lockstep with the video valve by `enable_file_recording` /
          `disable_file_recording`. That keeps audio and video segment
          boundaries aligned.
        """
        assert self._pipeline is not None
        assert self._splitmuxsink is not None
        Gst = self._Gst
        assert Gst is not None

        queue = self._make_element("queue", "audio_record_queue")
        valve = self._make_element("valve", "audio_record_valve")
        convert = self._make_element("audioconvert", "audio_record_convert")
        resample = self._make_element("audioresample", "audio_record_resample")
        encoder = self._make_element("opusenc", "audio_record_opusenc")

        valve.set_property("drop", True)
        encoder.set_property("bitrate", self._audio_bitrate)

        for element in (queue, valve, convert, resample, encoder):
            self._pipeline.add(element)

        if not (
            queue.link(valve)
            and valve.link(convert)
            and convert.link(resample)
            and resample.link(encoder)
        ):
            raise RuntimeError("Failed to link the audio_record branch head.")

        # Request the audio_%u pad BEFORE linking the encoder's src.
        audio_sink_pad = self._splitmuxsink.request_pad_simple("audio_%u")
        if audio_sink_pad is None:
            audio_sink_pad = self._splitmuxsink.get_request_pad("audio_%u")
        if audio_sink_pad is None:
            raise RuntimeError(
                "splitmuxsink does not expose an audio_%u request pad — "
                "muxer-factory likely doesn't support audio."
            )
        encoder_src_pad = encoder.get_static_pad("src")
        if encoder_src_pad is None:
            raise RuntimeError("opusenc src pad missing")
        if encoder_src_pad.link(audio_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(
                "Failed to link opusenc.src to splitmuxsink.audio_%u"
            )

        tee_src_pad = self._request_audio_tee_pad()
        queue_sink_pad = queue.get_static_pad("sink")
        if tee_src_pad.link(queue_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(
                "Failed to link audio tee output to the audio_record branch."
            )

        for element in (queue, valve, convert, resample, encoder):
            element.sync_state_with_parent()
        self._audio_branch_valves["record"] = valve
        LOGGER.info(
            "Audio record branch wired into splitmuxsink at %d bps (opus).",
            self._audio_bitrate,
        )

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
        src_name = message.src.get_name() if message.src is not None else "<None>"
        LOGGER.info(
            "prepare-window-handle from sink=%s; "
            "operator_handle=%r program_handle=%r",
            src_name,
            self._operator_window_handle,
            self._program_window_handle,
        )
        if (
            self._operator_preview_sink is not None
            and message.src == self._operator_preview_sink
        ):
            if self._operator_window_handle is None:
                LOGGER.warning(
                    "prepare-window-handle for operator sink but no "
                    "operator handle stored yet (likely Qt winId race) — "
                    "d3d11videosink will create its own window."
                )
                return
            with self._pipeline_lock:
                self._bind_named_video_sink_locked(
                    self._operator_preview_sink, self._operator_window_handle
                )
            return
        if (
            self._program_preview_sink is not None
            and message.src == self._program_preview_sink
        ):
            if self._program_window_handle is None:
                LOGGER.warning(
                    "prepare-window-handle for program sink but no "
                    "program handle stored yet (likely Qt winId race) — "
                    "d3d11videosink will create its own window."
                )
                return
            with self._pipeline_lock:
                self._bind_named_video_sink_locked(
                    self._program_preview_sink, self._program_window_handle
                )
            return
        if message.src == self._preview_sink:
            with self._pipeline_lock:
                self._bind_video_sink_locked(message.src)
            return
        LOGGER.warning(
            "prepare-window-handle from unrecognized sink=%s — likely "
            "the source of the unintended third window.",
            src_name,
        )

    def _set_branch_enabled(self, branch_name: str, enabled: bool) -> None:
        valve = self._branch_valves.get(branch_name)
        if valve is not None:
            valve.set_property("drop", not enabled)
        audio_valve = self._audio_branch_valves.get(branch_name)
        if audio_valve is not None:
            audio_enabled = enabled
            if branch_name == "preview":
                audio_enabled = enabled and self._live_audio_monitor_enabled
            audio_valve.set_property("drop", not audio_enabled)

    def _bind_active_video_sink_locked(self, expose: bool = True) -> None:
        self._bind_video_sink_locked(self._preview_sink, expose=expose)

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

    def _on_record_audio_sample(self, sink: Any) -> Any:
        # Audio recording (muxed into the splitmuxsink segments) is deferred
        # to slice 4.F. For now drain the appsink so it doesn't
        # back-pressure the audio tee.
        Gst = self._Gst
        assert Gst is not None
        sink.emit("pull-sample")
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
                return

            Gst = self._Gst
            assert Gst is not None

            tee = self._pipeline.get_by_name("source_tee")
            audio_tee = self._pipeline.get_by_name("audio_tee")
            self._pipeline.set_state(Gst.State.NULL)

            if self._preview_probe_pad is not None and self._preview_probe_id is not None:
                self._preview_probe_pad.remove_probe(self._preview_probe_id)

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
