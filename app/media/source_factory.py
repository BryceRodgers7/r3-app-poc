"""Build the configured source for one feed.

After Phase 2.5 the source surface is intentionally narrow: production uses
NDI (`kind = "ndi"`); development without NDI hardware uses the synthetic
test source (`kind = "synthetic"`). Any other `kind` value raises
`ConfigError` so misconfigured prod deployments fail loudly rather than
falling back to a dev source.
"""

from __future__ import annotations

import logging

from app.config.settings import AppSettings
from app.core.models import FeedDefinition
from app.media.ndi_receiver import NDIReceiver
from app.media.source_interface import SourceInterface
from app.media.test_source import TestSource

LOGGER = logging.getLogger(__name__)

VALID_SOURCE_KINDS = {"ndi", "synthetic"}


class ConfigError(RuntimeError):
    """Raised when feed configuration cannot be turned into a source."""


def build_source_for_feed(settings: AppSettings, feed: FeedDefinition) -> SourceInterface:
    """Build the configured source for one feed."""
    kind = (feed.source_kind or "").strip().lower()
    if kind == "ndi":
        if not feed.ndi_name or not str(feed.ndi_name).strip():
            raise ConfigError(
                f"Feed {feed.feed_id!r}: kind='ndi' requires an `ndi_name`."
            )
        return NDIReceiver(
            source_name=feed.display_name,
            feed_id=feed.feed_id,
            ndi_name=feed.ndi_name,
            frame_width=settings.target_frame_width,
            frame_height=settings.target_frame_height,
            target_fps=settings.target_fps,
        )
    if kind == "synthetic":
        return TestSource(
            source_name=feed.display_name,
            feed_id=feed.feed_id,
            frame_width=settings.target_frame_width,
            frame_height=settings.target_frame_height,
            target_fps=settings.target_fps,
        )
    raise ConfigError(
        f"Feed {feed.feed_id!r}: unsupported kind={feed.source_kind!r}. "
        f"Valid values are {sorted(VALID_SOURCE_KINDS)}. "
        f"(USB-camera ingest was removed in Phase 2.5; use kind='synthetic' "
        f"for dev or kind='ndi' for production.)"
    )


def build_default_source(settings: AppSettings) -> SourceInterface:
    """Build a single default source from `[source]` in the legacy TOML schema."""
    return build_source_for_feed(
        settings,
        FeedDefinition(
            feed_id=settings.default_feed_id,
            display_name=settings.default_source_name,
            source_kind=settings.default_source_kind,
            ndi_name=settings.ndi_source_name,
        ),
    )
