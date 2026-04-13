"""Feed registry for configured ingest sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.settings import AppSettings
from app.core.models import FeedDefinition

_MAX_ENABLED_FEEDS = 8


@dataclass(slots=True)
class FeedRegistry:
    """Own the configured feeds for the current application run."""

    feeds: list[FeedDefinition]

    @classmethod
    def build_default(cls, settings: AppSettings) -> "FeedRegistry":
        """Build feeds from [[feeds]] in TOML when present, else legacy [source]."""
        if settings.feeds_table_rows:
            feeds = [_feed_from_settings_row(row) for row in settings.feeds_table_rows]
            enabled = [f for f in feeds if f.enabled]
            if not enabled:
                raise RuntimeError("At least one enabled feed is required in [[feeds]].")
            if len(enabled) > _MAX_ENABLED_FEEDS:
                raise RuntimeError(f"At most {_MAX_ENABLED_FEEDS} enabled feeds are allowed.")
            seen: set[str] = set()
            for feed in enabled:
                if feed.feed_id in seen:
                    raise RuntimeError(f"Duplicate feed_id in [[feeds]]: {feed.feed_id!r}.")
                seen.add(feed.feed_id)
                kind = feed.source_kind.strip().lower()
                if kind not in {"auto", "ndi"}:
                    raise RuntimeError(
                        f"Feed {feed.feed_id!r}: kind must be 'auto' or 'ndi', not {feed.source_kind!r}."
                    )
                if kind == "ndi" and not (feed.ndi_name and str(feed.ndi_name).strip()):
                    raise RuntimeError(f"Feed {feed.feed_id!r}: ndi_name is required when kind is 'ndi'.")
            return cls(feeds=feeds)

        return cls(
            feeds=[
                FeedDefinition(
                    feed_id=settings.default_feed_id,
                    display_name=settings.default_source_name,
                    source_kind=settings.default_source_kind,
                    camera_index=settings.test_camera_index,
                    ndi_name=settings.ndi_source_name,
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


def _feed_from_settings_row(row: dict[str, Any]) -> FeedDefinition:
    return FeedDefinition(
        feed_id=str(row["feed_id"]),
        display_name=str(row.get("display_name", row["feed_id"])),
        source_kind=str(row.get("source_kind", "auto")),
        camera_index=int(row.get("camera_index", 0)),
        ndi_name=row.get("ndi_name"),
        enabled=bool(row.get("enabled", True)),
    )
