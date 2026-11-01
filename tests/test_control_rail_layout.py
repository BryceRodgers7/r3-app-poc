"""Both windows keep their controls in a right-edge rail.

Pins the layout decision that hands the video panel the window's full
height, and the `UiState` → latched-highlight wiring in `_render_state`.
"""

from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStatusBar

from app.config.settings import AppSettings
from app.core.app_state import UiState
from app.core.models import CLIP_TYPE_TIMEOUT, FeedDefinition
from app.ui.main_window import _CONTROL_RAIL_WIDTH, _LAYOUT_GAP, MainWindow
from app.ui.status_bar_widget import TransientStatusLabel


def _make_window(*, controls_role: str, show_diagnostics: bool = False) -> MainWindow:
    controller = mock.Mock()
    controller.get_state.return_value = UiState()
    renderer = mock.Mock()
    renderer.widgets_by_feed_id = {}
    return MainWindow(
        settings=AppSettings(),
        controller=controller,
        output_renderer=renderer,
        feeds=[
            FeedDefinition(feed_id="feed_a", display_name="A", source_kind="synthetic"),
            FeedDefinition(feed_id="feed_b", display_name="B", source_kind="synthetic"),
        ],
        controls_role=controls_role,
        live_only_window=(controls_role == "operator"),
        application_coordinator=None,
        show_diagnostics=show_diagnostics,
    )


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


class ControlRailLayoutTests(_QtTestCase):
    def test_referee_controls_share_the_rail(self) -> None:
        window = _make_window(controls_role="referee")
        rail = window.camera_ribbon.parentWidget()
        self.assertEqual(rail.width(), _CONTROL_RAIL_WIDTH)
        self.assertIs(window.referee_controls.parentWidget(), rail)
        self.assertIs(window.jog_wheel.parentWidget(), rail)
        self.assertIs(window._status_bar.parentWidget(), rail)

    def test_operator_controls_share_the_rail(self) -> None:
        window = _make_window(controls_role="operator")
        rail = window.camera_ribbon.parentWidget()
        self.assertEqual(rail.width(), _CONTROL_RAIL_WIDTH)
        self.assertIs(window.operator_controls.parentWidget(), rail)
        self.assertIs(window.post_process_link.parentWidget(), rail)
        self.assertIs(window._status_bar.parentWidget(), rail)

    def test_video_panel_gets_full_window_height_and_rest_of_width(self) -> None:
        for role in ("referee", "operator"):
            with self.subTest(role=role):
                window = _make_window(controls_role=role)
                window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                window.resize(1280, 860)
                window.show()
                QApplication.processEvents()
                panel = window.video_panel
                self.assertEqual(panel.height(), 860 - 2 * _LAYOUT_GAP)
                self.assertEqual(
                    panel.width(),
                    1280 - _CONTROL_RAIL_WIDTH - 3 * _LAYOUT_GAP,
                )
                window.close()

    def test_no_qstatusbar_strip(self) -> None:
        for role in ("referee", "operator", "none"):
            with self.subTest(role=role):
                window = _make_window(controls_role=role)
                self.assertEqual(window.findChildren(QStatusBar), [])
                self.assertIsNotNone(window._status_bar)

    def test_no_controls_role_has_no_rail(self) -> None:
        window = _make_window(controls_role="none")
        self.assertIs(window._status_bar.parentWidget(), window.centralWidget())

    def test_diagnostics_chrome_still_builds_alongside_rail(self) -> None:
        window = _make_window(controls_role="referee", show_diagnostics=True)
        self.assertIs(window.status_widget.parentWidget(), window.centralWidget())


class RenderStateHighlightTests(_QtTestCase):
    def test_marked_clip_latches_mark_play(self) -> None:
        window = _make_window(controls_role="operator")
        state = UiState(is_recording=True, current_clip_marked=True)
        window._render_state(state)
        self.assertTrue(window.operator_controls.mark_play_button.is_active())
        window._render_state(UiState(is_recording=True))
        self.assertFalse(window.operator_controls.mark_play_button.is_active())

    def test_open_timeout_latches_timeout(self) -> None:
        window = _make_window(controls_role="operator")
        window._render_state(
            UiState(
                is_recording=True,
                current_play_number=2,
                current_clip_type=CLIP_TYPE_TIMEOUT,
            )
        )
        self.assertTrue(window.operator_controls.timeout_button.is_active())

    def test_rate_latches_speed_button(self) -> None:
        window = _make_window(controls_role="referee")
        state = UiState()
        state.playback_overlay = dataclasses.replace(
            state.playback_overlay, playback_rate=0.25
        )
        window._render_state(state)
        self.assertTrue(window.referee_controls.quarter_speed_button.is_active())
        self.assertFalse(window.referee_controls.half_speed_button.is_active())


class TransientStatusLabelTests(_QtTestCase):
    def test_message_persists_without_timeout(self) -> None:
        label = TransientStatusLabel()
        label.show_message("Clip marked.")
        QTest.qWait(30)
        self.assertEqual(label.text(), "Clip marked.")

    def test_message_clears_after_timeout(self) -> None:
        label = TransientStatusLabel()
        label.show_message("No active session to process.", 10)
        QTest.qWait(60)
        self.assertEqual(label.text(), "")

    def test_new_message_cancels_pending_clear(self) -> None:
        label = TransientStatusLabel()
        label.show_message("first", 10)
        label.show_message("second")
        QTest.qWait(60)
        self.assertEqual(label.text(), "second")


if __name__ == "__main__":
    unittest.main()
