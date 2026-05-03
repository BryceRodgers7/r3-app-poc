"""Mid-session segment validator (Phase 10.E).

Re-runs the same validation `session_recovery.validate_session_segments`
performs on startup, but on each just-finalized segment so a corrupt
file caught after a hard disk hiccup gets quarantined before replay
hands it to cv2 / GStreamer.

One worker per feed, FIFO queue, daemon thread. Per-feed serialisation
keeps a slow disk hiccup from queuing up dozens of concurrent
validations and competing with the recording branch for I/O.

The worker is deliberately decoupled from `PipelineManager`: producers
construct one and call `submit(...)` per finalized segment. Consumers
(tests, future tooling) can drive the same logic synchronously via
`run_one(...)`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import queue
import threading
from typing import Any

from app.core.health_events import (
    HealthEventLog,
    HealthSeverity,
    default_log,
)
from app.core.models import SEGMENT_STATE_QUARANTINED, Segment
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex
from app.storage.session_recovery import (
    SegmentValidationResult,
    SegmentValidator,
    _default_segment_validator,
    _quarantine_file,
)

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class _ValidationTask:
    """One unit of work the worker pops off the queue."""

    segment: Segment
    quarantine_dir: Path
    metadata_db: MetadataDb | None
    segment_index: SegmentIndex | None


class SegmentValidatorWorker:
    """Per-feed FIFO validator + quarantine pipeline."""

    def __init__(
        self,
        feed_id: str,
        *,
        validator: SegmentValidator | None = None,
        health_log: HealthEventLog | None = None,
    ) -> None:
        self._feed_id = feed_id
        self._validator: SegmentValidator = (
            validator if validator is not None else _default_segment_validator
        )
        self._health_log = health_log if health_log is not None else default_log()
        # `queue.SimpleQueue` is thread-safe and (unlike `queue.Queue`)
        # never raises on a get/put under unusual interpreter shutdown
        # paths — the worker is a daemon thread, so quietness on exit
        # is preferable to noise.
        self._queue: queue.SimpleQueue[_ValidationTask | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._lock = threading.Lock()

    def submit(
        self,
        segment: Segment,
        *,
        quarantine_dir: Path,
        metadata_db: MetadataDb | None,
        segment_index: SegmentIndex | None,
    ) -> None:
        """Enqueue a finalized segment for background validation.

        Idempotent in the sense that the worker thread is started on
        first submit and reused for the lifetime of the worker."""
        with self._lock:
            if self._stopped:
                return
            self._ensure_thread_running_locked()
        task = _ValidationTask(
            segment=segment,
            quarantine_dir=quarantine_dir,
            metadata_db=metadata_db,
            segment_index=segment_index,
        )
        self._queue.put(task)

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """Drain the queue and stop the worker thread."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout=timeout)

    def queue_length(self) -> int:
        """Approximate length of the pending queue (test introspection)."""
        return self._queue.qsize()

    def run_one(
        self,
        segment: Segment,
        *,
        quarantine_dir: Path,
        metadata_db: MetadataDb | None,
        segment_index: SegmentIndex | None,
    ) -> SegmentValidationResult:
        """Synchronous variant of the worker's per-task logic.

        Used by tests that want deterministic behavior, and by anyone
        who'd rather block on validation than queue it. The async
        worker thread routes through this same code path.
        """
        return self._process_task(
            _ValidationTask(
                segment=segment,
                quarantine_dir=quarantine_dir,
                metadata_db=metadata_db,
                segment_index=segment_index,
            )
        )

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _ensure_thread_running_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"segment-validator[{self._feed_id}]",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        while True:
            try:
                task = self._queue.get()
            except Exception:
                LOGGER.exception(
                    "segment-validator[%s] queue.get raised", self._feed_id
                )
                return
            if task is None:
                return
            try:
                self._process_task(task)
            except Exception:
                LOGGER.exception(
                    "segment-validator[%s] task raised for %s",
                    self._feed_id,
                    task.segment.file_path,
                )

    def _process_task(self, task: _ValidationTask) -> SegmentValidationResult:
        segment = task.segment
        file_path = Path(segment.file_path)
        if not file_path.exists():
            # File vanished between finalization and validation — this
            # is the §11.4 quarantined-tail case but mid-session. The
            # DB row already exists; flag it quarantined so replay
            # ignores it. Don't try to move a missing file.
            LOGGER.warning(
                "segment-validator[%s] file missing for segment_id=%s path=%s; "
                "marking quarantined",
                self._feed_id,
                segment.segment_id,
                file_path,
            )
            self._mark_quarantined_in_storage(task, new_path=file_path)
            self._emit_quarantine_event(file_path, reason="file_missing")
            return SegmentValidationResult(is_valid=False)
        try:
            result = self._validator(file_path)
        except Exception:
            LOGGER.exception(
                "segment-validator[%s] validator raised for %s; treating as invalid",
                self._feed_id,
                file_path,
            )
            result = SegmentValidationResult(is_valid=False)
        if result.is_valid:
            return result
        try:
            new_path = _quarantine_file(file_path, task.quarantine_dir)
        except OSError:
            LOGGER.exception(
                "segment-validator[%s] quarantine move failed for %s",
                self._feed_id,
                file_path,
            )
            new_path = file_path
        self._mark_quarantined_in_storage(task, new_path=new_path)
        self._emit_quarantine_event(new_path, reason="invalid")
        return result

    def _mark_quarantined_in_storage(
        self, task: _ValidationTask, *, new_path: Path
    ) -> None:
        """Update the SQLite row and evict from the in-memory index."""
        segment = task.segment
        old_path = segment.file_path
        if (
            task.metadata_db is not None
            and segment.segment_id is not None
        ):
            try:
                if str(new_path) != old_path:
                    task.metadata_db.update_segment_file_path(
                        segment.segment_id, str(new_path)
                    )
                task.metadata_db.update_segment_state(
                    segment.segment_id, SEGMENT_STATE_QUARANTINED
                )
            except Exception:
                LOGGER.exception(
                    "segment-validator[%s] DB update failed for segment_id=%s",
                    self._feed_id,
                    segment.segment_id,
                )
        if task.segment_index is not None:
            task.segment_index.remove_by_path(old_path)

    def _emit_quarantine_event(self, file_path: Path, *, reason: str) -> None:
        # Each quarantine is a distinct operator-relevant event — let
        # the open-marker for `(feed_id, segment_quarantined_runtime)`
        # carry the most-recent occurrence. The banner shows the
        # latest message; the diagnostics widget tallies the lifetime
        # count via `category_count`.
        self._health_log.record(
            severity=HealthSeverity.WARNING,
            category="segment_quarantined_runtime",
            message=(
                f"feed={self._feed_id}: segment quarantined ({reason}) — "
                f"{file_path.name}"
            ),
            feed_id=self._feed_id,
            metadata={"path": str(file_path), "reason": reason},
        )
