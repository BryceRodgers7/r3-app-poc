"""Phase 8.B — long-form MP4 export via ffmpeg subprocess.

For each `(game_subdir, feed_id)` plan item, this module:

  1. Resolves the ordered list of completed segments from disk.
  2. Writes an ffmpeg concat-demuxer list file to a temp location.
  3. Runs `ffmpeg -f concat -safe 0 -i list.txt
        -c:v libx264 -preset medium -crf 23
        -c:a aac -b:a 128k -y <output>.mp4`
  4. Captures stderr for forensics on failure.
  5. Returns an `ExportResult` per artifact.

Source MKV files are read-only — the concat demuxer never modifies
its inputs. The output goes to `<session>/processed/<game>/<feed>.mp4`.

ffmpeg is the dependency: install it with
`pacman -S mingw-w64-ucrt-x86_64-ffmpeg` on MSYS2 UCRT64 (or via
choco/scoop on Windows). `--ffmpeg-path` on the CLI overrides
auto-detection.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.core.models import (
    EXPORT_KIND_LONG_FORM,
    EXPORT_STATUS_FAILED,
    EXPORT_STATUS_SUCCESS,
    ExportArtifact,
)
from app.storage.metadata_db import MetadataDb
from app.tools.post_session_processor import LongFormPlanItem

LOGGER = logging.getLogger(__name__)

# Phase 8.C: per-run "skipped" status that's tracked in the result
# stream but NOT persisted to the export_artifacts table — skipping
# means an earlier successful row already covers this artifact, so
# inserting another would just be noise.
_RESULT_STATUS_SKIPPED = "skipped"


class FfmpegNotFoundError(RuntimeError):
    """Raised when the ffmpeg binary cannot be located."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Outcome of a single long-form export attempt.

    Phase 8.C: success / failed results are persisted into the
    `export_artifacts` table; `skipped` results indicate a prior
    successful artifact exists and the operator did not pass `--force`.
    """

    plan_item: LongFormPlanItem
    status: str  # 'success' | 'failed' | 'skipped'
    output_path: Path
    error_message: str | None = None
    size_bytes: int | None = None
    started_at: str | None = None
    finalized_at: str | None = None


# Codec defaults. CRF 23 is the libx264 default and a good
# quality/size balance for archival deliverables. AAC at 128k matches
# typical broadcast use. `-preset medium` keeps encode time
# reasonable on a workstation; an operator who cares more about size
# than speed can adjust later via a CLI flag (deferred — out of
# scope for 8.B).
_DEFAULT_VIDEO_CODEC_ARGS = (
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "23",
    "-pix_fmt", "yuv420p",
)
_DEFAULT_AUDIO_CODEC_ARGS = (
    "-c:a", "aac",
    "-b:a", "128k",
)


class LongFormExporter:
    """Encodes `(game_subdir, feed_id)` plan items to MP4 via ffmpeg."""

    def __init__(self, ffmpeg_path: Path | None = None) -> None:
        self._ffmpeg_path = self._resolve_ffmpeg(ffmpeg_path)

    @staticmethod
    def _resolve_ffmpeg(override: Path | None) -> Path:
        """Locate the ffmpeg binary, raising `FfmpegNotFoundError` on miss.

        Preference order:
          1. Explicit `--ffmpeg-path` argument (validated to exist).
          2. `shutil.which("ffmpeg")` (PATH lookup).

        Common MSYS2 UCRT64 install locations (`/ucrt64/bin/ffmpeg.exe`)
        aren't on the default Git Bash PATH; the operator should pass
        `--ffmpeg-path` or add the directory to PATH.
        """
        if override is not None:
            override = override.resolve()
            if not override.exists():
                raise FfmpegNotFoundError(
                    f"ffmpeg path {override} does not exist"
                )
            return override
        located = shutil.which("ffmpeg")
        if located is None:
            raise FfmpegNotFoundError(
                "ffmpeg not found on PATH. Install with "
                "`pacman -S mingw-w64-ucrt-x86_64-ffmpeg` (MSYS2 UCRT64) "
                "or pass `--ffmpeg-path C:/path/to/ffmpeg.exe`."
            )
        return Path(located)

    @property
    def ffmpeg_path(self) -> Path:
        return self._ffmpeg_path

    def export(
        self,
        plan_item: LongFormPlanItem,
        segment_paths: list[Path],
    ) -> ExportResult:
        """Encode one plan item. Returns a result; does not raise on
        encode failure (so a single bad segment doesn't abort the
        whole batch).
        """
        started_at = datetime.now(timezone.utc).isoformat()
        if not segment_paths:
            return ExportResult(
                plan_item=plan_item,
                status="failed",
                output_path=plan_item.output_path,
                error_message="no segments resolved on disk",
                started_at=started_at,
                finalized_at=datetime.now(timezone.utc).isoformat(),
            )

        plan_item.output_path.parent.mkdir(parents=True, exist_ok=True)

        # Concat-demuxer list lives in a temp file: ffmpeg parses it
        # as `file '<path>'` lines. `-safe 0` allows absolute paths.
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as concat_file:
            concat_path = Path(concat_file.name)
            for seg in segment_paths:
                concat_file.write(_format_concat_line(seg))
        try:
            args = self._build_ffmpeg_args(concat_path, plan_item.output_path)
            LOGGER.info(
                "encoding %s/%s -> %s (%d segments)",
                plan_item.game_subdir,
                plan_item.feed_id,
                plan_item.output_path,
                len(segment_paths),
            )
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            try:
                concat_path.unlink()
            except OSError:
                LOGGER.debug("could not remove concat list %s", concat_path)

        finalized_at = datetime.now(timezone.utc).isoformat()
        if result.returncode != 0:
            stderr_tail = (result.stderr or "")[-2000:]
            LOGGER.error(
                "ffmpeg failed for %s/%s: returncode=%d",
                plan_item.game_subdir,
                plan_item.feed_id,
                result.returncode,
            )
            return ExportResult(
                plan_item=plan_item,
                status="failed",
                output_path=plan_item.output_path,
                error_message=(
                    f"ffmpeg exited with returncode={result.returncode}; "
                    f"last stderr: {stderr_tail}"
                ),
                started_at=started_at,
                finalized_at=finalized_at,
            )

        try:
            size = plan_item.output_path.stat().st_size
        except OSError:
            size = None
        LOGGER.info(
            "encoded %s/%s OK (%s bytes)",
            plan_item.game_subdir,
            plan_item.feed_id,
            size if size is not None else "?",
        )
        return ExportResult(
            plan_item=plan_item,
            status="success",
            output_path=plan_item.output_path,
            size_bytes=size,
            started_at=started_at,
            finalized_at=finalized_at,
        )

    def _build_ffmpeg_args(
        self, concat_path: Path, output_path: Path
    ) -> list[str]:
        """Return the full ffmpeg argv for encoding one artifact.

        Factored out so tests can assert the args structure without
        actually running ffmpeg.

        `-avoid_negative_ts make_zero` is load-bearing for games beyond
        the first within a single app run. Each `recording/<game_NNN>/`
        subtree's segment files inherit PTS from the GStreamer
        pipeline's running clock, which keeps ticking across Stop/Start
        cycles (the clock is per-pipeline, not per-game). Without this
        flag, the encoded MP4 for game 2+ starts at whatever PTS the
        first segment of that game has — e.g. `0:42` if game 1 ran for
        42 seconds — and players show the timeline starting at that
        offset rather than 0. `make_zero` instructs the muxer to shift
        the smallest input timestamp to zero, which is exactly the
        per-file normalization we want here.
        """
        return [
            str(self._ffmpeg_path),
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_path),
            *_DEFAULT_VIDEO_CODEC_ARGS,
            *_DEFAULT_AUDIO_CODEC_ARGS,
            "-avoid_negative_ts", "make_zero",
            "-y",  # overwrite existing output (8.C will skip via DB instead)
            str(output_path),
        ]


def export_all(
    exporter: LongFormExporter,
    plan_items: Iterable[LongFormPlanItem],
    segment_paths_for: "callable",
    *,
    db: MetadataDb | None = None,
    session_id: str | None = None,
    force: bool = False,
) -> list[ExportResult]:
    """Run `exporter.export` for every plan item, collecting results.

    `segment_paths_for(plan_item)` is supplied by the caller (the CLI
    main) so this function stays decoupled from the SegmentIndex.

    Phase 8.C: when `db` and `session_id` are provided, this function
    becomes the persistence + idempotency point:

      - Before encoding, look up the set of `(kind, game_subdir, feed_id)`
        triples that already have a successful export row. Items in
        that set are returned as `status='skipped'` without invoking
        ffmpeg.
      - After each encode attempt (success or failure), insert an
        `ExportArtifact` row. Failed attempts are NOT replaced or
        deleted on retry — every attempt persists, providing forensic
        history.
      - `force=True` ignores the success set and re-encodes everything.

    `db` and `session_id` are optional so existing callers / tests
    that only need the encode-and-collect-results flow keep working
    without DB plumbing.
    """
    plan_items_list = list(plan_items)
    persist = db is not None and session_id is not None
    if persist and not force:
        already_done = db.successful_artifact_keys(session_id)
    else:
        already_done = set()

    results: list[ExportResult] = []
    for plan_item in plan_items_list:
        artifact_key = (
            EXPORT_KIND_LONG_FORM,
            plan_item.game_subdir,
            plan_item.feed_id,
        )
        if artifact_key in already_done:
            LOGGER.info(
                "skipping %s/%s — prior success row exists "
                "(pass --force to re-encode)",
                plan_item.game_subdir,
                plan_item.feed_id,
            )
            results.append(
                ExportResult(
                    plan_item=plan_item,
                    status=_RESULT_STATUS_SKIPPED,
                    output_path=plan_item.output_path,
                )
            )
            continue
        segment_paths = segment_paths_for(plan_item)
        result = exporter.export(plan_item, segment_paths)
        results.append(result)
        if persist:
            _persist_result_locked(
                db, session_id, plan_item, result
            )
    return results


def _persist_result_locked(
    db: MetadataDb,
    session_id: str,
    plan_item: LongFormPlanItem,
    result: ExportResult,
) -> None:
    """Insert an `ExportArtifact` row mirroring `result`.

    'skipped' results never reach this function; only success/failure
    attempts are persisted. The artifact's `duration_ns` comes from
    the plan (sum of segment durations) — the actual MP4 duration may
    differ slightly due to encoder timing, but the plan value is the
    authoritative "intended" duration and is what the operator sees
    in the readout.
    """
    status = (
        EXPORT_STATUS_SUCCESS
        if result.status == "success"
        else EXPORT_STATUS_FAILED
    )
    artifact = ExportArtifact(
        session_id=session_id,
        kind=EXPORT_KIND_LONG_FORM,
        game_subdir=plan_item.game_subdir,
        feed_id=plan_item.feed_id,
        output_path=str(result.output_path),
        status=status,
        error_message=result.error_message,
        size_bytes=result.size_bytes,
        duration_ns=plan_item.total_duration_ns,
        started_at=result.started_at or "",
        finalized_at=result.finalized_at,
    )
    try:
        db.insert_export_artifact(artifact)
    except Exception:
        LOGGER.exception(
            "failed to persist export_artifact row for %s/%s",
            plan_item.game_subdir,
            plan_item.feed_id,
        )


def _format_concat_line(path: Path) -> str:
    """Render one line of the ffmpeg concat-demuxer list.

    The concat demuxer's syntax wraps the path in single quotes and
    requires single-quote escaping as `'\\''`. Not relevant for
    Windows session paths today (no apostrophes in `session_NNN` /
    `game_NNN` / feed_id), but defensive for hand-edited future paths.
    """
    safe = str(path).replace("'", "'\\''")
    return f"file '{safe}'\n"
