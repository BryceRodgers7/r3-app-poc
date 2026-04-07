"""Native GStreamer-backed NDI source."""

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


class NDIReceiver(SourceInterface):
    """Capture frames from an NDI source through GStreamer when available."""

    def __init__(
        self,
        source_name: str,
        feed_id: str,
        ndi_name: str | None,
        frame_width: int,
        frame_height: int,
        target_fps: float,
    ) -> None:
        self._source_name = source_name
        self._feed_id = feed_id
        self._ndi_name = ndi_name
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._target_fps = max(target_fps, 1.0)
        self._connected = False
        self._status_message: str | None = None
        self._frame_counter = 0

        self._Gst: Any | None = None
        self._pipeline: Any | None = None
        self._appsink: Any | None = None
        self._bus: Any | None = None

    def connect_source(self) -> bool:
        """Connect to an NDI source via the `ndisrc` GStreamer plugin."""
        if self._connected:
            return True

        try:
            self._ensure_gstreamer_loaded()
        except Exception as exc:
            self._status_message = f"GStreamer NDI support unavailable: {exc}"
            LOGGER.info(self._status_message)
            return False

        assert self._Gst is not None
        if self._Gst.ElementFactory.find("ndisrc") is None:
            self._status_message = "GStreamer plugin 'ndisrc' is not installed."
            LOGGER.info(self._status_message)
            return False

        try:
            pipeline = self._Gst.Pipeline.new(f"ndi-source-{self._feed_id}")
            source = self._make_element("ndisrc", "ndi_source")
            decodebin = self._make_element("decodebin", "ndi_decodebin")
            convert = self._make_element("videoconvert", "ndi_convert")
            scale = self._make_element("videoscale", "ndi_scale")
            rate = self._make_element("videorate", "ndi_rate")
            capsfilter = self._make_element("capsfilter", "ndi_caps")
            appsink = self._make_element("appsink", "ndi_sink")
        except RuntimeError as exc:
            self._status_message = str(exc)
            LOGGER.warning("Failed to create NDI pipeline: %s", exc)
            return False

        if self._ndi_name:
            self._set_property_if_supported(source, "ndi-name", self._ndi_name)
            self._set_property_if_supported(source, "source-name", self._ndi_name)
            self._set_property_if_supported(source, "name", self._ndi_name)

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
            self._status_message = "Failed to link NDI source into decodebin."
            return False
        if not convert.link(scale) or not scale.link(rate) or not rate.link(capsfilter) or not capsfilter.link(appsink):
            self._status_message = "Failed to link the NDI processing path."
            return False

        decodebin.connect("pad-added", self._on_decodebin_pad_added, convert)

        state_change = pipeline.set_state(self._Gst.State.PLAYING)
        if state_change == self._Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(self._Gst.State.NULL)
            self._status_message = "Failed to start the NDI pipeline."
            return False

        self._pipeline = pipeline
        self._appsink = appsink
        self._bus = pipeline.get_bus()
        self._connected = True
        self._status_message = None
        return True

    def disconnect_source(self) -> None:
        """Disconnect the current source."""
        if self._pipeline is not None and self._Gst is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
        self._pipeline = None
        self._appsink = None
        self._bus = None
        self._connected = False

    def is_connected(self) -> bool:
        """Return the current connection flag."""
        return self._connected

    def get_display_name(self) -> str:
        """Return the configured source name."""
        return self._source_name

    def get_feed_id(self) -> str:
        """Return the configured feed identifier."""
        return self._feed_id

    def create_pipeline_fragment(self) -> str:
        """Describe the native NDI source fragment."""
        return "native-gstreamer-ndi-source"

    def read_frame(self) -> MediaFrame | None:
        """Read the next frame from the NDI appsink."""
        if not self._connected or self._appsink is None:
            return None

        frame = self._pull_frame(timeout_ns=self._sample_timeout_ns())
        if frame is None:
            return None

        self._frame_counter += 1
        return MediaFrame(
            frame_id=self._frame_counter,
            timestamp=time.time(),
            image=frame,
            source_name=self._source_name,
            feed_id=self._feed_id,
        )

    def get_frame_size(self) -> tuple[int, int]:
        """Return the requested output size."""
        return self._frame_width, self._frame_height

    def get_nominal_fps(self) -> float:
        """Return the target NDI frame rate."""
        return self._target_fps

    def get_status_message(self) -> str | None:
        """Return the current non-fatal source status."""
        return self._status_message

    def _ensure_gstreamer_loaded(self) -> None:
        if self._Gst is not None:
            return

        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gst_module = importlib.import_module("gi.repository.Gst")
        gst_module.init(None)
        self._Gst = gst_module

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
            self._status_message = debug or str(error)
            self.disconnect_source()
        elif message.type == self._Gst.MessageType.EOS:
            self._status_message = "NDI source reached EOS."
            self.disconnect_source()

    def _sample_timeout_ns(self) -> int:
        assert self._Gst is not None
        return max(1, int(self._Gst.SECOND / max(self._target_fps, 1.0)))
