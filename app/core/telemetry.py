"""Per-feed runtime telemetry counters and 1Hz snapshot logging.

This module is the foundation for the production-architecture observability
work (§12.1 / Phase 1). It is deliberately stdlib-only so it can be unit-tested
without Qt or GStreamer present.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import math
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Optional

from app.core.feed_state import FeedState
from app.core.health_events import HealthEventLog, HealthSeverity, default_log
from app.core.state_machine import StateMachine

LOGGER = logging.getLogger(__name__)

FEED_LOST_ZERO_SAMPLES = 3
DISK_LOW_FRACTION = 0.05
DROPPED_BUFFERS_DEGRADED_THRESHOLD = 1.0  # buffers/sec sustained → DEGRADED


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
    dropped_per_sec: float = 0.0


class FeedMetrics:
    """Per-feed counters wired at the ingest, preview, and recording seams.

    `tick_source` fires at the head of the per-feed pipeline (one event per
    inbound frame, regardless of branch). `tick_preview` and `tick_recording`
    fire on the respective tee branch sinks. `tick_dropped` fires once per
    GStreamer QOS bus message on the live pipeline. `recording_fps` is
    expected to be zero whenever the operator has not started long-form game
    recording.
    """

    __slots__ = ("feed_id", "display_name", "_source", "_preview", "_recording", "_dropped")

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
        self._dropped = RateCounter(window_seconds, clock=clock)

    def tick_source(self) -> None:
        self._source.tick()

    def tick_preview(self) -> None:
        self._preview.tick()

    def tick_recording(self) -> None:
        self._recording.tick()

    def tick_dropped(self) -> None:
        self._dropped.tick()

    def snapshot(self) -> FeedMetricsSnapshot:
        return FeedMetricsSnapshot(
            feed_id=self.feed_id,
            display_name=self.display_name,
            source_fps=self._source.rate(),
            preview_fps=self._preview.rate(),
            recording_fps=self._recording.rate(),
            dropped_per_sec=self._dropped.rate(),
        )


@dataclass(frozen=True, slots=True)
class LatencySnapshot:
    """Roll-up of one named latency sampler over the trailing window."""

    name: str
    count: int
    avg_ms: float
    max_ms: float
    p95_ms: float


class LatencySampler:
    """Rolling-window latency aggregator for one named operation.

    `record(duration_seconds)` adds a sample; `snapshot()` returns the count,
    mean, max, and p95 over the trailing `window_seconds`. Thread-safe.
    """

    __slots__ = ("name", "_window", "_samples", "_lock", "_clock")

    def __init__(
        self,
        name: str,
        *,
        window_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not name:
            raise ValueError("name must be non-empty")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.name = name
        self._window = float(window_seconds)
        self._samples: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic

    def record(self, duration_seconds: float) -> None:
        if duration_seconds < 0:
            return
        now = self._clock()
        with self._lock:
            self._samples.append((now, duration_seconds))
            self._evict_locked(now)

    def snapshot(self) -> LatencySnapshot:
        now = self._clock()
        with self._lock:
            self._evict_locked(now)
            if not self._samples:
                return LatencySnapshot(name=self.name, count=0, avg_ms=0.0, max_ms=0.0, p95_ms=0.0)
            durations = sorted(d for _, d in self._samples)
        count = len(durations)
        avg = sum(durations) / count
        max_d = durations[-1]
        p95_index = max(0, int(math.ceil(0.95 * count)) - 1)
        p95 = durations[p95_index]
        return LatencySnapshot(
            name=self.name,
            count=count,
            avg_ms=avg * 1000.0,
            max_ms=max_d * 1000.0,
            p95_ms=p95 * 1000.0,
        )

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self._window
        samples = self._samples
        while samples and samples[0][0] < cutoff:
            samples.popleft()


class LatencyRegistry:
    """Thread-safe registry of named latency samplers."""

    def __init__(self, *, window_seconds: float = 5.0) -> None:
        self._window = window_seconds
        self._samplers: dict[str, LatencySampler] = {}
        self._lock = threading.Lock()

    def sampler(self, name: str) -> LatencySampler:
        with self._lock:
            existing = self._samplers.get(name)
            if existing is not None:
                return existing
            sampler = LatencySampler(name, window_seconds=self._window)
            self._samplers[name] = sampler
            return sampler

    def snapshots(self) -> list[LatencySnapshot]:
        with self._lock:
            samplers = list(self._samplers.values())
        return [s.snapshot() for s in samplers]

    def reset(self) -> None:
        with self._lock:
            self._samplers.clear()


_LATENCY_REGISTRY = LatencyRegistry()


def record_latency(name: str, duration_seconds: float) -> None:
    """Record one latency sample under `name` in the global registry."""
    _LATENCY_REGISTRY.sampler(name).record(duration_seconds)


def latency_snapshots() -> list[LatencySnapshot]:
    """Snapshot every named latency sampler in the global registry."""
    return _LATENCY_REGISTRY.snapshots()


def reset_latency_registry() -> None:
    """Drop all global latency samplers. For test isolation."""
    _LATENCY_REGISTRY.reset()


@contextmanager
def time_block(name: str) -> Iterator[None]:
    """Time the body and record under `name` in the global registry.

        with time_block("segment_write_video"):
            writer.push_buffer(buf)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        record_latency(name, time.monotonic() - start)


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
        health_log: HealthEventLog | None = None,
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
        self._health_log = health_log if health_log is not None else default_log()
        self._zero_fps_streak: dict[str, int] = {}
        self._feed_states: dict[str, StateMachine[FeedState]] = {}

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

    def register_feed_state(
        self, feed_id: str, state_machine: StateMachine[FeedState]
    ) -> None:
        """Attach a per-feed state machine so the hub can drive transitions."""
        with self._lock:
            self._feed_states[feed_id] = state_machine

    def feed_state(self, feed_id: str) -> StateMachine[FeedState] | None:
        with self._lock:
            return self._feed_states.get(feed_id)

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
        """Emit periodic INFO log lines for feeds and latency samplers."""
        for snap in self.snapshot():
            LOGGER.info(
                "telemetry feed_id=%s source_fps=%.2f preview_fps=%.2f recording_fps=%.2f",
                snap.feed_id,
                snap.source_fps,
                snap.preview_fps,
                snap.recording_fps,
            )
            self._evaluate_feed_health(snap)
        for lat in latency_snapshots():
            if lat.count == 0:
                continue
            LOGGER.info(
                "telemetry latency name=%s count=%d avg_ms=%.2f p95_ms=%.2f max_ms=%.2f",
                lat.name,
                lat.count,
                lat.avg_ms,
                lat.p95_ms,
                lat.max_ms,
            )

    def _evaluate_feed_health(self, snap: FeedMetricsSnapshot) -> None:
        """Drive the per-feed state machine from observed counters.

        Two heuristics run here:
          - sustained zero source-fps (`FEED_LOST_ZERO_SAMPLES`) → DISCONNECTED.
            Acts as a fallback for sources that never raise a bus ERROR.
          - sustained QOS drops (`DROPPED_BUFFERS_DEGRADED_THRESHOLD`/sec) →
            DEGRADED.

        Health events for `feed_lost` / `feed_recovered` / `feed_degraded`
        are emitted by the state machine's `on_enter` hook (see
        `feed_state.make_feed_state_machine`); the hub does not record them
        directly.
        """
        feed_state = self.feed_state(snap.feed_id)

        if snap.source_fps == 0.0:
            streak = self._zero_fps_streak.get(snap.feed_id, 0) + 1
            self._zero_fps_streak[snap.feed_id] = streak
            if streak == FEED_LOST_ZERO_SAMPLES:
                if feed_state is not None:
                    if feed_state.state in {FeedState.LIVE, FeedState.DEGRADED}:
                        feed_state.transition_to(FeedState.DISCONNECTED)
                elif not self._health_log.has_open_event(
                    category="feed_lost", feed_id=snap.feed_id
                ):
                    # Legacy path for hubs without a registered state machine
                    # (kept for tests and tooling that exercise the hub
                    # directly).
                    self._health_log.record(
                        severity=HealthSeverity.WARNING,
                        category="feed_lost",
                        message=(
                            f"No frames observed from {snap.display_name} "
                            f"for {streak}s."
                        ),
                        feed_id=snap.feed_id,
                    )
            return

        # source_fps > 0
        if self._zero_fps_streak.get(snap.feed_id):
            self._zero_fps_streak[snap.feed_id] = 0

        if feed_state is None:
            # Legacy recovery path (no state machine registered).
            if self._health_log.has_open_event(
                category="feed_lost", feed_id=snap.feed_id
            ):
                self._health_log.record(
                    severity=HealthSeverity.INFO,
                    category="feed_recovered",
                    message=f"Frames resumed from {snap.display_name}.",
                    feed_id=snap.feed_id,
                )
                self._health_log.clear_open_event(
                    category="feed_lost", feed_id=snap.feed_id
                )
            return

        # State-machine driven recovery + degradation evaluation.
        if snap.dropped_per_sec >= DROPPED_BUFFERS_DEGRADED_THRESHOLD:
            if feed_state.state == FeedState.LIVE:
                feed_state.transition_to(FeedState.DEGRADED)
        else:
            if feed_state.state == FeedState.DEGRADED:
                feed_state.transition_to(FeedState.LIVE)

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
        self._evaluate_disk_health(snap)

    def _evaluate_disk_health(self, snap: DiskSnapshot) -> None:
        """Emit a health event when free disk space drops below the threshold."""
        if snap.total_bytes <= 0:
            return
        fraction_free = snap.free_bytes / snap.total_bytes
        if fraction_free < DISK_LOW_FRACTION:
            if not self._health_log.has_open_event(category="disk_low", feed_id=None):
                self._health_log.record(
                    severity=HealthSeverity.WARNING,
                    category="disk_low",
                    message=(
                        f"Disk free space is {fraction_free * 100:.1f}% on {snap.path}."
                    ),
                    metadata={
                        "path": snap.path,
                        "free_bytes": snap.free_bytes,
                        "total_bytes": snap.total_bytes,
                    },
                )
        elif self._health_log.has_open_event(category="disk_low", feed_id=None):
            self._health_log.record(
                severity=HealthSeverity.INFO,
                category="disk_recovered",
                message=f"Disk free space recovered to {fraction_free * 100:.1f}%.",
                metadata={"path": snap.path},
            )
            self._health_log.clear_open_event(category="disk_low", feed_id=None)
