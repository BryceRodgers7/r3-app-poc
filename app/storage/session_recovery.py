"""Crash-recovery + startup segment scan (slice 4.E).

Implements the §6.5 / §10.6 recovery contract:

- **Dirty session marking.** On app launch, walk every prior session's
  `session.json`. If the persisted state is `RECORDING` or `STOPPED`
  *and* `finalized_at` is missing, transition the manifest to `DIRTY`.
  This is the marker the §11.4 "Resume / End and finalize / Discard"
  prompt will react to once that UI lands; for now we just record it
  durably so the next startup behaves correctly.

- **Segment validation + quarantine.** For a given session, walk
  `recording/<feed_id>/segment_*.mkv` files and validate each via
  `cv2.VideoCapture` (fast, intra-frame). Files that fail to open or
  have no readable frames are moved to
  `<session>/quarantine/<feed_id>/segment_NNNNN.mkv`. The matching
  SQLite row (if any) is updated to `corrupt` and pointed at the new
  path. Files with no matching DB row but valid content (the typical
  outcome of a crash mid-segment, since `_finalize_pending_segment`
  only fires when the *next* segment's `format-location` runs) are
  inserted as new rows with state `dirty`.

- **Index rebuild.** `load_segment_index_for_session` reads the SQLite
  `segments` rows for one session and returns a populated
  `SegmentIndex`. The §11.4 recovery flow will call this when the
  operator opens a prior session for replay or post-session export.

The validator is injected so tests can avoid pulling in cv2/FFmpeg.
The default validator is `_default_segment_validator` and uses cv2
the same way `SegmentDecoder` does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_DIRTY,
    SEGMENT_STATE_QUARANTINED,
    Segment,
    SessionPaths,
)
from app.core.session_state import SESSION_MANIFEST_FILENAME
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex

LOGGER = logging.getLogger(__name__)

# A segment file's basic shape after the splitmuxsink's format-location
# callback names it. Used to filter the recording dir during validation.
_SEGMENT_FILENAME_RE = re.compile(r"^segment_(\d{5})\.mkv$")


@dataclass(slots=True, frozen=True)
class SegmentValidationResult:
    """Outcome of opening a single segment file for validation."""

    is_valid: bool
    duration_seconds: float = 0.0
    frame_count: int = 0


SegmentValidator = Callable[[Path], SegmentValidationResult]


@dataclass(slots=True)
class SessionRecoveryReport:
    """Summary of recovery actions taken across all prior sessions on launch."""

    dirty_sessions_marked: list[str] = field(default_factory=list)
    sessions_scanned: int = 0


@dataclass(slots=True)
class SegmentRecoveryReport:
    """Summary of recovery actions for a single session's segment files."""

    session_id: str
    files_scanned: int = 0
    files_quarantined: list[str] = field(default_factory=list)
    files_marked_dirty: list[str] = field(default_factory=list)
    db_rows_marked_corrupt: list[int] = field(default_factory=list)


def mark_dirty_sessions(sessions_root: Path) -> SessionRecoveryReport:
    """Walk `<sessions_root>/session_*` and mark unfinished sessions DIRTY.

    Reads each `session.json` directly (not through the state machine —
    we don't want to instantiate a live `SessionManifest` per session
    just to mutate a JSON file). Writes back the updated state and
    leaves `finalized_at` `null`. Idempotent: a session already in
    `DIRTY` is left alone.
    """
    report = SessionRecoveryReport()
    if not sessions_root.exists():
        return report
    for session_dir in sorted(sessions_root.iterdir()):
        if not session_dir.is_dir() or not session_dir.name.startswith("session_"):
            continue
        manifest_path = session_dir / SESSION_MANIFEST_FILENAME
        if not manifest_path.exists():
            continue
        report.sessions_scanned += 1
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("recovery: cannot read manifest %s", manifest_path)
            continue
        state = data.get("state")
        finalized_at = data.get("finalized_at")
        if state in {"recording", "stopped"} and not finalized_at:
            data["state"] = "dirty"
            try:
                _atomic_write_json(manifest_path, data)
                report.dirty_sessions_marked.append(str(data.get("session_id", session_dir.name)))
                LOGGER.info(
                    "recovery: marked session %s as DIRTY (was %s, no finalized_at)",
                    data.get("session_id"),
                    state,
                )
            except OSError:
                LOGGER.exception("recovery: failed to write %s", manifest_path)
    return report


def validate_session_segments(
    session_paths: SessionPaths,
    db: MetadataDb,
    *,
    validator: SegmentValidator | None = None,
) -> SegmentRecoveryReport:
    """Scan one session's `recording/<feed_id>/` files and reconcile against DB.

    Three cases per file:

    1. **Valid file + matching `complete` DB row** — no action.
    2. **Invalid file + matching DB row** — move to `quarantine/`,
       update DB row state → `quarantined`, point `file_path` at the
       new location.
    3. **Valid file + no DB row** — insert a `dirty` row with
       best-effort PTS metadata derived from cv2's reported duration.
       This is the typical outcome of a mid-segment crash.

    Files not matching the `segment_NNNNN.mkv` pattern are skipped
    (zero-byte files, leftovers from prior tooling, etc.).
    """
    if validator is None:
        validator = _default_segment_validator
    assert session_paths.quarantine_dir is not None
    report = SegmentRecoveryReport(session_id=session_paths.session_id)
    if not session_paths.recording_dir.exists():
        return report
    for feed_dir in sorted(session_paths.recording_dir.iterdir()):
        if not feed_dir.is_dir():
            continue
        feed_id = feed_dir.name
        feed_paths = session_paths.get_feed_paths(feed_id)
        for file_path in sorted(feed_dir.iterdir()):
            if not file_path.is_file():
                continue
            if not _SEGMENT_FILENAME_RE.match(file_path.name):
                continue
            report.files_scanned += 1
            existing = db.get_segment_by_path(str(file_path))
            try:
                result = validator(file_path)
            except Exception:
                LOGGER.exception("recovery: validator raised for %s", file_path)
                result = SegmentValidationResult(is_valid=False)
            if not result.is_valid:
                quarantined_path = _quarantine_file(file_path, feed_paths.quarantine_dir)
                report.files_quarantined.append(str(quarantined_path))
                if existing is not None and existing.segment_id is not None:
                    db.update_segment_file_path(
                        existing.segment_id, str(quarantined_path)
                    )
                    db.update_segment_state(
                        existing.segment_id, SEGMENT_STATE_QUARANTINED
                    )
                    report.db_rows_marked_corrupt.append(existing.segment_id)
                continue
            if existing is None:
                # Valid file, no DB row — insert as dirty.
                segment = _build_dirty_segment(
                    session_id=session_paths.session_id,
                    feed_id=feed_id,
                    file_path=file_path,
                    duration_seconds=result.duration_seconds,
                    frame_count=result.frame_count,
                )
                try:
                    db.insert_segment(segment)
                    report.files_marked_dirty.append(str(file_path))
                    LOGGER.info(
                        "recovery: inserted dirty row for unrouted segment %s",
                        file_path,
                    )
                except Exception:
                    LOGGER.exception(
                        "recovery: failed to insert dirty row for %s",
                        file_path,
                    )
                continue
            # Valid file with an existing DB row — nothing to do.
    return report


def load_segment_index_for_session(db: MetadataDb, session_id: str) -> SegmentIndex:
    """Build a fresh `SegmentIndex` populated from SQLite for one session.

    Used by §11.4's "open prior session for replay/export" flow once the
    UI lands. Only `complete` and `dirty` segments are loaded; quarantined
    and corrupt rows are excluded so they don't surface in replay queries.
    """
    index = SegmentIndex()
    for segment in db.segments_for_session(session_id):
        if segment.state in {SEGMENT_STATE_COMPLETE, SEGMENT_STATE_DIRTY}:
            index.add(segment)
    return index


def _quarantine_file(file_path: Path, quarantine_dir: Path) -> Path:
    """Move `file_path` into `quarantine_dir`, creating it if necessary.

    Returns the new path. If the destination already exists, appends a
    numeric suffix to avoid clobbering. The recovery pass should be
    idempotent enough that a re-run doesn't lose data.
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / file_path.name
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while True:
            candidate = quarantine_dir / f"{stem}.recovered_{counter:03d}{suffix}"
            if not candidate.exists():
                target = candidate
                break
            counter += 1
    file_path.replace(target)
    LOGGER.info("recovery: quarantined %s → %s", file_path, target)
    return target


def _build_dirty_segment(
    *,
    session_id: str,
    feed_id: str,
    file_path: Path,
    duration_seconds: float,
    frame_count: int,
) -> Segment:
    """Build a `Segment` row for a recovered in-progress file.

    PTS metadata is best-effort — we don't have the original
    `splitmuxsink` timestamps. The duration cv2 reports for an MKV is
    accurate enough to keep `available_pts_range` queries sensible; the
    `start_pts_ns` is left at 0 because we have no way to recover the
    original session-relative PTS without re-decoding the entire
    session's segment chain.
    """
    fragment_match = _SEGMENT_FILENAME_RE.match(file_path.name)
    fragment_index = int(fragment_match.group(1)) if fragment_match else 0
    duration_ns = max(0, int(duration_seconds * 1_000_000_000))
    try:
        size_bytes = file_path.stat().st_size
    except OSError:
        size_bytes = 0
    now = datetime.now(timezone.utc).isoformat()
    return Segment(
        session_id=session_id,
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=str(file_path),
        codec="mjpeg",
        container="mkv",
        start_pts_ns=0,
        end_pts_ns=duration_ns,
        duration_ns=duration_ns,
        frame_count_estimate=int(frame_count),
        size_bytes=size_bytes,
        state=SEGMENT_STATE_DIRTY,
        created_at=now,
        finalized_at=now,
    )


def _default_segment_validator(file_path: Path) -> SegmentValidationResult:
    """Open the segment via `cv2.VideoCapture` and probe duration/frame count.

    The check is intentionally lightweight: open, read frame_count and
    fps, attempt one frame read. A file that cannot be opened, has zero
    frames, or fails the first read is treated as corrupt.
    """
    try:
        import cv2
    except ImportError:
        # No cv2 available — be conservative and consider all files
        # valid so we don't quarantine real recordings on a stripped-down
        # install. The caller can pass an explicit validator if stricter
        # behavior is wanted.
        LOGGER.warning("recovery: cv2 not available; segment validation skipped")
        return SegmentValidationResult(is_valid=True)
    capture = cv2.VideoCapture(str(file_path))
    try:
        if not capture.isOpened():
            return SegmentValidationResult(is_valid=False)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if frame_count <= 0:
            return SegmentValidationResult(is_valid=False)
        ok, _frame = capture.read()
        if not ok:
            return SegmentValidationResult(is_valid=False)
        duration_seconds = (frame_count / fps) if fps > 0 else 0.0
        return SegmentValidationResult(
            is_valid=True,
            duration_seconds=duration_seconds,
            frame_count=frame_count,
        )
    finally:
        capture.release()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via `tmp + replace` (matches `SessionManifest`)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
