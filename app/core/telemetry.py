"""Per-feed runtime telemetry counters and 1Hz snapshot logging.

This module is the foundation for the production-architecture observability
work (§12.1 / Phase 1). It is deliberately stdlib-only so it can be unit-tested
without Qt or GStreamer present.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Optional

LOGGER = logging.getLogger(__name__)


class RateCounter:
    """Rolling-window event-rate counter using monotonic time.

    `tick()` records that one event happened. `rate()` returns the mean rate
    over the trailing `window_seconds`. Thread-safe under a single internal
    lock so it is safe to call from GStreamer streaming threads.
    """

    __slots__ = ("_window", "_events", "_lock", "_clock")

    def __init__(
        self,
        window_seconds: float = 5.0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._window = float(window_seconds)
        self._events: deque[float] = deque()
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic

    def tick(self) -> None:
        """Record one event at the current monotonic time."""
        now = self._clock()
        with self._lock:
            self._events.append(now)
            self._evict_locked(now)

    def rate(self) -> float:
        """Return events per second over the trailing window."""
        now = self._clock()
        with self._lock:
            self._evict_locked(now)
            count = len(self._events)
            if count == 0:
                return 0.0
            return count / self._window

    def reset(self) -> None:
        """Drop all recorded events."""
        with self._lock:
            self._events.clear()

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self._window
        events = self._events
        while events and events[0] < cutoff:
            events.popleft()


@dataclass(frozen=True, slots=True)
class FeedMetricsSnapshot:
    """Read-only view of one feed's counters at a moment in time."""

    feed_id: str
    display_name: str
    source_fps: float
    preview_fps: float
    recording_fps: float


class FeedMetrics:
    """Per-feed counters wired at the ingest, preview, and recording seams.

    `tick_source` fires at the head of the per-feed pipeline (one event per
    inbound frame, regardless of branch). `tick_preview` and `tick_recording`
    fire on the respective tee branch sinks. `recording_fps` is expected to
    be zero whenever the operator has not started long-form game recording.
    """

    __slots__ = ("feed_id", "display_name", "_source", "_preview", "_recording")

    def __init__(
        self,
        feed_id: str,
        display_name: str,
        *,
        window_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.feed_id = feed_id
        self.display_name = display_name
        self._source = RateCounter(window_seconds, clock=clock)
        self._preview = RateCounter(window_seconds, clock=clock)
        self._recording = RateCounter(window_seconds, clock=clock)

    def tick_source(self) -> None:
        self._source.tick()

    def tick_preview(self) -> None:
        self._preview.tick()

    def tick_recording(self) -> None:
        self._recording.tick()

    def snapshot(self) -> FeedMetricsSnapshot:
        return FeedMetricsSnapshot(
            feed_id=self.feed_id,
            display_name=self.display_name,
            source_fps=self._source.rate(),
            preview_fps=self._preview.rate(),
            recording_fps=self._recording.rate(),
        )


class TelemetryHub:
    """Owner of per-feed metrics + a 1Hz log line per feed.

    The hub is intentionally decoupled from Qt: `start()` accepts a periodic-
    callback registrar so the production app can pass a `QTimer`-backed driver
    while tests can drive `_log_all_snapshots` directly.
    """

    def __init__(self, *, log_interval_seconds: float = 1.0) -> None:
        if log_interval_seconds <= 0:
            raise ValueError("log_interval_seconds must be positive")
        self._log_interval = float(log_interval_seconds)
        self._feeds: dict[str, FeedMetrics] = {}
        self._lock = threading.Lock()
        self._stop_periodic: Callable[[], None] | None = None

    def register(self, feed_id: str, display_name: str) -> FeedMetrics:
        """Create and store a `FeedMetrics` for `feed_id`."""
        if not feed_id:
            raise ValueError("feed_id must be non-empty")
        with self._lock:
            if feed_id in self._feeds:
                return self._feeds[feed_id]
            metrics = FeedMetrics(feed_id=feed_id, display_name=display_name)
            self._feeds[feed_id] = metrics
            return metrics

    def metrics_for(self, feed_id: str) -> Optional[FeedMetrics]:
        with self._lock:
            return self._feeds.get(feed_id)

    def snapshot(self) -> list[FeedMetricsSnapshot]:
        """Snapshot every registered feed in registration order."""
        with self._lock:
            return [m.snapshot() for m in self._feeds.values()]

    def start(
        self,
        periodic_registrar: Callable[[float, Callable[[], None]], Callable[[], None]],
    ) -> None:
        """Begin 1Hz snapshot logging.

        `periodic_registrar(interval_seconds, callback) -> cancel_fn` schedules
        `callback` to fire every `interval_seconds`; the returned `cancel_fn`
        stops it. The production caller wraps a `QTimer`; tests can pass a
        no-op or drive `_log_all_snapshots` manually.
        """
        if self._stop_periodic is not None:
            return
        self._stop_periodic = periodic_registrar(self._log_interval, self._log_all_snapshots)

    def stop(self) -> None:
        if self._stop_periodic is not None:
            try:
                self._stop_periodic()
            finally:
                self._stop_periodic = None

    def _log_all_snapshots(self) -> None:
        """Emit one INFO log line per registered feed."""
        for snap in self.snapshot():
            LOGGER.info(
                "telemetry feed_id=%s source_fps=%.2f preview_fps=%.2f recording_fps=%.2f",
                snap.feed_id,
                snap.source_fps,
                snap.preview_fps,
                snap.recording_fps,
            )
