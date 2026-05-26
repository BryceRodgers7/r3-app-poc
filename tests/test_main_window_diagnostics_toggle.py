"""Phase 14.F: `[ui] show_diagnostics` toggle in MainWindow.

Pins the production-vs-dev chrome split:

- Default (`show_diagnostics=False`): `StatusBarWidget` and
  `DiagnosticsWidget` are absent. The Qt `QStatusBar` and the
  operator `AlertBanner` stay present — they're the only sinks for
  transient transport messages and health-event alerts.
- `show_diagnostics=True`: both legacy widgets are built and laid
  out (preserves pre-14.F behavior for developers).
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.app_state import UiState
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


def _make_main_window(*, show_diagnostics: bool, controls_role: str):
    from app.ui.main_window import MainWindow

    settings = AppSettings()
    # Two-feed fixture matches what the operator window's ribbon
    # expects (1-based labels look right with at least two).
    feeds = [
        FeedDefinition(feed_id="feed_a", display_name="A", source_kind="synthetic"),
        FeedDefinition(feed_id="feed_b", display_name="B", source_kind="synthetic"),
    ]
    return MainWindow(
        settings=settings,
        controller=_stub_controller(),
        output_renderer=_stub_renderer(),
        feeds=feeds,
        controls_role=controls_role,
        live_only_window=(controls_role == "operator"),
        application_coordinator=None,
        show_diagnostics=show_diagnostics,
    )


class DiagnosticsToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_default_hides_status_widget_and_diagnostics(self) -> None:
        # No `show_diagnostics` kwarg → falls back to
        # `AppSettings().ui_show_diagnostics` which defaults False.
        from app.ui.main_window import MainWindow

        settings = AppSettings()
        feeds = [
            FeedDefinition(
                feed_id="feed_a", display_name="A", source_kind="synthetic"
            )
        ]
        window = MainWindow(
            settings=settings,
            controller=_stub_controller(),
            output_renderer=_stub_renderer(),
            feeds=feeds,
            controls_role="referee",
            application_coordinator=None,
        )
        self.assertIsNone(window.status_widget)
        self.assertIsNone(window.diagnostics_widget)
        # Qt status bar is always present — it's the only sink for
        # ad-hoc transport messages.
        self.assertIsNotNone(window._status_bar)

    def test_show_diagnostics_false_hides_widgets(self) -> None:
        window = _make_main_window(
            show_diagnostics=False, controls_role="referee"
        )
        self.assertIsNone(window.status_widget)
        self.assertIsNone(window.diagnostics_widget)

    def test_show_diagnostics_true_builds_widgets_on_referee(self) -> None:
        window = _make_main_window(
            show_diagnostics=True, controls_role="referee"
        )
        self.assertIsNotNone(window.status_widget)
        # DiagnosticsWidget additionally requires a coordinator with a
        # telemetry hub — the test passes coordinator=None so it stays
        # None even when the flag is true. That's expected; the flag
        # gates the legacy chrome, the coordinator gates the
        # telemetry plumbing.
        self.assertIsNone(window.diagnostics_widget)

    def test_show_diagnostics_true_on_operator_still_hides_diagnostics_widget(
        self,
    ) -> None:
        # DiagnosticsWidget is referee-only — never built on the
        # operator window regardless of the toggle.
        window = _make_main_window(
            show_diagnostics=True, controls_role="operator"
        )
        self.assertIsNone(window.diagnostics_widget)
        self.assertIsNotNone(window.status_widget)

    def test_render_state_safe_when_status_widget_hidden(self) -> None:
        # Regression guard: _render_state used to unconditionally
        # poke `self.status_widget`. With the widget gated to None,
        # that path must early-return.
        window = _make_main_window(
            show_diagnostics=False, controls_role="referee"
        )
        # Direct call — would crash with AttributeError on a None
        # widget if the guard regressed.
        window._render_state(UiState())

    def test_setting_inherits_from_app_settings(self) -> None:
        # AppSettings.ui_show_diagnostics is the source of truth when
        # the kwarg is omitted. Flipping the setting flips the build.
        from app.ui.main_window import MainWindow

        settings = AppSettings()
        settings.ui_show_diagnostics = True
        feeds = [
            FeedDefinition(
                feed_id="feed_a", display_name="A", source_kind="synthetic"
            )
        ]
        window = MainWindow(
            settings=settings,
            controller=_stub_controller(),
            output_renderer=_stub_renderer(),
            feeds=feeds,
            controls_role="referee",
            application_coordinator=None,
        )
        self.assertIsNotNone(window.status_widget)


class AppSettingsParseTests(unittest.TestCase):
    """Lock in TOML parsing for `[ui] show_diagnostics`."""

    def test_default_is_false(self) -> None:
        self.assertFalse(AppSettings().ui_show_diagnostics)

    def test_toml_true_overrides_default(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "app_settings.toml"
            cfg.write_text("[ui]\nshow_diagnostics = true\n", encoding="utf-8")
            loaded = AppSettings.load(cfg)
        self.assertTrue(loaded.ui_show_diagnostics)

    def test_toml_false_explicit(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "app_settings.toml"
            cfg.write_text("[ui]\nshow_diagnostics = false\n", encoding="utf-8")
            loaded = AppSettings.load(cfg)
        self.assertFalse(loaded.ui_show_diagnostics)


if __name__ == "__main__":
    unittest.main()
