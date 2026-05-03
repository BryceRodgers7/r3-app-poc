"""Phase 10.E — mid-session segment validator + quarantine.

Tests use the synchronous `run_one(...)` entry point so each
quarantine path is deterministic without thread-scheduling races.
A separate set of tests exercises the async worker (FIFO,
per-feed serialisation, shutdown) using a barrier-controlled
validator stub.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from app.core.health_events import HealthEventLog, HealthSeverity
import app.core.health_events as health_events
from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_QUARANTINED,
    Segment,
)
from app.core.segment_validator_worker import SegmentValidatorWorker
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex
from app.storage.session_recovery import SegmentValidationResult


class _LogPatch:
    def __init__(self) -> None:
        self.log = HealthEventLog()
        self._saved = None

    def __enter__(self) -> HealthEventLog:
        self._saved = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = self.log
        return self.log

    def __exit__(self, *exc) -> None:
        health_events._DEFAULT_LOG = self._saved


def _segment(
    *,
    file_path: str,
    fragment_index: int = 0,
    feed_id: str = "cam_a",
    segment_id: int | None = None,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    return Segment(
        segment_id=segment_id,
        session_id="session_001",
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=file_path,
        codec="mjpeg",
        container="mkv",
        start_pts_ns=fragment_index * 4_000_000_000,
        end_pts_ns=(fragment_index + 1) * 4_000_000_000,
        duration_ns=4_000_000_000,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at="2026-04-28T01:00:04+00:00",
    )


def _make_file(directory: Path, name: str, contents: bytes = b"\x00" * 64) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(contents)
    return path


class SyncQuarantineTests(unittest.TestCase):
    """Drive `run_one` directly; covers the per-task pipeline end-to-end."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.recording_dir = self.root / "recording"
        self.quarantine_dir = self.root / "quarantine" / "cam_a"
        self.db = MetadataDb(self.root / "metadata.db")
        self.db.connect()
        self.index = SegmentIndex()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _add_segment(self, file_path: Path) -> Segment:
        seg = _segment(file_path=str(file_path))
        seg_id = self.db.insert_segment(seg)
        from dataclasses import replace
        seg = replace(seg, segment_id=seg_id)
        self.index.add(seg)
        return seg

    def test_valid_segment_is_no_op(self) -> None:
        with _LogPatch() as log:
            file_path = _make_file(self.recording_dir, "segment_00000.mkv")
            seg = self._add_segment(file_path)
            worker = SegmentValidatorWorker(
                "cam_a",
                validator=lambda _: SegmentValidationResult(is_valid=True),
            )
            worker.run_one(
                seg,
                quarantine_dir=self.quarantine_dir,
                metadata_db=self.db,
                segment_index=self.index,
            )
            # File unchanged; DB row unchanged; index still has it.
            self.assertTrue(file_path.exists())
            stored = self.db.get_segment_by_path(str(file_path))
            assert stored is not None
            self.assertEqual(stored.state, SEGMENT_STATE_COMPLETE)
            self.assertEqual(len(self.index.all_for_feed("cam_a")), 1)
            self.assertFalse(
                log.has_open_event(
                    category="segment_quarantined_runtime", feed_id="cam_a"
                )
            )

    def test_invalid_segment_moves_to_quarantine(self) -> None:
        with _LogPatch() as log:
            file_path = _make_file(self.recording_dir, "segment_00000.mkv")
            seg = self._add_segment(file_path)
            worker = SegmentValidatorWorker(
                "cam_a",
                validator=lambda _: SegmentValidationResult(is_valid=False),
            )
            worker.run_one(
                seg,
                quarantine_dir=self.quarantine_dir,
                metadata_db=self.db,
                segment_index=self.index,
            )
            self.assertFalse(file_path.exists())
            quarantined = self.quarantine_dir / "segment_00000.mkv"
            self.assertTrue(quarantined.exists())
            self.assertTrue(
                log.has_open_event(
                    category="segment_quarantined_runtime", feed_id="cam_a"
                )
            )

    def test_invalid_segment_updates_db_state_and_path(self) -> None:
        file_path = _make_file(self.recording_dir, "segment_00000.mkv")
        seg = self._add_segment(file_path)
        worker = SegmentValidatorWorker(
            "cam_a",
            validator=lambda _: SegmentValidationResult(is_valid=False),
        )
        worker.run_one(
            seg,
            quarantine_dir=self.quarantine_dir,
            metadata_db=self.db,
            segment_index=self.index,
        )
        # Old path is gone from the DB; new quarantine path is present.
        self.assertIsNone(self.db.get_segment_by_path(str(file_path)))
        new_path = self.quarantine_dir / "segment_00000.mkv"
        moved = self.db.get_segment_by_path(str(new_path))
        assert moved is not None
        self.assertEqual(moved.state, SEGMENT_STATE_QUARANTINED)

    def test_invalid_segment_evicted_from_index(self) -> None:
        file_path = _make_file(self.recording_dir, "segment_00000.mkv")
        seg = self._add_segment(file_path)
        worker = SegmentValidatorWorker(
            "cam_a",
            validator=lambda _: SegmentValidationResult(is_valid=False),
        )
        worker.run_one(
            seg,
            quarantine_dir=self.quarantine_dir,
            metadata_db=self.db,
            segment_index=self.index,
        )
        self.assertEqual(self.index.all_for_feed("cam_a"), [])

    def test_missing_file_marks_quarantined_without_move(self) -> None:
        with _LogPatch() as log:
            ghost_path = self.recording_dir / "ghost_segment.mkv"
            seg = _segment(file_path=str(ghost_path))
            seg_id = self.db.insert_segment(seg)
            from dataclasses import replace
            seg = replace(seg, segment_id=seg_id)
            self.index.add(seg)
            worker = SegmentValidatorWorker("cam_a")
            worker.run_one(
                seg,
                quarantine_dir=self.quarantine_dir,
                metadata_db=self.db,
                segment_index=self.index,
            )
            stored = self.db.get_segment_by_path(str(ghost_path))
            assert stored is not None
            self.assertEqual(stored.state, SEGMENT_STATE_QUARANTINED)
            self.assertEqual(self.index.all_for_feed("cam_a"), [])
            events = [
                e for e in log.open_events()
                if e.category == "segment_quarantined_runtime"
            ]
            self.assertEqual(events[0].metadata["reason"], "file_missing")

    def test_validator_exception_treated_as_invalid(self) -> None:
        file_path = _make_file(self.recording_dir, "segment_00000.mkv")
        seg = self._add_segment(file_path)

        def boom(_):
            raise RuntimeError("validator crash")

        worker = SegmentValidatorWorker("cam_a", validator=boom)
        worker.run_one(
            seg,
            quarantine_dir=self.quarantine_dir,
            metadata_db=self.db,
            segment_index=self.index,
        )
        self.assertFalse(file_path.exists())
        self.assertEqual(self.index.all_for_feed("cam_a"), [])

    def test_quarantine_event_severity_is_warning(self) -> None:
        with _LogPatch() as log:
            file_path = _make_file(self.recording_dir, "segment_00000.mkv")
            seg = self._add_segment(file_path)
            worker = SegmentValidatorWorker(
                "cam_a",
                validator=lambda _: SegmentValidationResult(is_valid=False),
            )
            worker.run_one(
                seg,
                quarantine_dir=self.quarantine_dir,
                metadata_db=self.db,
                segment_index=self.index,
            )
            events = [
                e for e in log.open_events()
                if e.category == "segment_quarantined_runtime"
            ]
            self.assertEqual(events[0].severity, HealthSeverity.WARNING.value)

    def test_no_db_no_index_no_crash(self) -> None:
        # Defensive: a stub PipelineManager with no db/index attached
        # should still run the validator + quarantine the file.
        file_path = _make_file(self.recording_dir, "segment_00000.mkv")
        seg = _segment(file_path=str(file_path))
        worker = SegmentValidatorWorker(
            "cam_a",
            validator=lambda _: SegmentValidationResult(is_valid=False),
        )
        worker.run_one(
            seg,
            quarantine_dir=self.quarantine_dir,
            metadata_db=None,
            segment_index=None,
        )
        self.assertFalse(file_path.exists())
        self.assertTrue((self.quarantine_dir / "segment_00000.mkv").exists())


class AsyncWorkerTests(unittest.TestCase):
    """Exercise the daemon-thread + queue path."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.recording_dir = self.root / "recording"
        self.quarantine_dir = self.root / "quarantine" / "cam_a"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_submit_runs_validator_off_thread(self) -> None:
        path = _make_file(self.recording_dir, "segment_00000.mkv")
        seg = _segment(file_path=str(path))
        called = threading.Event()

        def validate(_):
            called.set()
            return SegmentValidationResult(is_valid=True)

        worker = SegmentValidatorWorker("cam_a", validator=validate)
        worker.submit(
            seg,
            quarantine_dir=self.quarantine_dir,
            metadata_db=None,
            segment_index=None,
        )
        self.assertTrue(called.wait(timeout=2.0))
        worker.shutdown(timeout=2.0)

    def test_fifo_serialisation_per_feed(self) -> None:
        # Two submits in order; the second blocks on a barrier the
        # first one releases. If the worker runs them concurrently
        # (no per-feed serialisation), the second's barrier wait
        # would never time out — but the order of their completions
        # would be backwards. Validate strict in-order completion.
        order: list[int] = []
        gate = threading.Event()
        first_started = threading.Event()

        def validate_first(_):
            first_started.set()
            gate.wait(timeout=2.0)
            order.append(1)
            return SegmentValidationResult(is_valid=True)

        def validate_second(_):
            order.append(2)
            return SegmentValidationResult(is_valid=True)

        # Use a single dispatch validator so the worker sees a queue
        # of two items handled by *one* underlying function.
        responses = [validate_first, validate_second]

        def dispatch(p):
            return responses.pop(0)(p)

        path1 = _make_file(self.recording_dir, "segment_00000.mkv")
        path2 = _make_file(self.recording_dir, "segment_00001.mkv")
        seg1 = _segment(file_path=str(path1), fragment_index=0)
        seg2 = _segment(file_path=str(path2), fragment_index=1)

        worker = SegmentValidatorWorker("cam_a", validator=dispatch)
        worker.submit(
            seg1,
            quarantine_dir=self.quarantine_dir,
            metadata_db=None,
            segment_index=None,
        )
        worker.submit(
            seg2,
            quarantine_dir=self.quarantine_dir,
            metadata_db=None,
            segment_index=None,
        )
        # Wait for the first task to actually be running, then
        # release it. The second task can only have started after
        # the first returned.
        self.assertTrue(first_started.wait(timeout=2.0))
        # At this point seg2 cannot be running yet — the queue is
        # FIFO and the first task is blocked.
        self.assertEqual(order, [])
        gate.set()
        # Drain by shutting down (sends None sentinel after pending).
        worker.shutdown(timeout=2.0)
        self.assertEqual(order, [1, 2])

    def test_shutdown_idempotent(self) -> None:
        worker = SegmentValidatorWorker("cam_a")
        worker.shutdown(timeout=1.0)
        worker.shutdown(timeout=1.0)  # no error

    def test_submit_after_shutdown_is_no_op(self) -> None:
        worker = SegmentValidatorWorker("cam_a")
        worker.shutdown(timeout=1.0)
        # Should not raise or queue anything.
        path = _make_file(self.recording_dir, "segment_00000.mkv")
        seg = _segment(file_path=str(path))
        worker.submit(
            seg,
            quarantine_dir=self.quarantine_dir,
            metadata_db=None,
            segment_index=None,
        )
        # Validator was never invoked; file still on disk.
        self.assertTrue(path.exists())


class AlertBannerWiringTests(unittest.TestCase):
    def test_segment_quarantined_runtime_in_allowlist(self) -> None:
        from app.ui.alert_banner import _OPERATOR_VISIBLE_CATEGORIES
        self.assertIn(
            "segment_quarantined_runtime", _OPERATOR_VISIBLE_CATEGORIES
        )


if __name__ == "__main__":
    unittest.main()
