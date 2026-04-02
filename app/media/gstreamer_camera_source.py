"""Native GStreamer-backed webcam source."""

from __future__ import annotations

from fractions import Fraction
import importlib
import logging
import time
from typing import Any

import numpy as np

from app.core.models import MediaFrame
from app.media.source_interface import SourceInterface

LOGGER = logging.getLogger(__name__)


class GStreamerCameraSource(SourceInterface):
    """Captures webcam frames via a native GStreamer source and appsink."""

    def __init__(
        self,
        source_name: str,
        camera_index: int,
        frame_width: int,
        frame_height: int,
        target_fps: float,
    ) -> None:
        self._base_source_name = source_name
        self._display_name = source_name
        self._camera_index = camera_index
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._target_fps = max(target_fps, 1.0)
        self._frame_counter = 0
        self._connected = False
        self._status_message: str | None = None

        self._Gst: Any | None = None
        self._pipeline: Any | None = None
        self._appsink: Any | None = None
        self._bus: Any | None = None
        self._source_factory_name: str | None = None
        self._pending_frame: np.ndarray | None = None

    def connect_source(self) -> bool:
        """Open the preferred GStreamer webcam source if one is available."""
        if self._connected:
            return True

        self._status_message = None
        self._pending_frame = None

        try:
            self._ensure_gstreamer_loaded()
        except Exception as exc:
            LOGGER.info("GStreamer camera source is unavailable: %s", exc)
            return False

        for factory_name in self._get_source_candidates():
            if not self._connect_with_factory(factory_name):
                continue

            startup_frame = self._read_usable_startup_frame()
            if startup_frame is None:
                LOGGER.warning(
                    "Rejected GStreamer camera source %s for index %s because startup frames were empty or black.",
                    factory_name,
                    self._camera_index,
                )
                self.disconnect_source()
                continue

            self._pending_frame = startup_frame
            self._connected = True
            self._display_name = (
                f"{self._base_source_name} (Camera {self._camera_index}, {factory_name})"
            )
            LOGGER.info(
                "Opened camera index %s using native GStreamer source %s.",
                self._camera_index,
                factory_name,
            )
            return True

        return False

    def disconnect_source(self) -> None:
        """Release the active GStreamer capture pipeline."""
        if self._pipeline is not None and self._Gst is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
        self._pipeline = None
        self._appsink = None
        self._bus = None
        self._source_factory_name = None
        self._pending_frame = None
        self._connected = False

    def is_connected(self) -> bool:
        """Return whether the source is available for frame reads."""
        return self._connected

    def get_display_name(self) -> str:
        """Return the active source label."""
        return self._display_name

    def create_pipeline_fragment(self) -> str:
        """Describe the native GStreamer source path."""
        return "native-gstreamer-camera-source"

    def read_frame(self) -> MediaFrame | None:
        """Read the next frame from the appsink."""
        if not self._connected:
            return None

        if self._pending_frame is not None:
            frame = self._pending_frame
            self._pending_frame = None
            return self._build_media_frame(frame)

        frame = self._pull_frame(timeout_ns=self._sample_timeout_ns())
        if frame is None:
            return None
        return self._build_media_frame(frame)

    def get_frame_size(self) -> tuple[int, int]:
        """Return the requested capture size."""
        return self._frame_width, self._frame_height

    def get_nominal_fps(self) -> float:
        """Return the requested capture frame rate."""
        return self._target_fps

    def get_status_message(self) -> str | None:
        """Return the current non-fatal status message."""
        return self._status_message

    def _ensure_gstreamer_loaded(self) -> None:
        if self._Gst is not None:
            return

        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gst_module = importlib.import_module("gi.repository.Gst")
        gst_module.init(None)
        self._Gst = gst_module

    def _get_source_candidates(self) -> list[str]:
        assert self._Gst is not None
        candidates = ("ksvideosrc", "mfvideosrc")
        return [
            factory_name
            for factory_name in candidates
            if self._Gst.ElementFactory.find(factory_name) is not None
        ]

    def _connect_with_factory(self, factory_name: str) -> bool:
        assert self._Gst is not None

        try:
            pipeline = self._Gst.Pipeline.new(f"camera-source-{factory_name}")
            source = self._make_element(factory_name, "camera_source")
            decodebin = self._make_element("decodebin", "camera_decodebin")
            convert = self._make_element("videoconvert", "camera_convert")
            scale = self._make_element("videoscale", "camera_scale")
            rate = self._make_element("videorate", "camera_rate")
            capsfilter = self._make_element("capsfilter", "camera_caps")
            appsink = self._make_element("appsink", "camera_sink")
        except RuntimeError as exc:
            LOGGER.info("Skipping GStreamer source %s: %s", factory_name, exc)
            return False

        self._set_property_if_supported(source, "device-index", self._camera_index)
        self._set_property_if_supported(source, "camera-index", self._camera_index)
        self._set_property_if_supported(source, "index", self._camera_index)

        fps_fraction = Fraction(str(self._target_fps)).limit_denominator(1000)
        capsfilter.set_property(
            "caps",
            self._Gst.Caps.from_string(
                "video/x-raw,format=BGR,"
                f"width={self._frame_width},height={self._frame_height},"
                f"framerate={fps_fraction.numerator}/{fps_fraction.denominator}"
            ),
        )
        appsink.set_property("emit-signals", False)
        appsink.set_property("sync", False)
        appsink.set_property("drop", True)
        appsink.set_property("max-buffers", 1)

        pipeline.add(source)
        pipeline.add(decodebin)
        pipeline.add(convert)
        pipeline.add(scale)
        pipeline.add(rate)
        pipeline.add(capsfilter)
        pipeline.add(appsink)

        if not source.link(decodebin):
            LOGGER.info("Skipping GStreamer source %s because source -> decodebin did not link.", factory_name)
            return False
        if not convert.link(scale) or not scale.link(rate) or not rate.link(capsfilter) or not capsfilter.link(appsink):
            LOGGER.info("Skipping GStreamer source %s because downstream elements did not link.", factory_name)
            return False

        decodebin.connect("pad-added", self._on_decodebin_pad_added, convert)

        state_change = pipeline.set_state(self._Gst.State.PLAYING)
        if state_change == self._Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(self._Gst.State.NULL)
            LOGGER.info("Skipping GStreamer source %s because the pipeline failed to start.", factory_name)
            return False

        self._pipeline = pipeline
        self._appsink = appsink
        self._bus = pipeline.get_bus()
        self._source_factory_name = factory_name
        return True

    def _make_element(self, factory_name: str, element_name: str) -> Any:
        assert self._Gst is not None
        element = self._Gst.ElementFactory.make(factory_name, element_name)
        if element is None:
            raise RuntimeError(f"Failed to create GStreamer element '{factory_name}'.")
        return element

    def _set_property_if_supported(self, element: Any, property_name: str, value: Any) -> None:
        try:
            element.set_property(property_name, value)
        except Exception:
            pass

    def _on_decodebin_pad_added(self, _decodebin: Any, pad: Any, convert: Any) -> None:
        sink_pad = convert.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        pad.link(sink_pad)

    def _read_usable_startup_frame(self) -> np.ndarray | None:
        for _ in range(12):
            self._poll_bus()
            frame = self._pull_frame(timeout_ns=self._sample_timeout_ns())
            if frame is None:
                time.sleep(0.05)
                continue
            if self._is_black_frame(frame):
                time.sleep(0.05)
                continue
            return frame
        return None

    def _pull_frame(self, timeout_ns: int) -> np.ndarray | None:
        self._poll_bus()
        if self._appsink is None:
            return None

        sample = self._appsink.emit("try-pull-sample", timeout_ns)
        if sample is None:
            return None
        return self._sample_to_array(sample)

    def _sample_to_array(self, sample: Any) -> np.ndarray | None:
        assert self._Gst is not None

        buffer = sample.get_buffer()
        caps = sample.get_caps()
        if buffer is None or caps is None or caps.get_size() == 0:
            return None

        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))

        success, map_info = buffer.map(self._Gst.MapFlags.READ)
        if not success:
            return None

        try:
            return np.frombuffer(map_info.data, dtype=np.uint8).reshape((height, width, 3)).copy()
        finally:
            buffer.unmap(map_info)

    def _poll_bus(self) -> None:
        if self._bus is None or self._Gst is None:
            return

        message = self._bus.timed_pop_filtered(
            0,
            self._Gst.MessageType.ERROR | self._Gst.MessageType.EOS,
        )
        if message is None:
            return

        if message.type == self._Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            details = debug or str(error)
            LOGGER.warning("GStreamer camera source %s reported an error: %s", self._source_factory_name, details)
            self.disconnect_source()
        elif message.type == self._Gst.MessageType.EOS:
            LOGGER.warning("GStreamer camera source %s reached EOS.", self._source_factory_name)
            self.disconnect_source()

    def _sample_timeout_ns(self) -> int:
        assert self._Gst is not None
        return max(1, int(self._Gst.SECOND / max(self._target_fps, 1.0)))

    def _is_black_frame(self, frame: np.ndarray) -> bool:
        return bool(frame.size == 0 or not np.any(frame))

    def _build_media_frame(self, frame: np.ndarray) -> MediaFrame:
        self._frame_counter += 1
        timestamp = time.time()
        return MediaFrame(
            frame_id=self._frame_counter,
            timestamp=timestamp,
            image=frame,
            source_name=self._display_name,
        )
