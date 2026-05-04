"""Disk-write budget estimation for the recording branch (Phase 7.A).

Each enabled feed writes one MJPEG-in-MKV stream through splitmuxsink.
If aggregate write throughput exceeds what the configured disk can
sustain, splitmuxsink stalls — the symptom is a silently-wedged
recording (segments stop rotating, replay is stuck at the last
finalized segment, preview keeps running because that branch is leaky).

This module estimates aggregate throughput from the configured frame
size, fps, codec, and feed count, and compares it against a configured
budget. The estimate is intentionally conservative — real safe
throughput is hardware- and content-dependent — so the goal is to
catch obviously-misconfigured rigs at startup, not to predict actual
write rates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from app.config.settings import AppSettings
    from app.core.models import FeedDefinition


class BudgetVerdict(str, Enum):
    """Outcome of comparing estimated write throughput against budget."""

    OK = "ok"
    WARN = "warn"
    OVER_BUDGET = "over_budget"


# Compression ratio against raw 24-bit RGB. Multiplied by
# (width × height × fps × 3 bytes/pixel) to get bytes/sec.
#
# MJPEG @ jpegenc default quality 85 on typical broadcast content
# compresses ~10:1 against raw 24-bit RGB, i.e. coefficient ≈ 0.10.
#
# Phase 11.B: ProRes 422 LT sits at the conservative end of the
# §5.2 range (0.20–0.25); DNxHR is one coefficient until 11.C
# introduces profile-aware tuning (DNxHR LB ≈ 0.18, HQ ≈ 0.45 —
# 0.30 is a midpoint estimate that won't underestimate at HQ).
_CODEC_RATIO_VS_RAW_RGB: dict[str, float] = {
    "mjpeg": 0.10,
    "prores": 0.25,
    "dnxhr": 0.30,
}

_RAW_BYTES_PER_PIXEL = 3.0  # 24-bit RGB


WARN_RATIO = 0.80
OVER_RATIO = 1.00


def estimate_per_feed_mb_s(
    target_frame_width: int,
    target_frame_height: int,
    target_fps: float,
    codec: str,
) -> float:
    """Return the estimated MB/s a single feed will write."""
    codec_key = codec.strip().lower()
    if codec_key not in _CODEC_RATIO_VS_RAW_RGB:
        raise NotImplementedError(
            f"Disk-budget estimator has no coefficient for codec={codec_key!r}. "
            f"Add an entry to _CODEC_RATIO_VS_RAW_RGB."
        )
    ratio = _CODEC_RATIO_VS_RAW_RGB[codec_key]
    pixels = max(0, int(target_frame_width)) * max(0, int(target_frame_height))
    raw_bytes_per_second = (
        pixels * max(0.0, float(target_fps)) * _RAW_BYTES_PER_PIXEL
    )
    return (raw_bytes_per_second * ratio) / (1024.0 * 1024.0)


def estimate_total_mb_s(
    enabled_feeds: "Iterable[FeedDefinition]",
    settings: "AppSettings",
) -> float:
    """Sum the per-feed estimate across all enabled feeds.

    All feeds currently share the same target frame size / fps from
    `AppSettings`, so the total is just per-feed × feed count. The
    signature accepts a feed iterable rather than a count so a future
    per-feed override (different resolutions per feed) can be added
    here without changing call sites.
    """
    feed_count = sum(1 for _ in enabled_feeds)
    if feed_count == 0:
        return 0.0
    per_feed = estimate_per_feed_mb_s(
        target_frame_width=settings.target_frame_width,
        target_frame_height=settings.target_frame_height,
        target_fps=settings.target_fps,
        codec=settings.recording_codec,
    )
    return per_feed * feed_count


def validate_budget(estimated_mb_s: float, budget_mb_s: float) -> BudgetVerdict:
    """Bucket estimated/budget into OK / WARN / OVER_BUDGET."""
    if budget_mb_s <= 0:
        return BudgetVerdict.OVER_BUDGET
    ratio = estimated_mb_s / budget_mb_s
    if ratio >= OVER_RATIO:
        return BudgetVerdict.OVER_BUDGET
    if ratio >= WARN_RATIO:
        return BudgetVerdict.WARN
    return BudgetVerdict.OK


@dataclass(slots=True, frozen=True)
class DiskBudgetAssessment:
    """Bundle of estimate + budget + verdict for downstream consumers."""

    estimated_mb_s: float
    budget_mb_s: float
    feed_count: int
    verdict: BudgetVerdict


def assess_disk_budget(
    enabled_feeds: "Iterable[FeedDefinition]",
    settings: "AppSettings",
) -> DiskBudgetAssessment:
    """Estimate aggregate write throughput and produce the verdict."""
    feeds_list = list(enabled_feeds)
    estimated = estimate_total_mb_s(feeds_list, settings)
    budget = float(settings.disk_budget_mb_s)
    verdict = validate_budget(estimated, budget)
    return DiskBudgetAssessment(
        estimated_mb_s=estimated,
        budget_mb_s=budget,
        feed_count=len(feeds_list),
        verdict=verdict,
    )


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Outcome of a Phase 10.C disk-space pre-flight at Start."""

    sufficient: bool
    free_bytes: int
    required_bytes: int
    grace_seconds: float
    estimated_mb_s: float


def evaluate_disk_preflight(
    recording_dir: Path,
    enabled_feeds: "Iterable[FeedDefinition]",
    settings: "AppSettings",
    *,
    disk_usage_fn: Callable[[Any], Any] = shutil.disk_usage,
) -> PreflightResult:
    """Phase 10.C pre-flight: refuse Start when free space < grace window.

    The probe target is the recording directory's parent volume — even
    if the directory itself doesn't exist yet, `disk_usage` walks up
    until it hits an existing path, which is what we want.
    """
    feeds_list = list(enabled_feeds)
    estimated_mb_s = estimate_total_mb_s(feeds_list, settings)
    grace_seconds = max(0.0, float(settings.disk_full_grace_seconds))
    required_bytes = int(estimated_mb_s * grace_seconds * 1024.0 * 1024.0)
    probe_target = _existing_ancestor(Path(recording_dir))
    try:
        usage = disk_usage_fn(probe_target)
        free_bytes = int(usage.free)
        sufficient = free_bytes >= required_bytes
    except (OSError, FileNotFoundError):
        # Probe failed (e.g. removable volume detached). Refuse Start
        # — the operator should pick a valid recording_dir.
        free_bytes = 0
        sufficient = False
    return PreflightResult(
        sufficient=sufficient,
        free_bytes=free_bytes,
        required_bytes=required_bytes,
        grace_seconds=grace_seconds,
        estimated_mb_s=estimated_mb_s,
    )


def _existing_ancestor(path: Path) -> Path:
    """Return `path` if it exists, else the nearest existing ancestor."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p
