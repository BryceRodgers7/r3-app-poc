"""Phase 14.F: referee-window transport gating on challenge state.

Locks in that the entire referee transport (every button + the jog
wheel) is disabled outside an active challenge and enabled only
while `challenge_state_changed(True)` is in flight.
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.app_state import UiState
from app.core.application_coordinator import _CoordinatorSignals
from app.core.models import FeedDefinition


def _stub_controller() -> mock.Mock:
    controller = mock.Mock()
    controller.get_state.return_value = UiState()
    controller.available_session_time_range.return_value = (None, None)
    controller.get_playback_session_time_ns.return_value = None
    return controller


def _stub_renderer() -> mock.Mock:
    renderer = mock.Mock()
    renderer.widgets_by_feed_id = {}
    return renderer


def _stub_coordinator() -> mock.Mock:
    """Coordinator stub whose `signals.challenge_state_changed` is a
    real Qt Signal so MainWindow's `.connect(...)` call lands on a
    live signal we can later `.emit(...)`."""
    coord = mock.Mock()
    coord.signals = _CoordinatorSignals()
    coord.telemetry_hub = None
    return coord


def _make_referee_window():
    from app.ui.main_window import MainWindow

    settings = AppSettings()
    feeds = [
        FeedDefinition(feed_id="feed_a", display_name="A", source_kind="synthetic"),
        FeedDefinition(feed_id="feed_b", display_name="B", source_kind="synthetic"),
    ]
    coord = _stub_coordinator()
    window = MainWindow(
        settings=settings,
        controller=_stub_controller(),
        output_renderer=_stub_renderer(),
        feeds=feeds,
        controls_role="referee",
        live_only_window=False,
        application_coordinator=coord,
        show_diagnostics=False,
    )
    return window, coord


def _transport_buttons(window) -> list:
    controls = window.referee_controls
    return [
        controls.pause_button,
        controls.speed_2x_button,
        controls.half_speed_button,
        controls.quarter_speed_button,
        controls.eighth_speed_button,
        controls.rewind_button,
        controls.step_back_button,
        controls.step_forward_button,
    ]


class RefereeTransportGatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_transport_disabled_at_construction(self) -> None:
        window, _coord = _make_referee_window()
        for button in _transport_buttons(window):
            self.assertFalse(
                button.isEnabled(),
                f"{button.text()!r} should start disabled (no challenge active)",
            )
        self.assertIsNotNone(window.jog_wheel)
        self.assertFalse(window.jog_wheel.is_wheel_enabled())

    def test_challenge_active_enables_every_transport_widget(self) -> None:
        window, coord = _make_referee_window()
        coord.signals.challenge_state_changed.emit(True)
        for button in _transport_buttons(window):
            self.assertTrue(
                button.isEnabled(),
                f"{button.text()!r} should be enabled while a challenge is active",
            )
        self.assertTrue(window.jog_wheel.is_wheel_enabled())

    def test_challenge_end_disables_every_transport_widget(self) -> None:
        window, coord = _make_referee_window()
        coord.signals.challenge_state_changed.emit(True)
        coord.signals.challenge_state_changed.emit(False)
        for button in _transport_buttons(window):
            self.assertFalse(
                button.isEnabled(),
                f"{button.text()!r} should be disabled after the challenge ends",
            )
        self.assertFalse(window.jog_wheel.is_wheel_enabled())

    def test_jog_wheel_seek_routes_to_step_frames(self) -> None:
        # The jog wheel is a pure seek source; its `seek_by_frames_requested`
        # signal must be wired to `controller.step_frames` so the
        # controller's clip-bounds clamping (Phase 14.D) applies.
        window, _coord = _make_referee_window()
        wheel = window.jog_wheel
        assert wheel is not None  # nosec: assert ok in test
        wheel.set_wheel_enabled(True)
        wheel.seek_by_frames_requested.emit(3)
        window._controller.step_frames.assert_called_with(3)
        wheel.seek_by_frames_requested.emit(-2)
        window._controller.step_frames.assert_called_with(-2)


if __name__ == "__main__":
    unittest.main()
