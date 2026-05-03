"""Focused tests for TOML-backed application settings."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config.settings import AppSettings
from app.core.feed_registry import FeedRegistry
from app.core.models import FeedDefinition
from app.media.source_factory import build_source_for_feed


class AppSettingsTests(unittest.TestCase):
    def test_load_returns_defaults_when_file_is_missing(self) -> None:
        settings = AppSettings.load(Path("missing_app_settings.toml"))

        self.assertEqual(settings.default_source_kind, "synthetic")
        self.assertIsNone(settings.ndi_source_name)
        self.assertEqual(settings.default_feed_id, "feed_main")
        # Slice 3.C default — production warnings are off until opted in.
        self.assertEqual(settings.app_mode, "development")

    def test_load_accepts_production_app_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
app_mode = "production"
""".strip(),
                encoding="utf-8",
            )
            settings = AppSettings.load(config_path)
        self.assertEqual(settings.app_mode, "production")

    def test_load_rejects_unknown_app_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
app_mode = "staging"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                AppSettings.load(config_path)

    def test_load_reads_source_configuration_from_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
base_data_dir = 'D:\\ReplayData'
target_fps = 29.97
enable_embedded_audio = true
live_audio_monitor_enabled = false
audio_sample_rate = 44100
audio_channels = 1
audio_bitrate = 96000

[source]
kind = "ndi"
display_name = "NDI Bench Camera"
feed_id = "feed_ndi"
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
        self.assertEqual(settings.ndi_source_name, "OBS-PC (Camera)")
        self.assertTrue(settings.enable_embedded_audio)
        self.assertFalse(settings.live_audio_monitor_enabled)
        self.assertEqual(settings.audio_sample_rate, 44100)
        self.assertEqual(settings.audio_channels, 1)
        self.assertEqual(settings.audio_bitrate, 96000)

    def test_load_rejects_legacy_auto_kind_with_migration_hint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[source]
kind = "auto"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as ctx:
                AppSettings.load(config_path)
        self.assertIn("Phase 2.5", str(ctx.exception))

    def test_feed_registry_uses_loaded_source_settings(self) -> None:
        settings = AppSettings(
            default_feed_id="feed_ndi",
            default_source_name="Program Camera",
            default_source_kind="ndi",
            ndi_source_name="Program NDI",
        )

        registry = FeedRegistry.build_default(settings)
        feed = registry.get_primary_feed()

        self.assertEqual(feed.feed_id, "feed_ndi")
        self.assertEqual(feed.display_name, "Program Camera")
        self.assertEqual(feed.source_kind, "ndi")
        self.assertEqual(feed.ndi_name, "Program NDI")

    def test_media_hardware_acceleration_defaults_to_auto(self) -> None:
        # No [media] table in TOML — default kicks in.
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
target_fps = 30.0
""".strip(),
                encoding="utf-8",
            )
            settings = AppSettings.load(config_path)
        self.assertEqual(settings.media_hardware_acceleration, "auto")

    def test_media_hardware_acceleration_round_trip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[media]
hardware_acceleration = "intel"
""".strip(),
                encoding="utf-8",
            )
            settings = AppSettings.load(config_path)
        self.assertEqual(settings.media_hardware_acceleration, "intel")

    def test_media_hardware_acceleration_rejects_unknown_value(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[media]
hardware_acceleration = "vulkan"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as ctx:
                AppSettings.load(config_path)
        self.assertIn("hardware_acceleration", str(ctx.exception))

    def test_load_reads_feeds_array_for_multi_feed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[app]
target_fps = 30.0

[[feeds]]
feed_id = "a"
display_name = "Synthetic A"
kind = "synthetic"

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
        self.assertEqual(enabled[0].source_kind, "synthetic")
        self.assertEqual(enabled[1].source_kind, "ndi")
        self.assertEqual(enabled[1].ndi_name, "Sender 1")

    def test_load_rejects_legacy_auto_kind_in_feeds_table(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app_settings.toml"
            config_path.write_text(
                """
[[feeds]]
feed_id = "legacy"
kind = "auto"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as ctx:
                AppSettings.load(config_path)
        self.assertIn("Phase 2.5", str(ctx.exception))

    def test_source_chain_reports_embedded_audio_capability_for_ndi(self) -> None:
        settings = AppSettings(default_source_kind="ndi")
        ndi_source = build_source_for_feed(
            settings,
            FeedDefinition(
                feed_id="ndi",
                display_name="NDI",
                source_kind="ndi",
                ndi_name="Sender",
            ),
        )
        synthetic_source = build_source_for_feed(
            settings,
            FeedDefinition(
                feed_id="dev",
                display_name="Dev",
                source_kind="synthetic",
            ),
        )

        self.assertTrue(ndi_source.supports_embedded_audio())
        self.assertFalse(synthetic_source.supports_embedded_audio())


if __name__ == "__main__":
    unittest.main()
