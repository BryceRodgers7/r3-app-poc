"""Tests for `app.media.source_factory.build_source_for_feed`.

Phase 2.5 removed the USB / OpenCV chain. The factory now dispatches on
`kind` to either the NDI receiver or the synthetic test source, and rejects
any other value with a clear migration message.
"""

from __future__ import annotations

import unittest

from app.config.settings import AppSettings
from app.core.models import FeedDefinition
from app.media.source_factory import ConfigError, VALID_SOURCE_KINDS, build_source_for_feed
from app.media.test_source import TestSource


class BuildSourceForFeedTests(unittest.TestCase):
    def test_synthetic_kind_returns_test_source(self) -> None:
        settings = AppSettings()
        feed = FeedDefinition(
            feed_id="feed_dev",
            display_name="Dev",
            source_kind="synthetic",
        )
        source = build_source_for_feed(settings, feed)
        self.assertIsInstance(source, TestSource)

    def test_ndi_kind_without_ndi_name_raises_config_error(self) -> None:
        settings = AppSettings()
        feed = FeedDefinition(
            feed_id="feed_ndi",
            display_name="NDI",
            source_kind="ndi",
            ndi_name=None,
        )
        with self.assertRaises(ConfigError):
            build_source_for_feed(settings, feed)

    def test_unsupported_kind_raises_with_migration_hint(self) -> None:
        settings = AppSettings()
        feed = FeedDefinition(
            feed_id="feed_legacy",
            display_name="Legacy",
            source_kind="auto",
        )
        with self.assertRaises(ConfigError) as ctx:
            build_source_for_feed(settings, feed)
        self.assertIn("Phase 2.5", str(ctx.exception))

    def test_valid_kinds_set(self) -> None:
        self.assertEqual(VALID_SOURCE_KINDS, {"ndi", "synthetic"})


if __name__ == "__main__":
    unittest.main()
