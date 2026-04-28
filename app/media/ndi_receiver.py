"""Native GStreamer NDI source bin.

Phase 3.A.2 converted NDI ingest from `python_push` (numpy round-trip
through an internal appsink + Python frame loop) to `native` mode: this
class now constructs a `Gst.Bin` whose video and audio src ghost pads are
linked directly into `PipelineManager`'s tees. Frames never enter Python
on the hot path.

The lifecycle pieces (`connect_source` / `disconnect_source` /
`is_connected`) only validate plugin availability and configuration; the
bin's actual playback is owned by the parent pipeline.
"""

from __future__ import annotations

from fractions import Fraction
import importlib
import logging
from typing import Any

from app.core.models import AudioChunk, AudioFormat, IngestTelemetry, MediaFrame
from app.media.source_interface import PipelineMode, SourceInterface

LOGGER = logging.getLogger(__name__)


class NDIReceiver(SourceInterface):
    """Native NDI source providing a configured `Gst.Bin` to `PipelineManager`."""

    @property
    def pipeline_mode(self) -> PipelineMode:
        return PipelineMode.NATIVE

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
        self._audio_format = AudioFormat()
        self._ingest_telemetry: IngestTelemetry | None = None
        self._native_source: Any | None = None
        self._native_demux: Any | None = None
        self._native_video_convert: Any | None = None
        self._native_video_caps: Any | None = None
        self._native_audio_convert: Any | None = None

    def connect_source(self) -> bool:
        """Verify the `ndisrc` plugin and `ndi_name` are available."""
        if self._connected:
            return True
        try:
            gi = importlib.import_module("gi")
            gi.require_version("Gst", "1.0")
            gst_module = importlib.import_module("gi.repository.Gst")
            gst_module.init(None)
        except Exception as exc:
            self._status_message = f"GStreamer NDI support unavailable: {exc}"
            LOGGER.info(self._status_message)
            return False

        if gst_module.ElementFactory.find("ndisrc") is None:
            self._status_message = "GStreamer plugin 'ndisrc' is not installed."
            LOGGER.info(self._status_message)
            return False

        if not self._ndi_name or not str(self._ndi_name).strip():
            self._status_message = (
                f"NDI feed {self._feed_id!r}: ndi_name is required."
            )
            LOGGER.info(self._status_message)
            return False

        self._connected = True
        self._status_message = None
        self._ingest_telemetry = IngestTelemetry(
            target_width=self._frame_width,
            target_height=self._frame_height,
            target_fps=self._target_fps,
            raw_width=None,
            raw_height=None,
            raw_fps=None,
        )
        return True

    def disconnect_source(self) -> None:
        """Release administrative state. The bin is owned by `PipelineManager`."""
        self._connected = False
        self._ingest_telemetry = None

    def is_connected(self) -> bool:
        return self._connected

    def get_display_name(self) -> str:
        return self._source_name

    def get_feed_id(self) -> str:
        return self._feed_id

    def create_pipeline_fragment(self) -> str:
        return "native-gstreamer-ndi-source"

    def supports_embedded_audio(self) -> bool:
        return True

    def get_audio_format(self) -> AudioFormat | None:
        return self._audio_format

    def read_frame(self) -> MediaFrame | None:
        # Native sources never deliver frames into Python; the parent pipeline
        # links the bin's video src ghost pad straight into the per-feed tee.
        return None

    def read_audio_chunk(self) -> AudioChunk | None:
        return None

    def get_frame_size(self) -> tuple[int, int]:
        return self._frame_width, self._frame_height

    def get_nominal_fps(self) -> float:
        return self._target_fps

    def get_status_message(self) -> str | None:
        return self._status_message

    def get_ingest_telemetry(self) -> IngestTelemetry | None:
        return self._ingest_telemetry

    def build_native_chain(self, gst_module: Any) -> dict | None:
        """Construct the native NDI element chain.

        Returns a dict::

            {
                "elements": [Gst.Element, ...],   # add each to the parent pipeline
                "video_src_pad": Gst.Pad,         # link to source tee.sink
                "audio_src_pad": Gst.Pad,         # link to audio tee.sink
            }

        or None on failure. Layout (matches the canonical gst-plugins-rs
        `gst-launch` example)::

            ndisrc -> ndisrcdemux name=demux
              demux.video -> videoconvert -> capsfilter (BGR) -> [video_src_pad]
              demux.audio -> audioconvert -> [audio_src_pad]

        Earlier 3.A.2 versions wrapped these elements in a `Gst.Bin` with
        ghost pads for cleanliness. That broke buffer flow on Windows +
        gst-plugins-rs: caps events traversed the ghost pad boundary but
        buffers did not, despite explicit `set_active(True)`. Putting
        elements directly in the parent pipeline matches the working
        gst-launch pattern and avoids the issue entirely. The encapsulation
        cost is small — `disconnect_source()` doesn't need to remove
        elements one-by-one because the parent pipeline tears them all down
        when it transitions to NULL.

        `ndisrcdemux`'s `video` and `audio` pads are "sometimes" pads — they
        appear once the corresponding stream is detected, so the
        `pad-added` handler does the linking.
        """
        if not self._connected:
            return None
        Gst = gst_module
        try:
            source = Gst.ElementFactory.make("ndisrc", f"ndisrc_{self._feed_id}")
            demux = Gst.ElementFactory.make(
                "ndisrcdemux", f"ndidemux_{self._feed_id}"
            )
            video_convert = Gst.ElementFactory.make(
                "videoconvert", f"ndivconvert_{self._feed_id}"
            )
            video_caps = Gst.ElementFactory.make(
                "capsfilter", f"ndivcaps_{self._feed_id}"
            )
            audio_convert = Gst.ElementFactory.make(
                "audioconvert", f"ndiaconvert_{self._feed_id}"
            )
        except Exception as exc:
            self._status_message = f"Failed to construct NDI elements: {exc}"
            LOGGER.warning(self._status_message)
            return None

        elements = [source, demux, video_convert, video_caps, audio_convert]
        if any(element is None for element in elements):
            self._status_message = "NDI chain: at least one GStreamer element is missing."
            LOGGER.warning(self._status_message)
            return None

        try:
            source.set_property("ndi-name", self._ndi_name)
        except Exception as exc:
            self._status_message = f"Failed to set ndi-name: {exc}"
            LOGGER.warning(self._status_message)
            return None

        video_caps.set_property(
            "caps", Gst.Caps.from_string("video/x-raw,format=BGR")
        )

        # Static-side links (ndisrc → demux, videoconvert → capsfilter). The
        # demux→videoconvert.sink and demux→audioconvert.sink links are
        # dynamic via pad-added. Note: source.link / videoconvert.link can
        # only succeed AFTER all elements are added to the same parent (the
        # parent pipeline). We therefore defer linking to the caller, who
        # adds the elements first. Return references the caller needs.
        demux.connect(
            "pad-added", self._on_ndisrcdemux_pad_added, (video_convert, audio_convert)
        )

        video_src_pad = video_caps.get_static_pad("src")
        audio_src_pad = audio_convert.get_static_pad("src")
        if video_src_pad is None or audio_src_pad is None:
            self._status_message = "NDI chain: capsfilter / audioconvert src pad is missing."
            return None

        LOGGER.info(
            "NDI chain built for feed_id=%s ndi_name=%r", self._feed_id, self._ndi_name
        )
        # Stash references for the caller's `link_native_chain_static`.
        self._native_source = source
        self._native_demux = demux
        self._native_video_convert = video_convert
        self._native_video_caps = video_caps
        self._native_audio_convert = audio_convert
        return {
            "elements": elements,
            "video_src_pad": video_src_pad,
            "audio_src_pad": audio_src_pad,
        }

    def link_native_chain_static(self) -> bool:
        """Link the static-side connections after caller adds elements to pipeline.

        `ndisrc → ndisrcdemux` and `videoconvert → capsfilter` can only be
        linked once both ends share a parent (the pipeline). The caller adds
        the elements to the pipeline, then calls this. The dynamic
        `demux.video → videoconvert.sink` link is done by the pad-added
        handler installed in `build_native_chain`.
        """
        if self._native_source is None or self._native_demux is None:
            return False
        if not self._native_source.link(self._native_demux):
            self._status_message = "Failed to link ndisrc into ndisrcdemux."
            return False
        if not self._native_video_convert.link(self._native_video_caps):
            self._status_message = "Failed to link videoconvert into video capsfilter."
            return False
        return True

    def _on_ndisrcdemux_pad_added(
        self, _demux: Any, pad: Any, targets: tuple[Any, Any]
    ) -> None:
        """Link demux.video → video_convert.sink and demux.audio → audio_convert.sink."""
        video_convert, audio_convert = targets
        pad_name = pad.get_name() or ""
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_name = ""
        if caps is not None and caps.get_size() > 0:
            caps_name = caps.get_structure(0).get_name()
        LOGGER.info(
            "NDI demux pad-added: feed_id=%s pad=%r caps=%r",
            self._feed_id,
            pad_name,
            caps_name,
        )
        if pad_name.startswith("video") or caps_name.startswith("video/"):
            target = video_convert
            target_label = "video_convert.sink"
        elif pad_name.startswith("audio") or caps_name.startswith("audio/"):
            target = audio_convert
            target_label = "audio_convert.sink"
        else:
            LOGGER.warning(
                "NDI demux pad-added: feed_id=%s unrecognized pad=%r caps=%r",
                self._feed_id,
                pad_name,
                caps_name,
            )
            return
        sink_pad = target.get_static_pad("sink")
        if sink_pad is None:
            LOGGER.warning(
                "NDI demux pad-added: feed_id=%s %s missing", self._feed_id, target_label
            )
            return
        if sink_pad.is_linked():
            return
        link_result = pad.link(sink_pad)
        # When pad-added fires after the parent pipeline is already PLAYING,
        # the freshly-linked downstream element needs to sync state with the
        # parent before it will accept buffers. Without this, videoconvert
        # can sit in NULL/READY while everything else runs.
        try:
            target.sync_state_with_parent()
        except Exception as exc:
            LOGGER.warning(
                "NDI demux pad-added: feed_id=%s sync_state_with_parent failed: %s",
                self._feed_id,
                exc,
            )
        LOGGER.info(
            "NDI demux pad-added: feed_id=%s linked pad=%r -> %s result=%s",
            self._feed_id,
            pad_name,
            target_label,
            link_result,
        )
