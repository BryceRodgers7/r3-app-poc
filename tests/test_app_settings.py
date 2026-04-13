"""Focused tests for TOML-backed application settings."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config.settings import AppSettings
from app.core.feed_registry import FeedRegistry


class AppSettingsTests(unittest.TestCase):
    def test_load_returns_defaults_when_file_is_missing(self) -> None:
        settings = AppSettings.load(Path("missing_app_settings.toml"))

        self.assertEqual(settings.default_source_kind, "auto")
        self.assertIsNone(settings.ndi_source_name)
        self.assertEqual(settings.default_feed_id, "feed_main")

    def test_load_reads_source_configuration_from_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
base_data_dir = 'D:\\ReplayData'
target_fps = 29.97

[source]
kind = "ndi"
display_name = "NDI Bench Camera"
feed_id = "feed_ndi"
camera_index = 2
ndi_name = "OBS-PC (Camera)"
""".strip(),
                encoding="utf-8",
            )

            settings = AppSettings.load(config_path)

        self.assertEqual(settings.base_data_dir, Path(r"D:\ReplayData"))
        self.assertEqual(settings.target_fps, 29.97)
        self.assertEqual(settings.default_source_kind, "ndi")
        self.assertEqual(settings.default_source_name, "NDI Bench Camera")
        self.assertEqual(settings.default_feed_id, "feed_ndi")
        self.assertEqual(settings.test_camera_index, 2)
        self.assertEqual(settings.ndi_source_name, "OBS-PC (Camera)")

    def test_feed_registry_uses_loaded_source_settings(self) -> None:
        settings = AppSettings(
            default_feed_id="feed_ndi",
            default_source_name="Program Camera",
            default_source_kind="ndi",
            test_camera_index=3,
            ndi_source_name="Program NDI",
        )

        registry = FeedRegistry.build_default(settings)
        feed = registry.get_primary_feed()

        self.assertEqual(feed.feed_id, "feed_ndi")
        self.assertEqual(feed.display_name, "Program Camera")
        self.assertEqual(feed.source_kind, "ndi")
        self.assertEqual(feed.camera_index, 3)
        self.assertEqual(feed.ndi_name, "Program NDI")

    def test_load_reads_feeds_array_for_multi_feed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
target_fps = 30.0

[[feeds]]
feed_id = "a"
display_name = "Cam A"
kind = "auto"
camera_index = 0

[[feeds]]
feed_id = "b"
display_name = "NDI B"
kind = "ndi"
ndi_name = "Sender 1"
""".strip(),
                encoding="utf-8",
            )

            settings = AppSettings.load(config_path)

        self.assertEqual(len(settings.feeds_table_rows), 2)
        registry = FeedRegistry.build_default(settings)
        enabled = registry.get_enabled_feeds()
        self.assertEqual(len(enabled), 2)
        self.assertEqual(enabled[0].feed_id, "a")
        self.assertEqual(enabled[1].source_kind, "ndi")
        self.assertEqual(enabled[1].ndi_name, "Sender 1")


if __name__ == "__main__":
    unittest.main()
