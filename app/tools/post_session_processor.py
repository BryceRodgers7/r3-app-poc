"""Post-session MP4 processor (Phase 8).

Phase 8.A — CLI scaffold + session validation:

  - Accept a session folder as input.
  - Refuse to run on active / dirty / non-finalized sessions.
  - File-lock the session for the duration of the run so two
    concurrent processes can't trample each other.
  - Read the segments table and produce a plan grouped by
    `(game_subdir, feed_id)`.
  - Print the plan; do not encode (8.B + 8.C).

Usage::

    python -m app.tools.post_session_processor <session_path>

Exit codes:
  0 — success (plan printed)
  2 — validation failure (wrong state, lock held, missing manifest, etc.)
  1 — unexpected error (programmer bug, IO error)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.core.models import SEGMENT_STATE_COMPLETE, Segment
from app.core.session_state import SESSION_MANIFEST_FILENAME, SessionState
from app.storage.metadata_db import MetadataDb

LOGGER = logging.getLogger(__name__)

LOCK_FILENAME = ".processing.lock"
PROCESSED_DIRNAME = "processed"


class ValidationError(RuntimeError):
    """Raised when the session can't be processed.

    The CLI catches this and exits with code 2. Distinct from
    `RuntimeError` so callers (tests, future GUI integrations) can
    react to validation failures specifically.
    """


@dataclass(frozen=True, slots=True)
class LongFormPlanItem:
    """One long-form MP4 artifact the processor would produce."""

    game_subdir: str
    feed_id: str
    segment_count: int
    total_duration_ns: int
    output_path: Path
    # Phase 8.B: ordered absolute paths of the segments that will be
    # concatenated into `output_path`. Populated by `build_plan` from
    # the SegmentIndex; the encoder uses this rather than re-querying
    # the DB during export. Tuple for hashability + dataclass frozen.
    segment_paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessingPlan:
    """The full plan for a session."""

    session_id: str
    long_form: list[LongFormPlanItem]


# --------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code.

    Exit codes:
      0 — success (all artifacts encoded, or `--dry-run`).
      1 — partial success (one or more artifacts failed to encode).
      2 — pre-flight failure (validation, missing DB, missing ffmpeg, lock held).
    """
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    session_path = args.session_path.resolve()
    LOGGER.info("processing session %s", session_path)

    try:
        validate_session_state(session_path)
    except ValidationError as exc:
        LOGGER.error("validation failed: %s", exc)
        return 2

    db_path = (args.metadata_db or _default_metadata_db_path(session_path)).resolve()
    if not db_path.exists():
        LOGGER.error("metadata database not found at %s", db_path)
        return 2

    # Resolve ffmpeg up front (unless `--dry-run`) so a missing binary
    # fails before we acquire the lock or open the DB. Phase 8.C will
    # bring its own `dry_run` plumbing into the artifact persistence
    # layer; for 8.B it just short-circuits encoding.
    exporter: "LongFormExporter | None" = None
    if not args.dry_run:
        from app.tools.long_form_export import (
            FfmpegNotFoundError,
            LongFormExporter,
        )
        try:
            exporter = LongFormExporter(ffmpeg_path=args.ffmpeg_path)
        except FfmpegNotFoundError as exc:
            LOGGER.error("%s", exc)
            return 2

    try:
        with acquire_lock(session_path):
            # Phase 8.C: keep the DB open through encoding so the
            # exporter can read the `successful_artifact_keys` set and
            # write `export_artifacts` rows. The plan-building scope
            # is unchanged — `build_plan` reads segments and is done.
            db = MetadataDb(db_path)
            try:
                plan = build_plan(db, session_path)
                print_plan(plan)
                if args.dry_run or not plan.long_form:
                    return 0
                assert exporter is not None
                from app.tools.long_form_export import export_all
                results = export_all(
                    exporter,
                    plan.long_form,
                    segment_paths_for=lambda item: list(item.segment_paths),
                    db=db,
                    session_id=plan.session_id,
                    force=args.force,
                )
                # Phase 8.D: write `<game>/plays.json` sidecars,
                # one per game in the plan. Independent of MP4
                # encoding success — a play-marker file is useful
                # forensically even if some encodes failed.
                from app.tools.plays_json_export import (
                    write_plays_sidecars_for_session,
                )
                game_subdirs = [item.game_subdir for item in plan.long_form]
                sidecars = write_plays_sidecars_for_session(
                    db, session_path, game_subdirs
                )
            finally:
                db.close()
    except ValidationError as exc:
        LOGGER.error("%s", exc)
        return 2

    succeeded = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    print(
        f"\nExport complete: {len(succeeded)} succeeded, "
        f"{len(failed)} failed, {len(skipped)} skipped."
    )
    if sidecars:
        print(f"plays.json sidecars: {len(sidecars)} written.")
    for r in skipped:
        print(
            f"  SKIPPED {r.plan_item.game_subdir}/{r.plan_item.feed_id}: "
            f"prior success row exists (use --force to re-encode)"
        )
    for r in failed:
        print(
            f"  FAILED  {r.plan_item.game_subdir}/{r.plan_item.feed_id}: "
            f"{(r.error_message or 'unknown error').splitlines()[0]}"
        )
    return 0 if not failed else 1


def validate_session_state(session_path: Path) -> dict:
    """Read `session.json` and refuse anything other than `finalized`.

    Raises `ValidationError` with an explicit message naming the wrong
    state. Returns the parsed manifest dict on success — callers that
    want session metadata (created_at, finalized_at) can use the
    returned dict instead of re-reading the file.
    """
    manifest_path = session_path / SESSION_MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ValidationError(
            f"no {SESSION_MANIFEST_FILENAME} at {manifest_path} — "
            f"is {session_path} actually a session folder?"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{manifest_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ValidationError(
            f"{manifest_path} is not a JSON object"
        )
    state = manifest.get("state")
    if state != SessionState.FINALIZED.value:
        # Use the SessionState enum so the error message matches the
        # actual state names the recording app writes. Unknown states
        # surface as-is rather than being silently rewritten.
        valid_states = {s.value for s in SessionState}
        if state in valid_states:
            raise ValidationError(
                f"session state is {state!r}; processor only runs on "
                f"{SessionState.FINALIZED.value!r} sessions. Use the "
                f"recording app's recovery dialog to finalize a "
                f"dirty/stopped session before processing."
            )
        raise ValidationError(
            f"session state is {state!r}; expected one of "
            f"{sorted(valid_states)}"
        )
    return manifest


@contextmanager
def acquire_lock(session_path: Path) -> Iterator[Path]:
    """Atomically claim `<session>/.processing.lock` for the duration.

    Uses `O_CREAT | O_EXCL` so two concurrent processors fail-fast
    rather than corrupting each other's outputs. Lock file contains
    the holding process's PID for forensic value if a crashed run
    leaves the lock behind.

    On crash, the lock file persists. Operator must delete it
    manually. (A more sophisticated stale-lock detection — checking
    whether the PID is alive — is deferred to Phase 8.B+ if the
    crash-and-resume path needs it.)
    """
    lock_path = session_path / LOCK_FILENAME
    try:
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError as exc:
        raise ValidationError(
            f"another processing run holds {lock_path}. If the prior run "
            f"crashed, delete this file manually and retry."
        ) from exc
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except OSError:
            LOGGER.warning("could not remove lock file %s", lock_path)


def build_plan(db: MetadataDb, session_path: Path) -> ProcessingPlan:
    """Group completed segments by (game_subdir, feed_id) into export plan items.

    Segments under the legacy flat layout (`recording/<feed_id>/...`,
    no `game_NNN/` parent) are skipped — Phase 8 only exports
    per-game artifacts and the flat layout has no game grouping.
    A `WARNING` is logged so the operator notices.

    Writing-state segments are excluded per §6.6 / §15.7 — a
    not-yet-finalized segment isn't safely readable.
    """
    session_id = session_path.name
    segments = db.segments_for_session(session_id)
    grouped: dict[tuple[str, str], list[Segment]] = defaultdict(list)
    skipped_legacy = 0
    skipped_writing = 0
    for segment in segments:
        if segment.state != SEGMENT_STATE_COMPLETE:
            skipped_writing += 1
            continue
        game_subdir = _game_subdir_for_segment(segment)
        if game_subdir is None:
            skipped_legacy += 1
            continue
        grouped[(game_subdir, segment.feed_id)].append(segment)

    if skipped_legacy:
        LOGGER.warning(
            "skipped %d legacy flat-layout segments (no game_NNN/ parent)",
            skipped_legacy,
        )
    if skipped_writing:
        LOGGER.info(
            "skipped %d non-complete segments (state != %r)",
            skipped_writing,
            SEGMENT_STATE_COMPLETE,
        )

    items: list[LongFormPlanItem] = []
    processed_dir = session_path / PROCESSED_DIRNAME
    for (game_subdir, feed_id), segs in sorted(grouped.items()):
        # Sort segments by fragment_index so the concat order matches
        # the recording timeline rather than DB insert order.
        segs.sort(key=lambda s: s.fragment_index)
        total_duration_ns = sum(s.duration_ns for s in segs)
        output_path = processed_dir / game_subdir / f"{feed_id}.mp4"
        items.append(
            LongFormPlanItem(
                game_subdir=game_subdir,
                feed_id=feed_id,
                segment_count=len(segs),
                total_duration_ns=total_duration_ns,
                output_path=output_path,
                segment_paths=tuple(Path(s.file_path) for s in segs),
            )
        )
    return ProcessingPlan(session_id=session_id, long_form=items)


def print_plan(plan: ProcessingPlan) -> None:
    """Render a plan to stdout in operator-friendly form."""
    print(f"Session: {plan.session_id}")
    if not plan.long_form:
        print("  (no exportable per-game segments found)")
        return
    print(f"Long-form artifacts to export: {len(plan.long_form)}")
    for item in plan.long_form:
        duration_s = item.total_duration_ns / 1_000_000_000
        print(
            f"  {item.game_subdir}/{item.feed_id}: "
            f"{item.segment_count} segments, "
            f"{duration_s:6.1f}s  ->  {item.output_path}"
        )


# --------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="post_session_processor",
        description=(
            "Generate MP4 deliverables from a finalized session's recording "
            "segments. Default behavior: validate, print the plan, encode "
            "each artifact via ffmpeg. Use `--dry-run` to skip encoding."
        ),
    )
    parser.add_argument(
        "session_path",
        type=Path,
        help="Path to the session folder, e.g. C:/SportsReplay/sessions/session_001",
    )
    parser.add_argument(
        "--metadata-db",
        type=Path,
        default=None,
        help=(
            "Override the metadata DB path (default: <session_path>/../../metadata.db)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the export plan and exit without encoding.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-encode artifacts even if a previous run already produced a "
            "successful export. Default behavior is idempotent — artifacts "
            "with a success row in `export_artifacts` are skipped."
        ),
    )
    parser.add_argument(
        "--ffmpeg-path",
        type=Path,
        default=None,
        help=(
            "Override the ffmpeg binary location. Default: PATH lookup via "
            "shutil.which. Useful on MSYS2 UCRT64 where ffmpeg may live at "
            "/ucrt64/bin/ffmpeg.exe but isn't on the Git Bash PATH."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging (DEBUG level).",
    )
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _default_metadata_db_path(session_path: Path) -> Path:
    """Return `<base>/metadata.db` given `<base>/sessions/<session_id>`.

    Mirrors `AppSettings.metadata_db_path` derivation: the recording
    app stores sessions under `base_data_dir/sessions/` and the DB at
    `base_data_dir/metadata.db`. Two parents up from the session
    folder lands at `base_data_dir`.
    """
    return session_path.parent.parent / "metadata.db"


def _game_subdir_for_segment(segment: Segment) -> str | None:
    """Return the `game_NNN` path component of `segment.file_path`, or None.

    Per-game layout is `<recording_dir>/<game_subdir>/<feed_id>/segment_*.mkv`.
    Legacy flat layout (`<recording_dir>/<feed_id>/segment_*.mkv`) has
    no `game_*` component — return None so the caller can skip.
    Path-component match (not substring) avoids the `game_001` ⊂
    `game_0011` trap.
    """
    for part in Path(segment.file_path).parts:
        if part.startswith("game_") and part[len("game_"):].isdigit():
            return part
    return None


if __name__ == "__main__":
    sys.exit(main())
