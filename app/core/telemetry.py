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
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Optional

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


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    """Read-only view of disk capacity and recent write rate."""

    path: str
    available: bool
    free_bytes: int = 0
    total_bytes: int = 0
    write_mb_s_estimate: float = 0.0


class DiskSampler:
    """Free-space + sustained-write-rate sampler for one filesystem path.

    Write rate is estimated from the change in `shutil.disk_usage(path).free`
    between successive `sample()` calls. This intentionally captures *all*
    writes to the underlying volume, not just this app's writes — for Phase 1
    the goal is "is the disk too slow?", not per-process accounting. Negative
    deltas (a delete elsewhere on the volume) clamp to 0.

    The sampler is safe to construct before `path` exists; the first sample
    after the path appears returns `available=True` with `write_mb_s = 0.0`
    (no baseline yet).
    """

    __slots__ = ("_path", "_clock", "_disk_usage_fn", "_lock", "_last_free", "_last_time")

    def __init__(
        self,
        path: Path | str | None,
        *,
        clock: Callable[[], float] | None = None,
        disk_usage_fn: Callable[[Any], Any] | None = None,
    ) -> None:
        self._path: Path | None = Path(path) if path is not None else None
        self._clock = clock or time.monotonic
        self._disk_usage_fn = disk_usage_fn or shutil.disk_usage
        self._lock = threading.Lock()
        self._last_free: int | None = None
        self._last_time: float | None = None

    def set_path(self, path: Path | str | None) -> None:
        """Change the path being sampled and reset baseline state."""
        with self._lock:
            self._path = Path(path) if path is not None else None
            self._last_free = None
            self._last_time = None

    def sample(self) -> DiskSnapshot:
        with self._lock:
            path = self._path
        if path is None:
            return DiskSnapshot(path="", available=False)
        try:
            usage = self._disk_usage_fn(path)
        except (OSError, FileNotFoundError):
            with self._lock:
                self._last_free = None
                self._last_time = None
            return DiskSnapshot(path=str(path), available=False)

        now = self._clock()
        write_rate = 0.0
        with self._lock:
            if self._last_free is not None and self._last_time is not None:
                dt = now - self._last_time
                if dt > 0:
                    bytes_consumed = self._last_free - usage.free
                    if bytes_consumed > 0:
                        write_rate = bytes_consumed / dt / (1024.0 * 1024.0)
            self._last_free = usage.free
            self._last_time = now

        return DiskSnapshot(
            path=str(path),
            available=True,
            free_bytes=usage.free,
            total_bytes=usage.total,
            write_mb_s_estimate=write_rate,
        )


class TelemetryHub:
    """Owner of per-feed metrics, disk metrics, and periodic log emission.

    The hub is intentionally decoupled from Qt: `start()` accepts a periodic-
    callback registrar so the production app can pass a `QTimer`-backed driver
    while tests can drive the log methods directly.
    """

    def __init__(
        self,
        *,
        log_interval_seconds: float = 1.0,
        disk_interval_seconds: float = 5.0,
    ) -> None:
        if log_interval_seconds <= 0:
            raise ValueError("log_interval_seconds must be positive")
        if disk_interval_seconds <= 0:
            raise ValueError("disk_interval_seconds must be positive")
        self._log_interval = float(log_interval_seconds)
        self._disk_interval = float(disk_interval_seconds)
        self._feeds: dict[str, FeedMetrics] = {}
        self._disk_sampler: DiskSampler | None = None
        self._latest_disk_snapshot: DiskSnapshot | None = None
        self._lock = threading.Lock()
        self._stop_callbacks: list[Callable[[], None]] = []
        self._started = False

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

    def set_disk_path(
        self,
        path: Path | str | None,
        *,
        sampler: DiskSampler | None = None,
    ) -> None:
        """Configure the disk sampler.

        Pass an explicit `sampler` for tests; otherwise a default
        `DiskSampler` is constructed for `path`.
        """
        with self._lock:
            if sampler is not None:
                self._disk_sampler = sampler
            elif self._disk_sampler is None:
                self._disk_sampler = DiskSampler(path)
            else:
                self._disk_sampler.set_path(path)
            self._latest_disk_snapshot = None

    def disk_snapshot(self) -> DiskSnapshot | None:
        """Return the most recent disk sample, or None if never sampled."""
        with self._lock:
            return self._latest_disk_snapshot

    def start(
        self,
        periodic_registrar: Callable[[float, Callable[[], None]], Callable[[], None]],
    ) -> None:
        """Begin periodic log emission.

        `periodic_registrar(interval_seconds, callback) -> cancel_fn` schedules
        `callback` to fire every `interval_seconds`; the returned `cancel_fn`
        stops it. The hub registers two callbacks: the per-feed log at
        `log_interval_seconds`, and (if a disk sampler is configured) the disk
        log at `disk_interval_seconds`.
        """
        if self._started:
            return
        self._stop_callbacks.append(
            periodic_registrar(self._log_interval, self._log_all_snapshots)
        )
        if self._disk_sampler is not None:
            self._stop_callbacks.append(
                periodic_registrar(self._disk_interval, self._log_disk_snapshot)
            )
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        callbacks = list(self._stop_callbacks)
        self._stop_callbacks.clear()
        self._started = False
        for cancel in callbacks:
            try:
                cancel()
            except Exception:
                LOGGER.debug("telemetry cancel raised", exc_info=True)

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

    def _log_disk_snapshot(self) -> None:
        """Sample disk usage and emit one INFO log line."""
        sampler = self._disk_sampler
        if sampler is None:
            return
        snap = sampler.sample()
        with self._lock:
            self._latest_disk_snapshot = snap
        if not snap.available:
            LOGGER.info("telemetry disk path=%s available=false", snap.path or "?")
            return
        free_gb = snap.free_bytes / (1024.0 ** 3)
        total_gb = snap.total_bytes / (1024.0 ** 3)
        LOGGER.info(
            "telemetry disk path=%s free_gb=%.2f total_gb=%.2f write_mb_s=%.2f",
            snap.path,
            free_gb,
            total_gb,
            snap.write_mb_s_estimate,
        )
