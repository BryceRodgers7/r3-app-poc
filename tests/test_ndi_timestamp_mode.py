"""NDI `timestamp-mode` wiring and the ingest-head telemetry it relies on.

See docs/NDI_LOW_FPS_INVESTIGATION.md: ndisrc's default `auto` mode
clumps PTS for senders with coarse timecodes, and `videorate` turns
that into mostly-duplicate output. These tests pin the receiver
default, the per-feed override, and the counters that make the
failure visible in the telemetry log.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.core.telemetry import FeedMetrics
from app.media.ndi_receiver import DEFAULT_NDI_TIMESTAMP_MODE, NDIReceiver
from app.media.pipeline_manager import PipelineManager


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
_HAS_NDISRC = _GST is not None and _GST.ElementFactory.find("ndisrc") is not None


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _receiver(timestamp_mode: str | None = None) -> NDIReceiver:
    return NDIReceiver(
        source_name="POV",
        feed_id="ndi_pov",
        ndi_name="POVZOOM (Test)",
        frame_width=1280,
        frame_height=720,
        target_fps=30.0,
        timestamp_mode=timestamp_mode,
    )


class NdiReceiverTimestampModeTests(unittest.TestCase):
    def test_default_is_receive_time_vs_timestamp(self) -> None:
        self.assertEqual(_receiver().get_timestamp_mode(), "receive-time-vs-timestamp")
        self.assertEqual(DEFAULT_NDI_TIMESTAMP_MODE, "receive-time-vs-timestamp")

    def test_explicit_mode_overrides_default(self) -> None:
        self.assertEqual(_receiver("auto").get_timestamp_mode(), "auto")


@unittest.skipUnless(_HAS_NDISRC, "GStreamer ndisrc plugin not installed")
class NdiNativeChainTests(unittest.TestCase):
    """Build the real element chain (no network: ndisrc only connects on PLAYING)."""

    def _build_chain(self, timestamp_mode: str | None = None) -> dict:
        receiver = _receiver(timestamp_mode)
        self.assertTrue(receiver.connect_source())
        chain = receiver.build_native_chain(_GST)
        self.assertIsNotNone(chain)
        return chain

    @staticmethod
    def _element(chain: dict, factory_name: str) -> object:
        for element in chain["elements"]:
            if element.get_factory().get_name() == factory_name:
                return element
        raise AssertionError(f"{factory_name} missing from chain")

    def test_ndisrc_gets_receive_time_vs_timestamp_by_default(self) -> None:
        ndisrc = self._element(self._build_chain(), "ndisrc")
        self.assertEqual(
            ndisrc.get_property("timestamp-mode").value_nick,
            "receive-time-vs-timestamp",
        )

    def test_ndisrc_gets_configured_mode(self) -> None:
        ndisrc = self._element(self._build_chain("receive-time"), "ndisrc")
        self.assertEqual(ndisrc.get_property("timestamp-mode").value_nick, "receive-time")

    def test_chain_exposes_videorate_and_its_input_pad(self) -> None:
        chain = self._build_chain()
        videorate = self._element(chain, "videorate")
        self.assertIs(chain["rate_element"], videorate)
        self.assertEqual(chain["ingest_frame_pad"], videorate.get_static_pad("sink"))


def _bare_pipeline_manager() -> tuple[PipelineManager, FeedMetrics]:
    """`PipelineManager` without a pipeline — just the state the probes touch."""
    pm = PipelineManager.__new__(PipelineManager)
    metrics = FeedMetrics(feed_id="ndi_pov", display_name="POV", clock=_FakeClock())
    pm._Gst = SimpleNamespace(PadProbeReturn=SimpleNamespace(OK="ok"))
    pm._feed_metrics = metrics
    pm._preview_running = True
    pm._live_sample_callback = None
    pm._ingest_rate_element = None
    return pm, metrics


class PipelineManagerIngestTelemetryTests(unittest.TestCase):
    def test_ingest_probe_ticks_source_only(self) -> None:
        pm, metrics = _bare_pipeline_manager()
        for _ in range(3):
            self.assertEqual(pm._on_ingest_frame_probe(None, None, None), "ok")
        snap = metrics.snapshot()
        self.assertGreater(snap.source_fps, 0.0)
        self.assertEqual(snap.preview_fps, 0.0)

    def test_preview_probe_no_longer_ticks_source(self) -> None:
        # source_fps used to be ticked here, past the leaky preview queue,
        # which made it read the same as preview_fps.
        pm, metrics = _bare_pipeline_manager()
        pm._on_native_preview_buffer_probe(None, None, None)
        snap = metrics.snapshot()
        self.assertEqual(snap.source_fps, 0.0)
        self.assertGreater(snap.preview_fps, 0.0)

    def test_sample_rate_conversion_is_none_without_videorate(self) -> None:
        pm, _ = _bare_pipeline_manager()
        self.assertIsNone(pm.sample_rate_conversion())

    def test_sample_rate_conversion_reads_counters(self) -> None:
        pm, _ = _bare_pipeline_manager()
        values = {"drop": 475, "duplicate": 595}
        pm._ingest_rate_element = SimpleNamespace(get_property=values.__getitem__)
        self.assertEqual(
            pm.sample_rate_conversion(), {"dropped": 475, "duplicated": 595}
        )

    def test_sample_rate_conversion_swallows_element_errors(self) -> None:
        pm, _ = _bare_pipeline_manager()

        def broken(_name: str) -> int:
            raise RuntimeError("element gone")

        pm._ingest_rate_element = SimpleNamespace(get_property=broken)
        self.assertIsNone(pm.sample_rate_conversion())

    @unittest.skipUnless(_GST is not None, "PyGObject / GStreamer not available")
    def test_sample_rate_conversion_property_names_match_real_videorate(self) -> None:
        pm, _ = _bare_pipeline_manager()
        pm._ingest_rate_element = _GST.ElementFactory.make("videorate", None)
        self.assertEqual(pm.sample_rate_conversion(), {"dropped": 0, "duplicated": 0})


if __name__ == "__main__":
    unittest.main()
