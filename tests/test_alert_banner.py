"""Phase 10.A — operator alert banner.

Pure-function `select_banner_state` is exercised first; widget
construction + refresh integration follows under a `QApplication`.
"""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.core.health_events import HealthEventLog, HealthSeverity
from app.ui.alert_banner import (
    AlertBanner,
    BannerState,
    format_banner_text,
    select_banner_state,
    stylesheet_for_severity,
)


class SelectBannerStateTests(unittest.TestCase):
    def test_empty_returns_hidden(self) -> None:
        state = select_banner_state([])
        self.assertFalse(state.visible)
        self.assertIsNone(state.primary)
        self.assertEqual(state.extra_count, 0)

    def test_single_warning_event_is_visible(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_a went dark",
            feed_id="cam_a",
        )
        state = select_banner_state(log.open_events())
        self.assertTrue(state.visible)
        self.assertIsNotNone(state.primary)
        assert state.primary is not None
        self.assertEqual(state.primary.category, "feed_lost")
        self.assertEqual(state.severity, HealthSeverity.WARNING.value)
        self.assertEqual(state.extra_count, 0)

    def test_error_outranks_concurrent_warning(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_a went dark",
            feed_id="cam_a",
        )
        log.record(
            severity=HealthSeverity.ERROR,
            category="recording_error",
            message="encoder failed",
        )
        state = select_banner_state(log.open_events())
        self.assertTrue(state.visible)
        assert state.primary is not None
        self.assertEqual(state.primary.category, "recording_error")
        self.assertEqual(state.extra_count, 1)

    def test_extra_count_reflects_only_operator_visible_categories(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_a",
            feed_id="cam_a",
        )
        # Diagnostic-only category — should NOT contribute to the count.
        log.record(
            severity=HealthSeverity.WARNING,
            category="audio_missing",
            message="no audio",
            feed_id="cam_a",
        )
        log.record(
            severity=HealthSeverity.WARNING,
            category="invalid_transition",
            message="oops",
        )
        state = select_banner_state(log.open_events())
        self.assertTrue(state.visible)
        assert state.primary is not None
        self.assertEqual(state.primary.category, "feed_lost")
        self.assertEqual(state.extra_count, 0)

    def test_recovery_clears_event_from_open_set(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="disk_low",
            message="20% free",
        )
        self.assertTrue(select_banner_state(log.open_events()).visible)
        log.clear_open_event(category="disk_low", feed_id=None)
        self.assertFalse(select_banner_state(log.open_events()).visible)

    def test_only_diagnostic_categories_open_returns_hidden(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="audio_missing",
            message="no audio",
            feed_id="cam_a",
        )
        state = select_banner_state(log.open_events())
        self.assertFalse(state.visible)

    def test_most_recent_error_wins_tie_break(self) -> None:
        # Two ERROR events open simultaneously: highest event id wins.
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.ERROR,
            category="recording_error",
            message="first failure",
        )
        log.record(
            severity=HealthSeverity.ERROR,
            category="feed_lost",
            message="cam_b dark",
            feed_id="cam_b",
        )
        state = select_banner_state(log.open_events())
        assert state.primary is not None
        self.assertEqual(state.primary.category, "feed_lost")


class FormatBannerTextTests(unittest.TestCase):
    def test_hidden_state_renders_empty_string(self) -> None:
        self.assertEqual(format_banner_text(BannerState(False, None, 0)), "")

    def test_with_feed_id_appends_bracketed_suffix(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_a went dark",
            feed_id="cam_a",
        )
        state = select_banner_state(log.open_events())
        self.assertEqual(
            format_banner_text(state),
            "Feed disconnected [cam_a] — cam_a went dark",
        )

    def test_without_feed_id_omits_suffix(self) -> None:
        log = HealthEventLog()
        log.record(
            severity=HealthSeverity.WARNING,
            category="disk_low",
            message="20% free",
        )
        state = select_banner_state(log.open_events())
        self.assertEqual(
            format_banner_text(state),
            "Disk nearly full — 20% free",
        )


class StylesheetTests(unittest.TestCase):
    def test_error_uses_red_palette(self) -> None:
        sheet = stylesheet_for_severity(HealthSeverity.ERROR.value)
        self.assertIn("#5b1414", sheet)

    def test_warning_uses_amber_palette(self) -> None:
        sheet = stylesheet_for_severity(HealthSeverity.WARNING.value)
        self.assertIn("#4a2a00", sheet)

    def test_unknown_severity_uses_neutral_palette(self) -> None:
        sheet = stylesheet_for_severity(None)
        self.assertIn("#1e1e1e", sheet)


class AlertBannerWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_hidden_when_log_has_no_open_events(self) -> None:
        log = HealthEventLog()
        banner = AlertBanner(log=log)
        self.assertFalse(banner.isVisible())

    def test_visible_after_refresh_when_open_event_present(self) -> None:
        log = HealthEventLog()
        banner = AlertBanner(log=log)
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_a went dark",
            feed_id="cam_a",
        )
        banner.refresh()
        # Calling isVisible() before show() always returns False; instead
        # verify the internal state via the label text + the
        # apply_state-driven `setVisible(True)` flag through `isHidden()`'s
        # inverse. setVisible(True) on an unparented widget toggles the
        # explicit-hidden state regardless of show().
        self.assertFalse(banner.isHidden())
        self.assertIn("cam_a went dark", banner._label.text())

    def test_hides_after_recovery(self) -> None:
        log = HealthEventLog()
        banner = AlertBanner(log=log)
        log.record(
            severity=HealthSeverity.WARNING,
            category="disk_low",
            message="20% free",
        )
        banner.refresh()
        self.assertFalse(banner.isHidden())
        log.clear_open_event(category="disk_low", feed_id=None)
        banner.refresh()
        self.assertTrue(banner.isHidden())
        self.assertEqual(banner._label.text(), "")

    def test_extra_count_label_shows_when_multiple_open(self) -> None:
        log = HealthEventLog()
        banner = AlertBanner(log=log)
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_a",
            feed_id="cam_a",
        )
        log.record(
            severity=HealthSeverity.WARNING,
            category="feed_lost",
            message="cam_b",
            feed_id="cam_b",
        )
        log.record(
            severity=HealthSeverity.WARNING,
            category="disk_low",
            message="20% free",
        )
        banner.refresh()
        self.assertFalse(banner._extra_label.isHidden())
        self.assertEqual(banner._extra_label.text(), "+2 more")

    def test_error_banner_uses_error_palette(self) -> None:
        log = HealthEventLog()
        banner = AlertBanner(log=log)
        log.record(
            severity=HealthSeverity.ERROR,
            category="recording_error",
            message="encoder failed",
        )
        banner.refresh()
        self.assertIn("#5b1414", banner.styleSheet())


if __name__ == "__main__":
    unittest.main()
