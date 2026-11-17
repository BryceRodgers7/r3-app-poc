"""NV12 as the ingest working format (replaces the BGR chain).

The NDI ingest chain keeps video in NV12, NDI's native YUV layout, instead
of expanding every frame to BGR. These tests pin the receiver's format
handling, the order of the ingest elements, the record branch's caps (which
`qsvjpegenc` can finally link to), and, with real GStreamer, that the one
BT.709 -> BT.601 conversion JPEG needs happens once, at ingest.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from app.config.settings import AppSettings
from app.core.models import FeedDefinition
from app.media.ndi_receiver import (
    DEFAULT_WORKING_FORMAT,
    VALID_WORKING_FORMATS,
    NDIReceiver,
)
from app.media.pipeline_manager import PipelineManager
from app.media.source_factory import build_source_for_feed


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


def _has(*factories: str) -> bool:
    return _GST is not None and all(
        _GST.ElementFactory.find(name) is not None for name in factories
    )


def _receiver(
    working_format: str | None = None,
    width: int = 1280,
    height: int = 720,
    fps: float = 30.0,
) -> NDIReceiver:
    return NDIReceiver(
        source_name="Cam",
        feed_id="ndi_cam",
        ndi_name="HOST (Cam)",
        frame_width=width,
        frame_height=height,
        target_fps=fps,
        working_format=working_format,
    )


def _encode_caps_string(codec: str = "mjpeg") -> str:
    pm = PipelineManager.__new__(PipelineManager)
    pm._recording_codec = codec
    return pm._recording_encode_caps_string()


class WorkingFormatTests(unittest.TestCase):
    def test_default_is_nv12(self) -> None:
        self.assertEqual(DEFAULT_WORKING_FORMAT, "NV12")
        self.assertEqual(_receiver().get_working_format(), "NV12")

    def test_bgr_is_still_available(self) -> None:
        self.assertIn("BGR", VALID_WORKING_FORMATS)
        self.assertEqual(_receiver("BGR").get_working_format(), "BGR")

    def test_format_is_case_insensitive(self) -> None:
        self.assertEqual(_receiver("nv12").get_working_format(), "NV12")

    def test_unknown_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _receiver("I420")

    def test_nv12_needs_an_even_frame_size(self) -> None:
        with self.assertRaises(ValueError):
            _receiver("NV12", width=1281, height=720)
        with self.assertRaises(ValueError):
            _receiver("NV12", width=1280, height=721)

    def test_bgr_accepts_an_odd_frame_size(self) -> None:
        self.assertEqual(_receiver("BGR", width=641, height=361).get_working_format(), "BGR")

    def test_working_caps_carry_format_size_and_rate(self) -> None:
        self.assertEqual(
            _receiver(width=1920, height=1080, fps=30.0).working_caps_string(),
            "video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1",
        )

    def test_working_caps_do_not_pin_colorimetry(self) -> None:
        # The record branch's BT.601 pin reaches the ingest conversion through
        # caps negotiation; pinning it here would hard-wire the choice.
        self.assertNotIn("colorimetry", _receiver().working_caps_string())

    def test_bgr_working_caps(self) -> None:
        self.assertIn("format=BGR", _receiver("BGR").working_caps_string())


class SourceFactoryTests(unittest.TestCase):
    def _feed(self) -> FeedDefinition:
        return FeedDefinition(
            feed_id="ndi_cam",
            display_name="Cam",
            source_kind="ndi",
            ndi_name="HOST (Cam)",
        )

    def test_factory_passes_the_configured_format(self) -> None:
        settings = AppSettings()
        settings.media_working_pixel_format = "BGR"
        source = build_source_for_feed(settings, self._feed())
        self.assertEqual(source.get_working_format(), "BGR")

    def test_factory_defaults_to_nv12(self) -> None:
        source = build_source_for_feed(AppSettings(), self._feed())
        self.assertEqual(source.get_working_format(), "NV12")


class RecordEncodeCapsTests(unittest.TestCase):
    def test_mjpeg_caps_are_nv12_bt601(self) -> None:
        self.assertEqual(
            _encode_caps_string("mjpeg"), "video/x-raw,format=NV12,colorimetry=bt601"
        )

    @unittest.skipUnless(
        _has("qsvjpegenc", "capsfilter"), "qsvjpegenc plugin not installed"
    )
    def test_qsvjpegenc_links_to_the_record_caps(self) -> None:
        # The bug this fixes: `qsvjpegenc` takes NV12 / YUY2 / BGRA, not the
        # I420 the record branch used to pin, so it never linked and every
        # start fell back to software `jpegenc`.
        Gst = _GST
        encoder = Gst.ElementFactory.make("qsvjpegenc", None)
        if encoder is None:
            self.skipTest("qsvjpegenc cannot be created on this machine")
        caps = Gst.ElementFactory.make("capsfilter", None)
        caps.set_property("caps", Gst.Caps.from_string(_encode_caps_string("mjpeg")))
        pipeline = Gst.Pipeline.new("qsv-link")
        pipeline.add(caps)
        pipeline.add(encoder)
        self.assertTrue(caps.link(encoder))


class _FakeBuffer:
    """Just the parts of a `Gst.Buffer` the encoder probe touches."""

    def __init__(self, delta: bool, pts: int | None = None) -> None:
        self.delta = delta
        self.pts = pts if pts is not None else 2**64 - 1

    def has_flags(self, _flags: object) -> bool:
        return self.delta

    def unset_flags(self, _flags: object) -> None:
        self.delta = False


class EncoderKeyframeFlagTests(unittest.TestCase):
    """`qsvjpegenc` marks every MJPEG frame a delta unit; splitmuxsink then never rotates."""

    def _probe(self, needs_fix: bool, buffer: _FakeBuffer) -> None:
        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = SimpleNamespace(
            PadProbeType=SimpleNamespace(EVENT_DOWNSTREAM=2),
            PadProbeReturn=SimpleNamespace(OK="ok"),
            BufferFlags=SimpleNamespace(DELTA_UNIT="delta"),
            CLOCK_TIME_NONE=2**64 - 1,
        )
        pm._record_encoder_needs_keyframe_flag = needs_fix
        info = SimpleNamespace(type=1, get_buffer=lambda: buffer)
        self.assertEqual(pm._on_record_encoder_buffer_probe(None, info, None), "ok")

    def test_qsvjpegenc_is_listed_and_jpegenc_is_not(self) -> None:
        from app.media.encoder_factory import ENCODERS_FLAGGING_DELTA_UNITS

        self.assertIn("qsvjpegenc", ENCODERS_FLAGGING_DELTA_UNITS)
        self.assertNotIn("jpegenc", ENCODERS_FLAGGING_DELTA_UNITS)

    def test_probe_clears_the_flag_for_those_encoders(self) -> None:
        buffer = _FakeBuffer(delta=True)
        self._probe(True, buffer)
        self.assertFalse(buffer.delta)

    def test_probe_leaves_other_encoders_alone(self) -> None:
        buffer = _FakeBuffer(delta=True)
        self._probe(False, buffer)
        self.assertTrue(buffer.delta)

    def test_new_pipeline_manager_starts_without_the_fix(self) -> None:
        self.assertFalse(
            getattr(PipelineManager.__new__(PipelineManager), "_record_encoder_needs_keyframe_flag", False)
        )

    @unittest.skipUnless(
        _has("qsvjpegenc", "splitmuxsink", "matroskamux", "videotestsrc"),
        "qsvjpegenc / splitmuxsink not available",
    )
    def test_splitmuxsink_rotates_with_the_fix_and_not_without(self) -> None:
        import glob
        import os
        import tempfile

        Gst = _GST

        def segment_count(fix: bool) -> int:
            with tempfile.TemporaryDirectory() as folder:
                location = os.path.join(folder, "seg_%05d.mkv").replace("\\", "/")
                pipeline = Gst.parse_launch(
                    "videotestsrc is-live=true num-buffers=75 pattern=ball "
                    "! video/x-raw,format=NV12,width=640,height=360,framerate=30/1,colorimetry=bt601 "
                    f"! qsvjpegenc name=enc ! splitmuxsink muxer=matroskamux "
                    f"max-size-time=1000000000 async-handling=true location=\"{location}\""
                )
                if pipeline.get_by_name("enc") is None:
                    self.skipTest("qsvjpegenc cannot be created on this machine")
                pm = PipelineManager.__new__(PipelineManager)
                pm._Gst = Gst
                pm._record_encoder_needs_keyframe_flag = fix
                pm._record_encoder_segment = None
                pm._segment_bookkeeper_or_new = lambda: SimpleNamespace(note_buffer=lambda _t: None)
                pipeline.get_by_name("enc").get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    pm._on_record_encoder_buffer_probe,
                    None,
                )
                pipeline.set_state(Gst.State.PLAYING)
                message = pipeline.get_bus().timed_pop_filtered(
                    30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
                )
                pipeline.set_state(Gst.State.NULL)
                self.assertIsNotNone(message)
                self.assertEqual(message.type, Gst.MessageType.EOS)
                return len(glob.glob(os.path.join(folder, "seg_*.mkv")))

        self.assertEqual(segment_count(fix=False), 1)  # the bug: one file, never rotated
        self.assertGreaterEqual(segment_count(fix=True), 2)


@unittest.skipUnless(_has("ndisrc", "ndisrcdemux"), "gst-plugins-rs NDI not installed")
class NdiChainOrderTests(unittest.TestCase):
    def _linked_chain(self, working_format: str | None = None):
        receiver = _receiver(working_format)
        self.assertTrue(receiver.connect_source())
        chain = receiver.build_native_chain(_GST)
        self.assertIsNotNone(chain)
        pipeline = _GST.Pipeline.new("ndi-chain")
        for element in chain["elements"]:
            pipeline.add(element)
        self.assertTrue(receiver.link_native_chain_static())
        return receiver, chain, pipeline

    @staticmethod
    def _by_factory(chain: dict, factory: str):
        for element in chain["elements"]:
            if element.get_factory().get_name() == factory:
                return element
        raise AssertionError(f"{factory} missing from chain")

    def test_video_chain_scales_before_it_converts(self) -> None:
        _receiver_, chain, _pipeline = self._linked_chain()
        scale = self._by_factory(chain, "videoscale")
        convert = self._by_factory(chain, "videoconvert")
        rate = self._by_factory(chain, "videorate")
        caps = self._by_factory(chain, "capsfilter")
        self.assertEqual(scale.get_static_pad("src").get_peer().get_parent(), convert)
        self.assertEqual(convert.get_static_pad("src").get_peer().get_parent(), rate)
        self.assertEqual(rate.get_static_pad("src").get_peer().get_parent(), caps)

    def test_ingest_probe_pad_is_still_videorates_input(self) -> None:
        _receiver_, chain, _pipeline = self._linked_chain()
        rate = self._by_factory(chain, "videorate")
        self.assertEqual(chain["ingest_frame_pad"], rate.get_static_pad("sink"))

    def test_capsfilter_holds_the_working_format(self) -> None:
        for working_format in ("NV12", "BGR"):
            with self.subTest(working_format=working_format):
                _receiver_, chain, _pipeline = self._linked_chain(working_format)
                caps = self._by_factory(chain, "capsfilter").get_property("caps")
                self.assertEqual(
                    caps.get_structure(0).get_string("format"), working_format
                )

    def test_demux_video_pad_links_to_the_scaler(self) -> None:
        receiver = _receiver()
        scale, audio = mock.MagicMock(), mock.MagicMock()
        scale_sink = scale.get_static_pad.return_value
        scale_sink.is_linked.return_value = False
        pad = mock.MagicMock()
        pad.get_name.return_value = "video"
        pad.get_current_caps.return_value = None
        pad.query_caps.return_value = None
        receiver._on_ndisrcdemux_pad_added(None, pad, (scale, audio))
        pad.link.assert_called_once_with(scale_sink)
        scale.sync_state_with_parent.assert_called_once()
        audio.get_static_pad.assert_not_called()


@unittest.skipUnless(
    _has("videotestsrc", "videoscale", "videoconvert", "videorate", "capsfilter",
         "tee", "queue", "jpegenc", "appsink"),
    "core GStreamer elements not available",
)
class NegotiatedChainTests(unittest.TestCase):
    """The app's tee layout, fed by a test pattern instead of NDI."""

    SOURCE = "video/x-raw,format=UYVY,width=640,height=360,framerate=30/1,colorimetry=bt709"

    def _run(self, working_format: str) -> tuple[dict, list[bytes]]:
        Gst = _GST
        receiver = _receiver(working_format, width=320, height=180, fps=30.0)
        pipeline = Gst.parse_launch(
            f"videotestsrc num-buffers=20 pattern=smpte ! {self.SOURCE} "
            f"! videoscale ! videoconvert name=ingest_convert ! videorate "
            f"! capsfilter caps=\"{receiver.working_caps_string()}\" ! tee name=t "
            f"t. ! queue ! videoconvert name=preview_convert ! fakesink sync=false "
            f"t. ! queue ! videoconvert name=record_convert "
            f"! capsfilter name=encode_caps caps=\"{_encode_caps_string('mjpeg')}\" "
            f"! jpegenc ! appsink name=out sync=false max-buffers=100 drop=false"
        )
        sink = pipeline.get_by_name("out")
        self.assertEqual(
            pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE, True
        )
        frames: list[bytes] = []
        try:
            while True:
                sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
                if sample is None:
                    break
                buffer = sample.get_buffer()
                ok, info = buffer.map(Gst.MapFlags.READ)
                self.assertTrue(ok)
                frames.append(bytes(info.data))
                buffer.unmap(info)
            caps = {}
            for name in ("ingest_convert", "preview_convert", "record_convert"):
                element = pipeline.get_by_name(name)
                caps[name] = {
                    side: element.get_static_pad(side).get_current_caps().get_structure(0)
                    for side in ("sink", "src")
                }
        finally:
            pipeline.set_state(Gst.State.NULL)
        return caps, frames

    def test_nv12_chain_converts_colour_once_at_ingest(self) -> None:
        caps, frames = self._run("NV12")
        ingest = caps["ingest_convert"]
        self.assertEqual(ingest["sink"].get_string("colorimetry"), "bt709")
        self.assertEqual(ingest["src"].get_string("format"), "NV12")
        # JPEG needs BT.601 (invariant 1): the ingest converter does that
        # conversion, because negotiation carries the record pin upstream ...
        self.assertEqual(ingest["src"].get_string("colorimetry"), "bt601")
        # ... which leaves the record branch's converter a pass-through.
        record = caps["record_convert"]
        self.assertEqual(record["sink"].to_string(), record["src"].to_string())

    def test_nv12_chain_records_jpeg_frames(self) -> None:
        _caps, frames = self._run("NV12")
        self.assertGreaterEqual(len(frames), 15)
        for frame in frames:
            self.assertEqual(frame[:3], b"\xff\xd8\xff")

    def test_bgr_fallback_still_records_jpeg_frames(self) -> None:
        caps, frames = self._run("BGR")
        self.assertEqual(caps["ingest_convert"]["src"].get_string("format"), "BGR")
        # Here the record branch's converter does the BGR -> NV12/BT.601 step.
        record = caps["record_convert"]
        self.assertEqual(record["src"].get_string("format"), "NV12")
        self.assertEqual(record["src"].get_string("colorimetry"), "bt601")
        self.assertGreaterEqual(len(frames), 15)
        for frame in frames:
            self.assertEqual(frame[:3], b"\xff\xd8\xff")


if __name__ == "__main__":
    unittest.main()
