"""Phase 11.D — performance acceptance harness.

Drives the `ApplicationCoordinator` headlessly against synthetic feeds for a
fixed duration, captures per-feed telemetry + disk write rate + health events
to a JSON profile artifact, evaluates §16.3 pass/fail rules, and exits with
a status code reflecting the verdict.

Synthetic feeds run on the python_push pipeline path, so the harness measures
the *pipeline plumbing* (encoder branch, queue saturation, splitmuxsink
finalization, disk throughput), not real NDI ingest. The 720p@30 ceiling
called out in `AppSettings.target_frame_*` applies — runs at 1080p with the
synthetic source are expected to surface saturation.

Usage:
    python -m tools.perf_acceptance --feeds 2 --duration 60
    python -m tools.perf_acceptance --smoke         # 1 feed, 30s, fail-fast
    python -m tools.perf_acceptance --feeds 4 --resolution 1280x720 --fps 30 \\
        --duration 300 --data-dir C:/tmp/perf
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 1
DEFAULT_RETENTION = 50
DEFAULT_WARMUP_SECONDS = 3.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


# §16.3 pass/fail thresholds. Single source of truth so the tests can
# import them and the evaluator stays in sync with the spec.
SOURCE_FPS_TOLERANCE = 0.01      # within 1% of target
RECORDING_FPS_TOLERANCE = 0.01    # within 1% of source
QUEUE_SATURATION_MAX_PCT = 75.0   # peak ≤ 75%
DISALLOWED_HEALTH_CATEGORIES = (
    "recording_branch_saturated",
    "disk_full",
    "disk_full_imminent",
)


# ---------------------------------------------------------------------------
# Profile artifact + pure helpers (no app deps; safe to import from tests).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FeedStats:
    """Per-feed aggregated statistics for one harness run."""

    feed_id: str
    display_name: str
    pipeline_mode: str
    recording_encoder: str
    sample_count: int
    source_fps_p50: float
    source_fps_p95: float
    source_fps_min: float
    recording_fps_p50: float
    recording_fps_p95: float
    recording_fps_min: float
    dropped_per_sec_max: float
    queue_saturation_preview_max_pct: float
    queue_saturation_recording_max_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed_id": self.feed_id,
            "display_name": self.display_name,
            "pipeline_mode": self.pipeline_mode,
            "recording_encoder": self.recording_encoder,
            "sample_count": self.sample_count,
            "source_fps_p50": self.source_fps_p50,
            "source_fps_p95": self.source_fps_p95,
            "source_fps_min": self.source_fps_min,
            "recording_fps_p50": self.recording_fps_p50,
            "recording_fps_p95": self.recording_fps_p95,
            "recording_fps_min": self.recording_fps_min,
            "dropped_per_sec_max": self.dropped_per_sec_max,
            "queue_saturation_preview_max_pct": self.queue_saturation_preview_max_pct,
            "queue_saturation_recording_max_pct": self.queue_saturation_recording_max_pct,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedStats":
        return cls(
            feed_id=str(data["feed_id"]),
            display_name=str(data["display_name"]),
            pipeline_mode=str(data["pipeline_mode"]),
            recording_encoder=str(data["recording_encoder"]),
            sample_count=int(data["sample_count"]),
            source_fps_p50=float(data["source_fps_p50"]),
            source_fps_p95=float(data["source_fps_p95"]),
            source_fps_min=float(data["source_fps_min"]),
            recording_fps_p50=float(data["recording_fps_p50"]),
            recording_fps_p95=float(data["recording_fps_p95"]),
            recording_fps_min=float(data["recording_fps_min"]),
            dropped_per_sec_max=float(data["dropped_per_sec_max"]),
            queue_saturation_preview_max_pct=float(
                data["queue_saturation_preview_max_pct"]
            ),
            queue_saturation_recording_max_pct=float(
                data["queue_saturation_recording_max_pct"]
            ),
        )


@dataclass(slots=True)
class Profile:
    """Performance acceptance run summary written to disk as JSON."""

    schema_version: int
    hostname: str
    utc_iso: str
    feed_count: int
    resolution: str
    target_fps: float
    duration_seconds: float
    warmup_seconds: float
    disk_budget_mb_s: float
    disk_write_rate_p95_mb_s: float
    feeds: list[FeedStats]
    health_events: list[dict[str, Any]]
    passed: bool
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hostname": self.hostname,
            "utc_iso": self.utc_iso,
            "feed_count": self.feed_count,
            "resolution": self.resolution,
            "target_fps": self.target_fps,
            "duration_seconds": self.duration_seconds,
            "warmup_seconds": self.warmup_seconds,
            "disk_budget_mb_s": self.disk_budget_mb_s,
            "disk_write_rate_p95_mb_s": self.disk_write_rate_p95_mb_s,
            "feeds": [f.to_dict() for f in self.feeds],
            "health_events": list(self.health_events),
            "passed": self.passed,
            "failures": list(self.failures),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(
            schema_version=int(data["schema_version"]),
            hostname=str(data["hostname"]),
            utc_iso=str(data["utc_iso"]),
            feed_count=int(data["feed_count"]),
            resolution=str(data["resolution"]),
            target_fps=float(data["target_fps"]),
            duration_seconds=float(data["duration_seconds"]),
            warmup_seconds=float(data["warmup_seconds"]),
            disk_budget_mb_s=float(data["disk_budget_mb_s"]),
            disk_write_rate_p95_mb_s=float(data["disk_write_rate_p95_mb_s"]),
            feeds=[FeedStats.from_dict(f) for f in data["feeds"]],
            health_events=list(data.get("health_events", [])),
            passed=bool(data["passed"]),
            failures=list(data.get("failures", [])),
        )


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (ceil). Returns 0.0 for empty input."""
    import math

    if not values:
        return 0.0
    sorted_values = sorted(values)
    if pct <= 0:
        return sorted_values[0]
    if pct >= 100:
        return sorted_values[-1]
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def compute_feed_stats(
    feed_id: str,
    display_name: str,
    pipeline_mode: str,
    recording_encoder: str,
    source_fps: list[float],
    recording_fps: list[float],
    dropped_per_sec: list[float],
    preview_saturation_pct: list[float],
    recording_saturation_pct: list[float],
) -> FeedStats:
    """Aggregate per-feed sample lists into a `FeedStats`."""
    return FeedStats(
        feed_id=feed_id,
        display_name=display_name,
        pipeline_mode=pipeline_mode,
        recording_encoder=recording_encoder,
        sample_count=len(source_fps),
        source_fps_p50=_percentile(source_fps, 50),
        source_fps_p95=_percentile(source_fps, 95),
        source_fps_min=min(source_fps) if source_fps else 0.0,
        recording_fps_p50=_percentile(recording_fps, 50),
        recording_fps_p95=_percentile(recording_fps, 95),
        recording_fps_min=min(recording_fps) if recording_fps else 0.0,
        dropped_per_sec_max=max(dropped_per_sec) if dropped_per_sec else 0.0,
        queue_saturation_preview_max_pct=(
            max(preview_saturation_pct) if preview_saturation_pct else 0.0
        ),
        queue_saturation_recording_max_pct=(
            max(recording_saturation_pct) if recording_saturation_pct else 0.0
        ),
    )


def evaluate_pass_fail(
    feeds: list[FeedStats],
    health_events: list[dict[str, Any]],
    target_fps: float,
) -> tuple[bool, list[str]]:
    """Apply §16.3 pass/fail rules and return (passed, failure_messages).

    Rules (all must hold):
      * Each feed's source_fps_p50 within 1% of `target_fps`.
      * Each feed's recording_fps_p50 within 1% of its own source_fps_p50.
      * No feed's queue saturation (preview or recording) peak exceeds 75%.
      * No `recording_branch_saturated` / `disk_full*` events fired.
    """
    failures: list[str] = []
    if not feeds:
        failures.append("no feeds produced samples")
        return False, failures

    src_min = target_fps * (1.0 - SOURCE_FPS_TOLERANCE)
    for feed in feeds:
        if feed.source_fps_p50 < src_min:
            failures.append(
                f"feed {feed.feed_id}: source_fps_p50={feed.source_fps_p50:.2f} "
                f"below target_fps={target_fps:.2f} - 1% ({src_min:.2f})"
            )
        rec_min = feed.source_fps_p50 * (1.0 - RECORDING_FPS_TOLERANCE)
        if feed.recording_fps_p50 < rec_min:
            failures.append(
                f"feed {feed.feed_id}: recording_fps_p50={feed.recording_fps_p50:.2f} "
                f"below source_fps_p50={feed.source_fps_p50:.2f} - 1% ({rec_min:.2f})"
            )
        if feed.queue_saturation_preview_max_pct > QUEUE_SATURATION_MAX_PCT:
            failures.append(
                f"feed {feed.feed_id}: preview queue saturation peak "
                f"{feed.queue_saturation_preview_max_pct:.1f}% > "
                f"{QUEUE_SATURATION_MAX_PCT:.0f}%"
            )
        if feed.queue_saturation_recording_max_pct > QUEUE_SATURATION_MAX_PCT:
            failures.append(
                f"feed {feed.feed_id}: recording queue saturation peak "
                f"{feed.queue_saturation_recording_max_pct:.1f}% > "
                f"{QUEUE_SATURATION_MAX_PCT:.0f}%"
            )

    for event in health_events:
        category = str(event.get("category", ""))
        if category in DISALLOWED_HEALTH_CATEGORIES:
            failures.append(
                f"disallowed health event category={category} "
                f"feed_id={event.get('feed_id')!r} "
                f"message={event.get('message')!r}"
            )

    return not failures, failures


def enforce_retention(profile_dir: Path, max_count: int) -> list[Path]:
    """Keep newest `max_count` profile artifacts under `profile_dir`; delete the rest.

    Returns the list of paths that were removed.
    """
    if max_count < 0:
        raise ValueError("max_count must be non-negative")
    if not profile_dir.is_dir():
        return []
    artifacts = sorted(
        (p for p in profile_dir.iterdir() if p.is_file() and p.suffix == ".json"),
        key=lambda p: p.name,
    )
    if len(artifacts) <= max_count:
        return []
    to_delete = artifacts[: len(artifacts) - max_count]
    removed: list[Path] = []
    for path in to_delete:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            LOGGER.warning("could not remove old profile artifact %s", path)
    return removed


def find_latest_profile(profile_dir: Path) -> Path | None:
    """Return the newest `.json` artifact under `profile_dir`, or None."""
    if not profile_dir.is_dir():
        return None
    artifacts = sorted(
        (p for p in profile_dir.iterdir() if p.is_file() and p.suffix == ".json"),
        key=lambda p: p.name,
    )
    return artifacts[-1] if artifacts else None


def profile_filename(utc_iso: str, feed_count: int, resolution: str) -> str:
    """Build the canonical profile artifact filename.

    `utc_iso` colons are replaced with `-` for Windows filesystem safety.
    """
    safe = utc_iso.replace(":", "-")
    return f"{safe}_{feed_count}x{resolution}.json"


# ---------------------------------------------------------------------------
# Headless harness driver. Imports the app stack lazily so the pure helpers
# above stay importable from tests without the full media stack present.
# ---------------------------------------------------------------------------


def _build_app_settings(
    *,
    feed_count: int,
    width: int,
    height: int,
    fps: float,
    base_data_dir: Path,
    config_path: Path | None,
):
    """Build an `AppSettings` for the harness run.

    If `config_path` is given, load it as the baseline; otherwise start
    from `AppSettings()` defaults. Then override `base_data_dir`,
    `target_*`, and `feeds_table_rows` so the harness owns the run shape.
    """
    from app.config.settings import AppSettings

    settings = (
        AppSettings.load(config_path) if config_path is not None else AppSettings()
    )
    settings.base_data_dir = base_data_dir
    settings.target_frame_width = width
    settings.target_frame_height = height
    settings.target_fps = float(fps)
    # Synthetic source has no audio stream — splitmuxsink stalls if we
    # leave audio enabled (see comment on `recording_audio_enabled`).
    settings.recording_audio_enabled = False
    settings.enable_embedded_audio = False
    settings.live_audio_monitor_enabled = False

    settings.feeds_table_rows = [
        {
            "feed_id": f"perf_{i}",
            "display_name": f"Perf Feed {i}",
            "source_kind": "synthetic",
            "ndi_name": None,
            "enabled": True,
        }
        for i in range(feed_count)
    ]
    return settings


class PerfHarness:
    """Drive the coordinator headlessly and collect telemetry samples."""

    def __init__(
        self,
        *,
        feed_count: int,
        width: int,
        height: int,
        fps: float,
        duration_seconds: float,
        base_data_dir: Path,
        profile_out_dir: Path,
        hostname: str,
        config_path: Path | None = None,
        warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        retention: int = DEFAULT_RETENTION,
    ) -> None:
        if feed_count < 1:
            raise ValueError("feed_count must be ≥ 1")
        if duration_seconds <= warmup_seconds:
            raise ValueError(
                f"duration_seconds ({duration_seconds}) must exceed "
                f"warmup_seconds ({warmup_seconds})"
            )
        self.feed_count = feed_count
        self.width = width
        self.height = height
        self.fps = float(fps)
        self.duration_seconds = float(duration_seconds)
        self.base_data_dir = base_data_dir
        self.profile_out_dir = profile_out_dir
        self.hostname = hostname
        self.config_path = config_path
        self.warmup_seconds = float(warmup_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.retention = retention

        # Populated during run().
        self._samples: list[tuple[float, list[Any]]] = []
        self._disk_samples: list[float] = []
        self._recording_started_monotonic: float | None = None
        self._coordinator: Any | None = None
        self._qt_app: Any | None = None
        self._session_logs_dir: Path | None = None

    def run(self) -> Profile:
        """Run the harness end-to-end and return the populated `Profile`."""
        # Lazy imports — keep the top of the module importable in tests
        # that don't have the media stack.
        from PySide6.QtCore import QCoreApplication, QTimer

        from app.core.application_coordinator import (
            _qt_periodic_registrar,
            build_default_application_coordinator,
        )
        from app.media.output_renderer import MultiFeedOutputRenderer
        from app.storage.file_manager import FileManager
        from app.storage.metadata_db import MetadataDb
        from app.storage.session_manager import SessionManager

        settings = _build_app_settings(
            feed_count=self.feed_count,
            width=self.width,
            height=self.height,
            fps=self.fps,
            base_data_dir=self.base_data_dir,
            config_path=self.config_path,
        )

        self.base_data_dir.mkdir(parents=True, exist_ok=True)

        qt_app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        self._qt_app = qt_app

        file_manager = FileManager(settings)
        metadata_db = MetadataDb(settings.metadata_db_path)
        session_manager = SessionManager(file_manager, metadata_db)

        operator_renderer = MultiFeedOutputRenderer()
        program_renderer = MultiFeedOutputRenderer()

        coordinator = build_default_application_coordinator(
            settings,
            session_manager,
            operator_renderer=operator_renderer,
            program_renderer=program_renderer,
        )
        self._coordinator = coordinator

        coordinator.initialize(resume_session_id=None)

        session_paths = session_manager.get_active_session_paths()
        if session_paths is None or session_paths.logs_dir is None:
            raise RuntimeError(
                "harness initialization left no active session paths"
            )
        self._session_logs_dir = session_paths.logs_dir

        coordinator.telemetry_hub.start(_qt_periodic_registrar)

        # Poll telemetry once per second.
        poll_timer = QTimer()
        poll_timer.setInterval(int(self.poll_interval_seconds * 1000))
        poll_timer.timeout.connect(self._on_poll_tick)
        poll_timer.start()

        # Start recording after a brief warmup so feeds are producing
        # frames before splitmuxsink wires up the file branch.
        QTimer.singleShot(
            int(self.warmup_seconds * 1000), self._start_recording
        )

        # Stop after the configured duration.
        QTimer.singleShot(int(self.duration_seconds * 1000), qt_app.quit)

        run_started_monotonic = time.monotonic()
        qt_app.exec()
        poll_timer.stop()

        # Finalize: drain recording, stop telemetry, close session.
        try:
            coordinator.shutdown()
        except Exception:
            LOGGER.exception("coordinator shutdown raised; continuing to write profile")

        utc_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        profile = self._build_profile(
            run_started_monotonic=run_started_monotonic,
            utc_iso=utc_iso,
            disk_budget_mb_s=settings.disk_budget_mb_s,
        )
        return profile

    # -- internal callbacks ------------------------------------------------

    def _start_recording(self) -> None:
        coordinator = self._coordinator
        if coordinator is None:
            return
        try:
            coordinator.toggle_long_session_recording()
            self._recording_started_monotonic = time.monotonic()
            LOGGER.info("perf-acceptance: recording started")
        except Exception:
            LOGGER.exception("perf-acceptance: failed to start recording")

    def _on_poll_tick(self) -> None:
        coordinator = self._coordinator
        if coordinator is None:
            return
        try:
            snaps = coordinator.telemetry_hub.snapshot()
        except Exception:
            LOGGER.exception("perf-acceptance: snapshot failed")
            return
        self._samples.append((time.monotonic(), snaps))
        try:
            disk = coordinator.telemetry_hub.disk_snapshot()
        except Exception:
            disk = None
        if disk is not None and getattr(disk, "available", False):
            self._disk_samples.append(float(disk.write_mb_s_estimate or 0.0))

    # -- aggregation -------------------------------------------------------

    def _build_profile(
        self,
        *,
        run_started_monotonic: float,
        utc_iso: str,
        disk_budget_mb_s: float,
    ) -> Profile:
        # Aggregate samples per feed, gating recording_fps on samples
        # taken after recording was successfully started.
        warmup_cutoff = run_started_monotonic + self.warmup_seconds
        rec_cutoff = (
            self._recording_started_monotonic + self.warmup_seconds
            if self._recording_started_monotonic is not None
            else None
        )

        per_feed_source: dict[str, list[float]] = {}
        per_feed_recording: dict[str, list[float]] = {}
        per_feed_dropped: dict[str, list[float]] = {}
        per_feed_prev_sat: dict[str, list[float]] = {}
        per_feed_rec_sat: dict[str, list[float]] = {}
        per_feed_meta: dict[str, tuple[str, str, str]] = {}

        for sample_t, snaps in self._samples:
            if sample_t < warmup_cutoff:
                continue
            include_recording = rec_cutoff is not None and sample_t >= rec_cutoff
            for snap in snaps:
                feed_id = snap.feed_id
                per_feed_source.setdefault(feed_id, []).append(float(snap.source_fps))
                if include_recording:
                    per_feed_recording.setdefault(feed_id, []).append(
                        float(snap.recording_fps)
                    )
                per_feed_dropped.setdefault(feed_id, []).append(
                    float(snap.dropped_per_sec)
                )
                per_feed_prev_sat.setdefault(feed_id, []).append(
                    float(snap.preview_queue_saturation_pct)
                )
                per_feed_rec_sat.setdefault(feed_id, []).append(
                    float(snap.record_queue_saturation_pct)
                )
                per_feed_meta[feed_id] = (
                    snap.display_name,
                    snap.pipeline_mode,
                    snap.recording_encoder,
                )

        feeds_stats: list[FeedStats] = []
        for feed_id in sorted(per_feed_source.keys()):
            display, mode, encoder = per_feed_meta.get(feed_id, (feed_id, "", ""))
            feeds_stats.append(
                compute_feed_stats(
                    feed_id=feed_id,
                    display_name=display,
                    pipeline_mode=mode,
                    recording_encoder=encoder,
                    source_fps=per_feed_source.get(feed_id, []),
                    recording_fps=per_feed_recording.get(feed_id, []),
                    dropped_per_sec=per_feed_dropped.get(feed_id, []),
                    preview_saturation_pct=per_feed_prev_sat.get(feed_id, []),
                    recording_saturation_pct=per_feed_rec_sat.get(feed_id, []),
                )
            )

        events = _read_health_events(self._session_logs_dir)
        passed, failures = evaluate_pass_fail(
            feeds_stats, events, target_fps=self.fps
        )

        return Profile(
            schema_version=PROFILE_SCHEMA_VERSION,
            hostname=self.hostname,
            utc_iso=utc_iso,
            feed_count=self.feed_count,
            resolution=f"{self.width}x{self.height}",
            target_fps=self.fps,
            duration_seconds=self.duration_seconds,
            warmup_seconds=self.warmup_seconds,
            disk_budget_mb_s=float(disk_budget_mb_s),
            disk_write_rate_p95_mb_s=_percentile(self._disk_samples, 95),
            feeds=feeds_stats,
            health_events=events,
            passed=passed,
            failures=failures,
        )


def _read_health_events(logs_dir: Path | None) -> list[dict[str, Any]]:
    """Read `health_events.jsonl` from a session's logs dir.

    Returns an empty list when the file is missing or unreadable.
    """
    if logs_dir is None:
        return []
    path = logs_dir / "health_events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    LOGGER.warning("could not parse health-event line: %r", line)
    except OSError:
        LOGGER.warning("could not read %s", path)
    return events


def write_profile(profile: Profile, profile_dir: Path) -> Path:
    """Write `profile` to its canonical filename under `profile_dir`."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    filename = profile_filename(
        profile.utc_iso, profile.feed_count, profile.resolution
    )
    path = profile_dir / filename
    payload = json.dumps(profile.to_dict(), indent=2, sort_keys=True)
    path.write_text(payload, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_resolution(value: str) -> tuple[int, int]:
    parts = value.lower().split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"resolution must be WIDTHxHEIGHT, got {value!r}"
        )
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"resolution must be WIDTHxHEIGHT integers, got {value!r}"
        ) from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(
            f"resolution dimensions must be positive, got {value!r}"
        )
    return width, height


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_acceptance",
        description=(
            "Phase 11.D performance acceptance harness. Drives the "
            "ApplicationCoordinator headlessly against synthetic feeds and "
            "writes a profile JSON artifact under "
            "<base-data-dir>/perf_profiles/<hostname>/."
        ),
    )
    parser.add_argument(
        "--feeds",
        type=int,
        default=1,
        help="Number of synthetic feeds (default: 1).",
    )
    parser.add_argument(
        "--resolution",
        type=_parse_resolution,
        default=(1280, 720),
        help="WIDTHxHEIGHT (default: 1280x720). Synthetic source caps at 720p30.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Target FPS (default: 30).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Run duration in seconds (default: 300).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Smoke-test mode: 1 feed, 30s duration, fail-fast. Overrides "
            "--feeds and --duration."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Optional app_settings.toml path used as the baseline before "
            "the harness overrides feeds/resolution/fps."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Override base_data_dir. Defaults to "
            "<cwd>/perf_acceptance_data so harness runs do not mingle "
            "with operator session data."
        ),
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help=(
            "Override profile-artifact directory. Defaults to "
            "<base-data-dir>/perf_profiles/<hostname>/."
        ),
    )
    parser.add_argument(
        "--hostname",
        type=str,
        default=None,
        help="Override hostname recorded in the profile (default: socket.gethostname()).",
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=DEFAULT_RETENTION,
        help=f"Profile retention count per host (default: {DEFAULT_RETENTION}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    feeds = args.feeds
    duration = args.duration
    if args.smoke:
        feeds = 1
        duration = 30.0

    width, height = args.resolution
    hostname = args.hostname or socket.gethostname() or "unknown-host"

    base_data_dir = args.data_dir or (Path.cwd() / "perf_acceptance_data")
    profile_dir = args.profile_dir or (
        base_data_dir / "perf_profiles" / hostname
    )

    harness = PerfHarness(
        feed_count=feeds,
        width=width,
        height=height,
        fps=args.fps,
        duration_seconds=duration,
        base_data_dir=base_data_dir,
        profile_out_dir=profile_dir,
        hostname=hostname,
        config_path=args.config,
        retention=args.retention,
    )

    profile = harness.run()
    artifact_path = write_profile(profile, profile_dir)
    enforce_retention(profile_dir, harness.retention)

    LOGGER.info(
        "perf-acceptance: %s — wrote %s",
        "PASS" if profile.passed else "FAIL",
        artifact_path,
    )
    if not profile.passed:
        for message in profile.failures:
            LOGGER.error("perf-acceptance failure: %s", message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
