"""The pre-match camera layout is layered over the configured feeds."""

from __future__ import annotations

import unittest

from app.config.prematch_config import CameraSetting
from app.config.settings import AppSettings
from app.core.feed_registry import FeedRegistry


def _settings(*rows: dict) -> AppSettings:
    settings = AppSettings()
    settings.feeds_table_rows = list(rows)
    return settings


def _row(feed_id: str, display_name: str, *, enabled: bool = True) -> dict:
    return {
        "feed_id": feed_id,
        "display_name": display_name,
        "source_kind": "ndi",
        "ndi_name": f"HOST ({display_name})",
        "enabled": enabled,
    }


class CameraLayoutLayeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings(
            _row("ndi_1", "NDI One"),
            _row("ndi_2", "NDI Two"),
            _row("ndi_3", "NDI Three"),
        )

    def _names(self) -> list[str]:
        return [f.display_name for f in FeedRegistry.build_default(self.settings).get_enabled_feeds()]

    def test_no_layout_leaves_names_and_order_alone(self) -> None:
        self.assertEqual(self._names(), ["NDI One", "NDI Two", "NDI Three"])

    def test_short_name_replaces_display_name(self) -> None:
        self.settings.camera_layout = {
            "ndi_1": CameraSetting("ndi_1", "Baseline A", 1),
            "ndi_2": CameraSetting("ndi_2", "Baseline B", 2),
            "ndi_3": CameraSetting("ndi_3", "Sideline", 3),
        }
        self.assertEqual(self._names(), ["Baseline A", "Baseline B", "Sideline"])

    def test_viewport_order_reorders_the_feeds(self) -> None:
        self.settings.camera_layout = {
            "ndi_1": CameraSetting("ndi_1", "A", 3),
            "ndi_2": CameraSetting("ndi_2", "B", 1),
            "ndi_3": CameraSetting("ndi_3", "C", 2),
        }
        registry = FeedRegistry.build_default(self.settings)
        self.assertEqual([f.feed_id for f in registry.get_enabled_feeds()], ["ndi_2", "ndi_3", "ndi_1"])
        self.assertEqual(registry.get_primary_feed().feed_id, "ndi_2")

    def test_unlisted_feeds_keep_their_name_and_follow_the_listed_ones(self) -> None:
        self.settings.camera_layout = {"ndi_3": CameraSetting("ndi_3", "Only", 1)}
        self.assertEqual(self._names(), ["Only", "NDI One", "NDI Two"])

    def test_settings_for_a_feed_that_no_longer_exists_are_ignored(self) -> None:
        self.settings.camera_layout = {"gone": CameraSetting("gone", "Ghost", 1)}
        self.assertEqual(self._names(), ["NDI One", "NDI Two", "NDI Three"])

    def test_disabled_feeds_are_still_not_started(self) -> None:
        settings = _settings(
            _row("ndi_1", "One"), _row("ndi_2", "Two", enabled=False), _row("ndi_3", "Three")
        )
        settings.camera_layout = {
            "ndi_2": CameraSetting("ndi_2", "Spare", 1),
            "ndi_3": CameraSetting("ndi_3", "Third", 2),
            "ndi_1": CameraSetting("ndi_1", "First", 3),
        }
        registry = FeedRegistry.build_default(settings)
        self.assertEqual([f.display_name for f in registry.get_enabled_feeds()], ["Third", "First"])

    def test_the_session_label_uses_the_short_name_of_the_first_feed(self) -> None:
        self.settings.camera_layout = {"ndi_2": CameraSetting("ndi_2", "Baseline A", 1)}
        label = FeedRegistry.build_default(self.settings).build_session_label()
        self.assertTrue(label.startswith("Baseline A"))

    def test_the_layout_does_not_change_the_source_settings(self) -> None:
        self.settings.camera_layout = {"ndi_1": CameraSetting("ndi_1", "Renamed", 1)}
        feed = FeedRegistry.build_default(self.settings).get_feed("ndi_1")
        self.assertEqual(feed.ndi_name, "HOST (NDI One)")
        self.assertEqual(feed.source_kind, "ndi")

    def test_validation_still_runs_on_the_configured_feeds(self) -> None:
        settings = _settings(_row("a", "A", enabled=False))
        settings.camera_layout = {"a": CameraSetting("a", "A", 1)}
        with self.assertRaises(RuntimeError):
            FeedRegistry.build_default(settings)

    def test_legacy_single_source_takes_the_layout_too(self) -> None:
        settings = AppSettings()
        settings.default_feed_id = "feed_main"
        settings.camera_layout = {"feed_main": CameraSetting("feed_main", "Court", 1)}
        feeds = FeedRegistry.build_default(settings).get_enabled_feeds()
        self.assertEqual([f.display_name for f in feeds], ["Court"])


if __name__ == "__main__":
    unittest.main()
