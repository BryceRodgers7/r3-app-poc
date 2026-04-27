"""GStreamer-backed muxed audio/video file writer."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import importlib
import logging
from pathlib import Path
from typing import Any

import numpy as np

from app.core.models import AudioChunk, AudioFormat, MediaFrame
from app.core.telemetry import time_block

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class MuxedWriterInfo:
    """Details about the muxed media writer selected for an output file."""

    container: str
    video_encoder: str
    audio_encoder: str | None
    output_path: Path


class MuxedMediaWriter:
    """Write raw video frames and PCM audio chunks into one muxed media file."""

    def __init__(
        self,
        output_path: Path,
        *,
        fps_hint: float,
        audio_format: AudioFormat | None = None,
        audio_bitrate: int = 128_000,
    ) -> None:
        self._requested_output_path = output_path
        self._fps_hint = max(fps_hint, 1.0)
        self._audio_format = audio_format
        self._audio_bitrate = max(audio_bitrate, 16_000)
        self._Gst: Any | None = None
        self._pipeline: Any | None = None
        self._video_appsrc: Any | None = None
        self._audio_appsrc: Any | None = None
        self._info: MuxedWriterInfo | None = None
        self._video_start_timestamp: float | None = None
        self._audio_start_timestamp: float | None = None
        self._video_frame_count = 0
        self._audio_bytes = 0
        self._pending_audio_chunks: list[AudioChunk] = []

    @property
    def info(self) -> MuxedWriterInfo | None:
        """Return selected writer details once the pipeline has opened."""
        return self._info

    @property
    def output_path(self) -> Path:
        """Return the actual output path, including fallback suffix changes."""
        return self._info.output_path if self._info is not None else self._requested_output_path

    @property
    def video_frame_count(self) -> int:
        """Return the number of video frames pushed to the writer."""
        return self._video_frame_count

    @property
    def audio_bytes(self) -> int:
        """Return the number of audio bytes pushed to the writer."""
        return self._audio_bytes

    def write_frame(self, frame: MediaFrame) -> None:
        """Push one video frame into the muxed output."""
        with time_block("segment_write_video"):
            if self._pipeline is None:
                self._open(frame)
                self._flush_pending_audio()
            if self._video_appsrc is None:
                return

            Gst = self._Gst
            assert Gst is not None

            frame_array = np.ascontiguousarray(frame.image_bgr)
            gst_buffer = Gst.Buffer.new_allocate(None, frame_array.nbytes, None)
            gst_buffer.fill(0, frame_array.tobytes())
            if self._video_start_timestamp is None:
                self._video_start_timestamp = frame.timestamp
            running_timestamp = max(0.0, frame.timestamp - self._video_start_timestamp)
            gst_buffer.pts = int(running_timestamp * Gst.SECOND)
            gst_buffer.dts = gst_buffer.pts
            gst_buffer.duration = int(Gst.SECOND / self._fps_hint)
            flow_return = self._video_appsrc.emit("push-buffer", gst_buffer)
            if flow_return != Gst.FlowReturn.OK:
                raise RuntimeError(f"GStreamer video writer push failed: {flow_return}")
            self._video_frame_count += 1

    def write_audio_chunk(self, chunk: AudioChunk) -> None:
        """Push one PCM audio chunk into the muxed output."""
        with time_block("segment_write_audio"):
            if self._pipeline is None:
                self._audio_format = chunk.format
                self._pending_audio_chunks.append(chunk)
                while len(self._pending_audio_chunks) > 128:
                    self._pending_audio_chunks.pop(0)
                return
            if self._audio_appsrc is None:
                return

            Gst = self._Gst
            assert Gst is not None

            gst_buffer = Gst.Buffer.new_allocate(None, len(chunk.data), None)
            gst_buffer.fill(0, chunk.data)
            if self._audio_start_timestamp is None:
                self._audio_start_timestamp = chunk.timestamp
            running_timestamp = max(0.0, chunk.timestamp - self._audio_start_timestamp)
            gst_buffer.pts = int(running_timestamp * Gst.SECOND)
            gst_buffer.dts = gst_buffer.pts
            gst_buffer.duration = int(chunk.duration_seconds * Gst.SECOND)
            flow_return = self._audio_appsrc.emit("push-buffer", gst_buffer)
            if flow_return != Gst.FlowReturn.OK:
                raise RuntimeError(f"GStreamer audio writer push failed: {flow_return}")
            self._audio_bytes += len(chunk.data)

    def _flush_pending_audio(self) -> None:
        pending = list(self._pending_audio_chunks)
        self._pending_audio_chunks.clear()
        for chunk in pending:
            self.write_audio_chunk(chunk)

    def close(self) -> None:
        """Finalize and close the muxed output."""
        if self._pipeline is None or self._Gst is None:
            return

        for appsrc in (self._video_appsrc, self._audio_appsrc):
            if appsrc is not None:
                try:
                    appsrc.emit("end-of-stream")
                except Exception:
                    LOGGER.debug("Failed to send EOS to writer appsrc", exc_info=True)

        bus = self._pipeline.get_bus()
        if bus is not None:
            bus.timed_pop_filtered(
                int(5 * self._Gst.SECOND),
                self._Gst.MessageType.EOS | self._Gst.MessageType.ERROR,
            )
        self._pipeline.set_state(self._Gst.State.NULL)
        self._pipeline = None
        self._video_appsrc = None
        self._audio_appsrc = None

    def _open(self, frame: MediaFrame) -> None:
        self._ensure_gstreamer_loaded()
        Gst = self._Gst
        assert Gst is not None

        height, width = frame.image_bgr.shape[:2]
        profile = self._select_profile()
        output_path = self._requested_output_path
        if profile["container"] != "mp4":
            output_path = output_path.with_suffix(".mkv")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        pipeline = Gst.parse_launch(self._build_pipeline_description(profile, width, height))
        file_sink = pipeline.get_by_name("file_sink")
        if file_sink is None:
            raise RuntimeError("Muxed writer pipeline is missing filesink.")
        file_sink.set_property("location", str(output_path))

        state_change = pipeline.set_state(Gst.State.PLAYING)
        if state_change == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Failed to start muxed media writer pipeline.")

        self._pipeline = pipeline
        self._video_appsrc = pipeline.get_by_name("video_src")
        self._audio_appsrc = pipeline.get_by_name("audio_src")
        self._info = MuxedWriterInfo(
            container=profile["container"],
            video_encoder=profile["video_encoder"],
            audio_encoder=profile.get("audio_encoder"),
            output_path=output_path,
        )

    def _ensure_gstreamer_loaded(self) -> None:
        if self._Gst is not None:
            return
        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gst_module = importlib.import_module("gi.repository.Gst")
        gst_module.init(None)
        self._Gst = gst_module

    def _select_profile(self) -> dict[str, str | None]:
        Gst = self._Gst
        assert Gst is not None

        h264_encoder = self._first_available(("x264enc", "openh264enc"))
        aac_encoder = self._first_available(("avenc_aac", "voaacenc", "faac"))
        if Gst.ElementFactory.find("mp4mux") is not None and h264_encoder is not None and aac_encoder is not None:
            return {
                "container": "mp4",
                "muxer": "mp4mux",
                "video_encoder": h264_encoder,
                "video_parser": "h264parse",
                "audio_encoder": aac_encoder,
                "audio_parser": "aacparse",
            }

        if Gst.ElementFactory.find("matroskamux") is not None and Gst.ElementFactory.find("jpegenc") is not None:
            return {
                "container": "matroska",
                "muxer": "matroskamux",
                "video_encoder": "jpegenc",
                "video_parser": None,
                "audio_encoder": None,
                "audio_parser": None,
            }

        raise RuntimeError("No supported muxed media writer profile is available.")

    def _first_available(self, names: tuple[str, ...]) -> str | None:
        Gst = self._Gst
        assert Gst is not None
        for name in names:
            if Gst.ElementFactory.find(name) is not None:
                return name
        return None

    def _build_pipeline_description(self, profile: dict[str, str | None], width: int, height: int) -> str:
        audio_format = self._audio_format or AudioFormat()
        fps = Fraction(str(self._fps_hint)).limit_denominator(1000)
        muxer = profile["muxer"]
        video_encoder = profile["video_encoder"]
        video_parser = profile.get("video_parser")
        audio_encoder = profile.get("audio_encoder")
        audio_parser = profile.get("audio_parser")

        video_chain = (
            "appsrc name=video_src is-live=true format=time block=true do-timestamp=false "
            f"caps=video/x-raw,format=BGR,width={width},height={height},framerate={fps.numerator}/{fps.denominator} "
            f"! queue ! videoconvert ! {video_encoder} "
        )
        if video_encoder == "x264enc":
            video_chain += "tune=zerolatency speed-preset=veryfast "
        if video_parser is not None:
            video_chain += f"! {video_parser} "
        video_chain += "! mux. "

        audio_chain = (
            "appsrc name=audio_src is-live=true format=time block=true do-timestamp=false "
            f"caps=audio/x-raw,format={audio_format.sample_format},rate={audio_format.sample_rate},"
            f"channels={audio_format.channels},layout=interleaved "
            "! queue ! audioconvert ! audioresample "
        )
        if audio_encoder is not None:
            audio_chain += f"! {audio_encoder} "
            if audio_encoder == "avenc_aac":
                audio_chain += f"bitrate={self._audio_bitrate} "
            if audio_parser is not None:
                audio_chain += f"! {audio_parser} "
        audio_chain += "! mux. "

        return f"{muxer} name=mux ! filesink name=file_sink {video_chain}{audio_chain}"
