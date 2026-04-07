"""Coordinator for per-feed replay stores."""

from __future__ import annotations

from app.media.replay_buffer import ReplayStore


class ReplayStoreManager:
    """Track replay stores by feed identifier."""

    def __init__(self) -> None:
        self._stores: dict[str, ReplayStore] = {}

    def register(self, feed_id: str, store: ReplayStore) -> None:
        """Register a replay store for a feed."""
        self._stores[feed_id] = store

    def get(self, feed_id: str) -> ReplayStore:
        """Return the replay store for a feed."""
        return self._stores[feed_id]

    def stop_all(self) -> None:
        """Stop all registered replay stores."""
        for store in self._stores.values():
            store.stop()
