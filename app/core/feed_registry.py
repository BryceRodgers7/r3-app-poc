"""Feed registry for configured ingest sources."""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import AppSettings
from app.core.models import FeedDefinition


@dataclass(slots=True)
class FeedRegistry:
    """Own the configured feeds for the current application run."""

    feeds: list[FeedDefinition]

    @classmethod
    def build_default(cls, settings: AppSettings) -> "FeedRegistry":
        """Return the default single-feed configuration used today."""
        return cls(
            feeds=[
                FeedDefinition(
                    feed_id="feed_main",
                    display_name=settings.default_source_name,
                    source_kind="auto",
                    camera_index=settings.test_camera_index,
                )
            ]
        )

    def get_enabled_feeds(self) -> list[FeedDefinition]:
        """Return feeds that should be started."""
        return [feed for feed in self.feeds if feed.enabled]

    def get_primary_feed(self) -> FeedDefinition:
        """Return the first enabled feed."""
        enabled_feeds = self.get_enabled_feeds()
        if not enabled_feeds:
            raise RuntimeError("At least one enabled feed is required.")
        return enabled_feeds[0]

    def get_feed(self, feed_id: str) -> FeedDefinition:
        """Return a configured feed by identifier."""
        for feed in self.feeds:
            if feed.feed_id == feed_id:
                return feed
        raise KeyError(feed_id)

    def build_session_label(self) -> str:
        """Return a concise source label for session metadata."""
        enabled = self.get_enabled_feeds()
        if len(enabled) == 1:
            return enabled[0].display_name
        return f"{enabled[0].display_name} +{max(len(enabled) - 1, 0)} feeds"
