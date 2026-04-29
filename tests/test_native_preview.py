"""Tests for slice 3.A.3 native preview wiring (config + widget flip logic)."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.models import MediaFrame
from app.ui.video_widget import VideoWidget


class ForcePythonPushPreviewSettingsTests(unittest.TestCase):
    def test_default_is_off(self) -> None:
        s = AppSettings()
        self.assertFalse(s.force_python_push_preview)

    def test_load_explicit_true_from_toml(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "app_settings.toml"
            config_path.write_text(
                """
[app]
force_python_push_preview = true
""".strip(),
                encoding="utf-8",
            )
            s = AppSettings.load(config_path)
        self.assertTrue(s.force_python_push_preview)

    def test_load_default_when_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "app_settings.toml"
            config_path.write_text(
                """
[app]
target_fps = 30.0
""".strip(),
                encoding="utf-8",
            )
            s = AppSettings.load(config_path)
        self.assertFalse(s.force_python_push_preview)


def _make_frame() -> MediaFrame:
    return MediaFrame(
        frame_id=0,
        timestamp=0.0,
        image=np.zeros((24, 32, 3), dtype=np.uint8),
        source_name="Test",
        feed_id="test_feed",
    )


class VideoWidgetRenderModeFlipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_default_render_mode_is_qimage(self) -> None:
        widget = VideoWidget()
        widget.set_video_surface_visible(True)
        # qimage mode → frame_label should be the active stack widget.
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )

    def test_native_mode_live_shows_live_surface(self) -> None:
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(True, live=True)
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._live_surface
        )

    def test_native_mode_replay_shows_qimage_layer(self) -> None:
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(True, live=False)
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )

    def test_display_frame_in_native_mode_flips_to_qimage(self) -> None:
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(True, live=True)
        # Live → live_surface active.
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._live_surface
        )
        # A frame arrives (replay path) — QStackedLayout flips.
        widget.display_frame(_make_frame())
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )

    def test_apply_tile_visibility_returns_to_live_after_replay(self) -> None:
        # Drive the same flow MainWindow uses: set_video_surface_visible
        # is called by `apply_tile_visibility(LIVE)` → live=True.
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(True, live=True)
        widget.display_frame(_make_frame())  # simulate replay
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )
        # Operator hits Jump to Live → controller emits LIVE state →
        # MultiFeedVideoPanel.apply_tile_visibility passes live=True.
        widget.set_video_surface_visible(True, live=True)
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._live_surface
        )

    def test_qimage_mode_ignores_live_flag(self) -> None:
        widget = VideoWidget()
        # Default mode is qimage.
        widget.set_video_surface_visible(True, live=True)
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )
        widget.set_video_surface_visible(True, live=False)
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )

    def test_source_lost_always_shows_placeholder(self) -> None:
        widget = VideoWidget()
        widget.set_render_mode("native")
        widget.set_video_surface_visible(False)
        self.assertIs(
            widget._surface_stack.currentWidget(), widget._frame_label
        )

    def test_freeze_badge_hidden_by_default(self) -> None:
        """Phase 6: freeze badge starts hidden."""
        widget = VideoWidget()
        self.assertFalse(widget._freeze_badge_label.isVisibleTo(widget))

    def test_freeze_badge_toggles_via_set_freeze_indicator(self) -> None:
        """Phase 6: set_freeze_indicator(True/False) shows/hides the badge."""
        widget = VideoWidget()
        widget.set_freeze_indicator(True)
        self.assertTrue(
            widget._freeze_badge_label.isVisibleTo(widget._surface_stack_host)
        )
        widget.set_freeze_indicator(False)
        self.assertFalse(
            widget._freeze_badge_label.isVisibleTo(widget._surface_stack_host)
        )


if __name__ == "__main__":
    unittest.main()
