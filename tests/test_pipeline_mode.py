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


class VideoWidgetRenderModeTests(unittest.TestCase):
    """Slice 3.A.3: `VideoWidget` toggles the right surface per render mode."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_set_video_surface_visible_qimage_mode_shows_label(self) -> None:
        from app.ui.video_widget import VideoWidget
        widget = VideoWidget()
        widget.set_render_mode("qimage")
        widget.set_video_surface_visible(True)
        self.assertIs(widget._surface_stack.currentWidget(), widget._frame_label)

    def test_set_video_surface_visible_native_mode_shows_native_surface(self) -> None:
        from app.ui.video_widget import VideoWidget
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(True)
        self.assertIs(widget._surface_stack.currentWidget(), widget._live_surface)

    def test_disabled_falls_back_to_label_regardless_of_mode(self) -> None:
        from app.ui.video_widget import VideoWidget
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(False)
        self.assertIs(widget._surface_stack.currentWidget(), widget._frame_label)

    def test_unknown_render_mode_rejected(self) -> None:
        from app.ui.video_widget import VideoWidget
        widget = VideoWidget()
        with self.assertRaises(ValueError):
            widget.set_render_mode("opengl")


class PipelineManagerNativeWindowBindingTests(unittest.TestCase):
    """Slice 3.A.3: per-window handle setters route to the right sink."""

    def test_set_native_preview_window_handle_rejects_bad_role(self) -> None:
        from unittest.mock import MagicMock
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._pipeline_lock = __import__("threading").Lock()
        pm._referee_window_handle = None
        pm._operator_window_handle = None
        pm._referee_preview_sink = None
        pm._operator_preview_sink = None
        with self.assertRaises(ValueError):
            pm.set_native_preview_window_handle("third-monitor", 12345)

    def test_set_native_preview_window_handle_stores_when_sink_missing(self) -> None:
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._pipeline_lock = __import__("threading").Lock()
        pm._referee_window_handle = None
        pm._operator_window_handle = None
        pm._referee_preview_sink = None
        pm._operator_preview_sink = None
        # No sink yet — should just store the handle without raising.
        pm.set_native_preview_window_handle("referee", 0xABCD)
        pm.set_native_preview_window_handle("operator", 0x1234)
        self.assertEqual(pm._referee_window_handle, 0xABCD)
        self.assertEqual(pm._operator_window_handle, 0x1234)


class PipelineManagerRenderRectangleTests(unittest.TestCase):
    """A tile zooms live video through its sink's render rectangle."""

    def _manager(self):
        from unittest.mock import MagicMock
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._pipeline_lock = __import__("threading").Lock()
        pm._referee_window_handle = None
        pm._operator_window_handle = None
        pm._referee_render_rect = None
        pm._operator_render_rect = None
        pm._referee_preview_sink = MagicMock(name="referee_sink")
        pm._operator_preview_sink = MagicMock(name="operator_sink")
        pm._GstVideo = None
        return pm

    def test_rejects_bad_role(self) -> None:
        pm = self._manager()
        with self.assertRaises(ValueError):
            pm.set_native_preview_render_rectangle("third-monitor", (0, 0, 10, 10))

    def test_goes_to_that_roles_sink_only(self) -> None:
        pm = self._manager()
        pm.set_native_preview_render_rectangle("referee", (-100, -50, 800, 450))
        pm._referee_preview_sink.set_render_rectangle.assert_called_once_with(
            -100, -50, 800, 450
        )
        pm._operator_preview_sink.set_render_rectangle.assert_not_called()

    def test_stores_when_sink_missing(self) -> None:
        pm = self._manager()
        pm._referee_preview_sink = None
        pm.set_native_preview_render_rectangle("referee", (-100, -50, 800, 450))
        self.assertEqual(pm._referee_render_rect, (-100, -50, 800, 450))

    def test_rebuilt_sink_gets_the_view_back(self) -> None:
        # A reconnect replaces the sink; the tile doesn't re-send its
        # rectangle, because nothing about the tile changed.
        from unittest.mock import MagicMock

        pm = self._manager()
        pm.set_native_preview_render_rectangle("referee", (-100, -50, 800, 450))
        rebuilt = MagicMock(name="rebuilt_referee_sink")
        pm._referee_preview_sink = rebuilt
        pm._bind_named_video_sink_locked(rebuilt, 0xABCD)
        rebuilt.set_window_handle.assert_called_once_with(0xABCD)
        rebuilt.set_render_rectangle.assert_called_once_with(-100, -50, 800, 450)

    def test_never_zoomed_sink_is_bound_without_a_rectangle(self) -> None:
        # An explicit rectangle would stop it tracking its window.
        pm = self._manager()
        pm._bind_named_video_sink_locked(pm._referee_preview_sink, 0xABCD)
        pm._referee_preview_sink.set_render_rectangle.assert_not_called()


class _FakeVideoCrop:
    """A `videocrop` that, like the real one, refuses a crop wider than the frame."""

    def __init__(self, frame=(1920, 1080)) -> None:
        self.frame = frame
        self.props = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        self.history: list[tuple[str, int]] = []

    def get_property(self, name):
        return self.props[name]

    def set_property(self, name, value) -> None:
        trial = dict(self.props, **{name: value})
        if (
            trial["left"] + trial["right"] >= self.frame[0]
            or trial["top"] + trial["bottom"] >= self.frame[1]
        ):
            raise AssertionError(f"crop leaves no picture: {trial}")
        self.props[name] = value
        self.history.append((name, value))

    def crop(self) -> tuple[int, int, int, int]:
        p = self.props
        return p["left"], p["top"], p["right"], p["bottom"]


class PipelineManagerCropTests(unittest.TestCase):
    """The referee window crops the frame ahead of its sink to zoom past one surface."""

    def _manager(self):
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._pipeline_lock = __import__("threading").Lock()
        pm._referee_crop = (0, 0, 0, 0)
        pm._referee_crop_element = _FakeVideoCrop()
        return pm

    def test_the_crop_reaches_the_element(self) -> None:
        pm = self._manager()
        pm.set_native_preview_crop("referee", (480, 270, 480, 270))
        self.assertEqual(pm._referee_crop_element.crop(), (480, 270, 480, 270))

    def test_only_the_referee_window_crops(self) -> None:
        pm = self._manager()
        for role in ("operator", "third-monitor"):
            with self.assertRaises(ValueError):
                pm.set_native_preview_crop(role, (0, 0, 0, 0))

    def test_a_bad_crop_is_refused(self) -> None:
        pm = self._manager()
        for bad in ((1, 2, 3), (-2, 0, 0, 0)):
            with self.assertRaises(ValueError):
                pm.set_native_preview_crop("referee", bad)

    def test_it_is_stored_before_the_element_exists(self) -> None:
        pm = self._manager()
        pm._referee_crop_element = None
        pm.set_native_preview_crop("referee", (480, 270, 480, 270))
        self.assertEqual(pm._referee_crop, (480, 270, 480, 270))

    def test_a_rebuilt_branch_gets_the_crop_back(self) -> None:
        # A reconnect rebuilds the branch; the tile doesn't re-send its crop.
        pm = self._manager()
        pm.set_native_preview_crop("referee", (480, 270, 480, 270))
        pm._referee_crop_element = _FakeVideoCrop()
        pm._apply_referee_crop_locked()
        self.assertEqual(pm._referee_crop_element.crop(), (480, 270, 480, 270))

    def test_sliding_the_crop_never_passes_through_one_too_wide(self) -> None:
        # left + right must stay under the frame at every step of an update.
        pm = self._manager()
        pm.set_native_preview_crop("referee", (0, 540, 1440, 0))  # right-hand quarter-ish
        pm.set_native_preview_crop("referee", (1440, 0, 0, 540))  # all the way across
        self.assertEqual(pm._referee_crop_element.crop(), (1440, 0, 0, 540))
        pm.set_native_preview_crop("referee", (0, 540, 1440, 0))
        self.assertEqual(pm._referee_crop_element.crop(), (0, 540, 1440, 0))

    def test_changing_the_level_never_passes_through_one_too_wide(self) -> None:
        pm = self._manager()
        for crop in (
            (480, 270, 480, 270),
            (720, 405, 720, 405),
            (0, 0, 0, 0),
            (1440, 810, 0, 0),
            (0, 0, 1440, 810),
            (480, 270, 480, 270),
        ):
            pm.set_native_preview_crop("referee", crop)
            self.assertEqual(pm._referee_crop_element.crop(), crop)

    def test_zero_crop_restores_the_full_frame(self) -> None:
        pm = self._manager()
        pm.set_native_preview_crop("referee", (480, 270, 480, 270))
        pm.set_native_preview_crop("referee", (0, 0, 0, 0))
        self.assertEqual(pm._referee_crop_element.crop(), (0, 0, 0, 0))


class CoordinatorRenderRectangleTests(unittest.TestCase):
    def _coordinator(self, *, mode, force_python_push: bool = False):
        from unittest.mock import MagicMock
        from app.core.application_coordinator import ApplicationCoordinator

        coordinator = ApplicationCoordinator.__new__(ApplicationCoordinator)
        runtime = MagicMock()
        runtime.source.pipeline_mode = mode
        coordinator._feed_runtimes = {"feed_a": runtime}
        coordinator._settings = MagicMock(force_python_push_preview=force_python_push)
        return coordinator, runtime

    def test_native_feed_reaches_its_pipeline(self) -> None:
        coordinator, runtime = self._coordinator(mode=PipelineMode.NATIVE)
        coordinator.set_native_preview_render_rectangle(
            "referee", "feed_a", (-10, -5, 800, 450)
        )
        runtime.pipeline_manager.set_native_preview_render_rectangle.assert_called_once_with(
            "referee", (-10, -5, 800, 450)
        )

    def test_python_push_feed_is_left_to_the_widget(self) -> None:
        coordinator, runtime = self._coordinator(mode=PipelineMode.PYTHON_PUSH)
        coordinator.set_native_preview_render_rectangle(
            "referee", "feed_a", (-10, -5, 800, 450)
        )
        runtime.pipeline_manager.set_native_preview_render_rectangle.assert_not_called()

    def test_forced_python_push_is_left_to_the_widget(self) -> None:
        coordinator, runtime = self._coordinator(
            mode=PipelineMode.NATIVE, force_python_push=True
        )
        coordinator.set_native_preview_render_rectangle(
            "referee", "feed_a", (-10, -5, 800, 450)
        )
        runtime.pipeline_manager.set_native_preview_render_rectangle.assert_not_called()

    def test_unknown_feed_is_ignored(self) -> None:
        coordinator, _ = self._coordinator(mode=PipelineMode.NATIVE)
        coordinator.set_native_preview_render_rectangle(
            "referee", "feed_z", (-10, -5, 800, 450)
        )

    def test_native_feed_crop_reaches_its_pipeline(self) -> None:
        coordinator, runtime = self._coordinator(mode=PipelineMode.NATIVE)
        coordinator.set_native_preview_crop("referee", "feed_a", (480, 270, 480, 270))
        runtime.pipeline_manager.set_native_preview_crop.assert_called_once_with(
            "referee", (480, 270, 480, 270)
        )

    def test_python_push_feed_crop_is_left_to_the_widget(self) -> None:
        coordinator, runtime = self._coordinator(mode=PipelineMode.PYTHON_PUSH)
        coordinator.set_native_preview_crop("referee", "feed_a", (480, 270, 480, 270))
        runtime.pipeline_manager.set_native_preview_crop.assert_not_called()

    def test_unknown_feed_crop_is_ignored(self) -> None:
        coordinator, _ = self._coordinator(mode=PipelineMode.NATIVE)
        coordinator.set_native_preview_crop("referee", "feed_z", (480, 270, 480, 270))


if __name__ == "__main__":
    unittest.main()
