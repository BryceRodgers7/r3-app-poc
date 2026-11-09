"""Feed registry for configured ingest sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.config.prematch_config import CameraSetting, initial_viewport_orders
from app.config.settings import AppSettings
from app.core.models import FeedDefinition

_MAX_ENABLED_FEEDS = 8


def apply_camera_layout(
    feeds: list[FeedDefinition], layout: Mapping[str, CameraSetting]
) -> list[FeedDefinition]:
    """Layer the pre-match camera layout over `feeds`.

    A feed with a setting takes its `short_name` as `display_name`. The
    list is then ordered by `viewport_order`; feeds without a setting keep
    their relative order and follow the ones with one. Settings for
    feeds that no longer exist are ignored. An empty layout returns the
    feeds unchanged.
    """
    if not layout:
        return list(feeds)
    positions = initial_viewport_orders([feed.feed_id for feed in feeds], layout)
    renamed = [
        replace(feed, display_name=layout[feed.feed_id].short_name)
        if feed.feed_id in layout
        else feed
        for feed in feeds
    ]
    return sorted(renamed, key=lambda feed: positions[feed.feed_id])


@dataclass(slots=True)
class FeedRegistry:
    """Own the configured feeds for the current application run."""

    feeds: list[FeedDefinition]

    @classmethod
    def build_default(cls, settings: AppSettings) -> "FeedRegistry":
        """Build feeds from [[feeds]] in TOML when present, else legacy [source].

        `settings.camera_layout` (the pre-match camera setup) is layered
        on top: short names replace display names and `viewport_order`
        sets the feed order. Validation runs on the configured feeds
        before the layout is applied.
        """
        registry = cls._build_configured(settings)
        registry.feeds = apply_camera_layout(registry.feeds, settings.camera_layout)
        return registry

    @classmethod
    def _build_configured(cls, settings: AppSettings) -> "FeedRegistry":
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
                if kind not in {"ndi", "synthetic"}:
                    raise RuntimeError(
                        f"Feed {feed.feed_id!r}: kind must be 'ndi' or 'synthetic', not {feed.source_kind!r}."
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
                    ndi_name=settings.ndi_source_name,
                    ndi_timestamp_mode=settings.ndi_timestamp_mode,
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
        source_kind=str(row.get("source_kind", "synthetic")),
        ndi_name=row.get("ndi_name"),
        enabled=bool(row.get("enabled", True)),
        ndi_timestamp_mode=row.get("ndi_timestamp_mode"),
    )
