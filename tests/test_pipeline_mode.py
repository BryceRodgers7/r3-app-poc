"""Tests for the `PipelineMode` source-side declaration (§3.A.1)."""

from __future__ import annotations

import unittest

from app.config.settings import AppSettings
from app.media.ndi_receiver import NDIReceiver
from app.media.source_interface import PipelineMode, SourceInterface
from app.media.test_source import TestSource


class PipelineModeDefaultsTests(unittest.TestCase):
    def test_synthetic_source_declares_python_push(self) -> None:
        source = TestSource(
            source_name="Dev",
            feed_id="feed_dev",
            frame_width=64,
            frame_height=36,
            target_fps=15.0,
        )
        self.assertIs(source.pipeline_mode, PipelineMode.PYTHON_PUSH)

    def test_ndi_receiver_declares_native_after_3a2(self) -> None:
        source = NDIReceiver(
            source_name="NDI",
            feed_id="feed_ndi",
            ndi_name="Sender",
            frame_width=640,
            frame_height=360,
            target_fps=30.0,
        )
        self.assertIs(source.pipeline_mode, PipelineMode.NATIVE)

    def test_ndi_receiver_read_frame_returns_none_when_native(self) -> None:
        # Native sources never deliver frames into Python; PipelineManager
        # consumes them via the bin's video src ghost pad instead.
        source = NDIReceiver(
            source_name="NDI",
            feed_id="feed_ndi",
            ndi_name="Sender",
            frame_width=640,
            frame_height=360,
            target_fps=30.0,
        )
        self.assertIsNone(source.read_frame())

    def test_ndi_receiver_build_native_chain_returns_none_before_connect(self) -> None:
        # build_native_chain should only return a chain after connect_source
        # verified the runtime dependency. That keeps the NATIVE contract honest.
        source = NDIReceiver(
            source_name="NDI",
            feed_id="feed_ndi",
            ndi_name="Sender",
            frame_width=640,
            frame_height=360,
            target_fps=30.0,
        )
        # No connect_source() called -> chain should not be produced.
        self.assertIsNone(source.build_native_chain(gst_module=object()))

    def test_default_build_native_chain_returns_none(self) -> None:
        source = TestSource(
            source_name="Dev",
            feed_id="feed_dev",
            frame_width=64,
            frame_height=36,
            target_fps=15.0,
        )
        self.assertIsNone(source.build_native_chain(gst_module=None))

    def test_subclass_can_override_pipeline_mode(self) -> None:
        class _NativeFake(SourceInterface):
            @property
            def pipeline_mode(self) -> PipelineMode:
                return PipelineMode.NATIVE

            def connect_source(self) -> bool:
                return True

            def disconnect_source(self) -> None:
                pass

            def is_connected(self) -> bool:
                return True

            def get_display_name(self) -> str:
                return "Fake Native"

            def create_pipeline_fragment(self) -> str:
                return "fake-native"

            def read_frame(self) -> None:
                return None

            def get_frame_size(self) -> tuple[int, int]:
                return 64, 36

            def get_nominal_fps(self) -> float:
                return 30.0

        self.assertIs(_NativeFake().pipeline_mode, PipelineMode.NATIVE)


if __name__ == "__main__":
    unittest.main()
