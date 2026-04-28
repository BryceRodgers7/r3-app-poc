"""Tests for slice 4.E crash-recovery and startup segment scan."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_DIRTY,
    SEGMENT_STATE_QUARANTINED,
    Segment,
    SessionPaths,
)
from app.core.session_state import SESSION_MANIFEST_FILENAME
from app.storage.metadata_db import MetadataDb
from app.storage.session_recovery import (
    DirtySessionInfo,
    RecoveryAction,
    SegmentValidationResult,
    find_dirty_sessions,
    find_next_fragment_index,
    load_segment_index_for_session,
    mark_dirty_sessions,
    resolve_dirty_session,
    run_startup_scan,
    validate_session_segments,
)


def _write_manifest(
    session_dir: Path,
    *,
    session_id: str,
    state: str,
    finalized_at: str | None,
    created_at: str = "2026-04-28T00:00:00+00:00",
) -> Path:
    """Write a `session.json` matching the format `SessionManifest` produces."""
    payload = {
        "session_id": session_id,
        "state": state,
        "created_at": created_at,
        "finalized_at": finalized_at,
    }
    manifest_path = session_dir / SESSION_MANIFEST_FILENAME
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path


def _read_manifest(session_dir: Path) -> dict:
    return json.loads((session_dir / SESSION_MANIFEST_FILENAME).read_text(encoding="utf-8"))


def _build_session_paths(root: Path, session_id: str) -> SessionPaths:
    session_root = root / session_id
    recording = session_root / "recording"
    rolling = session_root / "rolling"
    clips = session_root / "clips"
    quarantine = session_root / "quarantine"
    for d in (session_root, recording, rolling, clips):
        d.mkdir(parents=True, exist_ok=True)
    return SessionPaths(
        session_id=session_id,
        root_dir=session_root,
        recording_dir=recording,
        rolling_dir=rolling,
        clips_dir=clips,
        quarantine_dir=quarantine,
    )


def _segment(
    *,
    session_id: str = "session_001",
    feed_id: str = "ndi_main",
    fragment_index: int = 0,
    file_path: str,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    return Segment(
        session_id=session_id,
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


class MarkDirtySessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.sessions_root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_recording_without_finalized_at_is_marked_dirty(self) -> None:
        session_dir = self.sessions_root / "session_001"
        _write_manifest(
            session_dir,
            session_id="session_001",
            state="recording",
            finalized_at=None,
        )
        report = mark_dirty_sessions(self.sessions_root)
        self.assertEqual(report.dirty_sessions_marked, ["session_001"])
        manifest = _read_manifest(session_dir)
        self.assertEqual(manifest["state"], "dirty")
        # finalized_at stays None — that's the §11.4 prompt's marker.
        self.assertIsNone(manifest["finalized_at"])

    def test_stopped_without_finalized_at_is_marked_dirty(self) -> None:
        session_dir = self.sessions_root / "session_002"
        _write_manifest(
            session_dir,
            session_id="session_002",
            state="stopped",
            finalized_at=None,
        )
        report = mark_dirty_sessions(self.sessions_root)
        self.assertEqual(report.dirty_sessions_marked, ["session_002"])

    def test_finalized_session_is_left_alone(self) -> None:
        session_dir = self.sessions_root / "session_003"
        _write_manifest(
            session_dir,
            session_id="session_003",
            state="finalized",
            finalized_at="2026-04-28T02:00:00+00:00",
        )
        report = mark_dirty_sessions(self.sessions_root)
        self.assertEqual(report.dirty_sessions_marked, [])
        manifest = _read_manifest(session_dir)
        self.assertEqual(manifest["state"], "finalized")

    def test_already_dirty_session_is_idempotent(self) -> None:
        session_dir = self.sessions_root / "session_004"
        _write_manifest(
            session_dir,
            session_id="session_004",
            state="dirty",
            finalized_at=None,
        )
        report = mark_dirty_sessions(self.sessions_root)
        self.assertEqual(report.dirty_sessions_marked, [])
        manifest = _read_manifest(session_dir)
        self.assertEqual(manifest["state"], "dirty")

    def test_recording_with_finalized_at_is_left_alone(self) -> None:
        # Defensive: if some other code path stamped finalized_at while
        # the state was still 'recording', don't second-guess it.
        session_dir = self.sessions_root / "session_005"
        _write_manifest(
            session_dir,
            session_id="session_005",
            state="recording",
            finalized_at="2026-04-28T02:00:00+00:00",
        )
        report = mark_dirty_sessions(self.sessions_root)
        self.assertEqual(report.dirty_sessions_marked, [])

    def test_unrelated_directories_are_skipped(self) -> None:
        (self.sessions_root / "not_a_session").mkdir()
        (self.sessions_root / "session_999").mkdir()
        # session_999 has no manifest — should just be skipped.
        report = mark_dirty_sessions(self.sessions_root)
        self.assertEqual(report.dirty_sessions_marked, [])

    def test_missing_root_returns_empty_report(self) -> None:
        report = mark_dirty_sessions(self.sessions_root / "does_not_exist")
        self.assertEqual(report.dirty_sessions_marked, [])
        self.assertEqual(report.sessions_scanned, 0)


class ValidateSessionSegmentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.session_paths = _build_session_paths(self.tmp / "sessions", "session_001")
        self.feed_dir = self.session_paths.recording_dir / "ndi_main"
        self.feed_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp / "metadata.db"
        self.db = MetadataDb(self.db_path)
        self.db.create_session(
            session_id="session_001",
            source_name="Test",
            started_at="2026-04-28T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def _make_segment_file(self, name: str = "segment_00000.mkv") -> Path:
        path = self.feed_dir / name
        path.write_bytes(b"\0")
        return path

    def test_corrupt_file_with_db_row_is_quarantined_and_row_updated(self) -> None:
        file_path = self._make_segment_file("segment_00000.mkv")
        seg_id = self.db.insert_segment(
            _segment(file_path=str(file_path), fragment_index=0)
        )

        def always_invalid(_path: Path) -> SegmentValidationResult:
            return SegmentValidationResult(is_valid=False)

        report = validate_session_segments(
            self.session_paths, self.db, validator=always_invalid
        )
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(len(report.files_quarantined), 1)
        self.assertEqual(report.db_rows_marked_corrupt, [seg_id])
        # Original file is gone; quarantine file exists.
        self.assertFalse(file_path.exists())
        quarantined = self.session_paths.quarantine_dir / "ndi_main" / "segment_00000.mkv"
        self.assertTrue(quarantined.exists())
        # DB row reflects the new path + state.
        updated = self.db.get_segment_by_path(str(quarantined))
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.state, SEGMENT_STATE_QUARANTINED)

    def test_valid_file_without_db_row_is_inserted_as_dirty(self) -> None:
        file_path = self._make_segment_file("segment_00000.mkv")

        def always_valid(_path: Path) -> SegmentValidationResult:
            return SegmentValidationResult(
                is_valid=True, duration_seconds=4.0, frame_count=120
            )

        report = validate_session_segments(
            self.session_paths, self.db, validator=always_valid
        )
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(len(report.files_marked_dirty), 1)
        # DB now has a row for it.
        recovered = self.db.get_segment_by_path(str(file_path))
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, SEGMENT_STATE_DIRTY)
        self.assertEqual(recovered.fragment_index, 0)
        self.assertEqual(recovered.duration_ns, 4_000_000_000)

    def test_valid_file_with_existing_complete_row_is_left_alone(self) -> None:
        file_path = self._make_segment_file("segment_00000.mkv")
        self.db.insert_segment(_segment(file_path=str(file_path), fragment_index=0))

        def always_valid(_path: Path) -> SegmentValidationResult:
            return SegmentValidationResult(
                is_valid=True, duration_seconds=4.0, frame_count=120
            )

        report = validate_session_segments(
            self.session_paths, self.db, validator=always_valid
        )
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(report.files_quarantined, [])
        self.assertEqual(report.files_marked_dirty, [])
        self.assertTrue(file_path.exists())

    def test_files_not_matching_segment_pattern_are_skipped(self) -> None:
        (self.feed_dir / "stray.txt").write_bytes(b"x")
        (self.feed_dir / "segment_99999.mkv.tmp").write_bytes(b"x")

        def fail_if_called(_path: Path) -> SegmentValidationResult:
            raise AssertionError("validator should not run on non-segment files")

        report = validate_session_segments(
            self.session_paths, self.db, validator=fail_if_called
        )
        self.assertEqual(report.files_scanned, 0)

    def test_quarantine_collision_uses_recovered_suffix(self) -> None:
        # Pre-stage a file already in quarantine with the same name to
        # force the collision path.
        feed_quarantine = self.session_paths.quarantine_dir / "ndi_main"
        feed_quarantine.mkdir(parents=True, exist_ok=True)
        (feed_quarantine / "segment_00000.mkv").write_bytes(b"existing")

        file_path = self._make_segment_file("segment_00000.mkv")

        def always_invalid(_path: Path) -> SegmentValidationResult:
            return SegmentValidationResult(is_valid=False)

        report = validate_session_segments(
            self.session_paths, self.db, validator=always_invalid
        )
        self.assertEqual(len(report.files_quarantined), 1)
        # New file landed at recovered_001 to avoid clobbering.
        recovered = feed_quarantine / "segment_00000.recovered_001.mkv"
        self.assertTrue(recovered.exists())
        # Original quarantine file is preserved.
        self.assertEqual(
            (feed_quarantine / "segment_00000.mkv").read_bytes(), b"existing"
        )

    def test_missing_recording_dir_is_no_op(self) -> None:
        # Build a session where recording/ doesn't exist (e.g. crash before any segments).
        empty_session = _build_session_paths(self.tmp / "sessions", "session_002")
        # Remove the recording dir to simulate the empty case.
        for child in empty_session.recording_dir.iterdir():
            child.rmdir()
        empty_session.recording_dir.rmdir()
        self.assertFalse(empty_session.recording_dir.exists())
        report = validate_session_segments(empty_session, self.db)
        self.assertEqual(report.files_scanned, 0)


class LoadSegmentIndexForSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.db = MetadataDb(Path(self._temp_dir.name) / "metadata.db")
        self.db.create_session(
            session_id="session_001",
            source_name="Test",
            started_at="2026-04-28T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_loads_complete_and_dirty_skips_quarantined(self) -> None:
        self.db.insert_segment(
            _segment(file_path="/x/0.mkv", fragment_index=0, state=SEGMENT_STATE_COMPLETE)
        )
        self.db.insert_segment(
            _segment(file_path="/x/1.mkv", fragment_index=1, state=SEGMENT_STATE_DIRTY)
        )
        self.db.insert_segment(
            _segment(
                file_path="/x/2.mkv",
                fragment_index=2,
                state=SEGMENT_STATE_QUARANTINED,
            )
        )
        index = load_segment_index_for_session(self.db, "session_001")
        loaded = index.all_for_feed("ndi_main")
        # Two of three rows survived the filter.
        self.assertEqual(len(loaded), 2)
        states = {s.state for s in loaded}
        self.assertEqual(states, {SEGMENT_STATE_COMPLETE, SEGMENT_STATE_DIRTY})

    def test_empty_session_returns_empty_index(self) -> None:
        index = load_segment_index_for_session(self.db, "session_001")
        self.assertEqual(index.feed_ids(), [])


class FindDirtySessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.sessions_root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_returns_only_dirty_sessions(self) -> None:
        _write_manifest(
            self.sessions_root / "session_001",
            session_id="session_001",
            state="dirty",
            finalized_at=None,
        )
        _write_manifest(
            self.sessions_root / "session_002",
            session_id="session_002",
            state="finalized",
            finalized_at="2026-04-28T02:00:00+00:00",
        )
        _write_manifest(
            self.sessions_root / "session_003",
            session_id="session_003",
            state="dirty",
            finalized_at=None,
        )
        result = find_dirty_sessions(self.sessions_root)
        self.assertEqual([info.session_id for info in result], ["session_001", "session_003"])
        self.assertEqual(result[0].session_dir, self.sessions_root / "session_001")
        self.assertEqual(result[0].state, "dirty")

    def test_returns_empty_when_no_sessions(self) -> None:
        self.assertEqual(find_dirty_sessions(self.sessions_root), [])

    def test_returns_empty_when_root_does_not_exist(self) -> None:
        self.assertEqual(find_dirty_sessions(self.sessions_root / "nope"), [])

    def test_invalid_manifest_json_is_skipped(self) -> None:
        session_dir = self.sessions_root / "session_004"
        session_dir.mkdir()
        (session_dir / "session.json").write_text("{not json", encoding="utf-8")
        # Doesn't raise; just doesn't include the broken session.
        self.assertEqual(find_dirty_sessions(self.sessions_root), [])


class ResolveDirtySessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.sessions_root = Path(self._temp_dir.name)
        _write_manifest(
            self.sessions_root / "session_001",
            session_id="session_001",
            state="dirty",
            finalized_at=None,
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_finalize_writes_finalized_state_and_stamps_timestamp(self) -> None:
        resolve_dirty_session(self.sessions_root, "session_001", RecoveryAction.FINALIZE)
        manifest = _read_manifest(self.sessions_root / "session_001")
        self.assertEqual(manifest["state"], "finalized")
        self.assertIsNotNone(manifest["finalized_at"])

    def test_discard_writes_created_state_and_clears_timestamp(self) -> None:
        resolve_dirty_session(self.sessions_root, "session_001", RecoveryAction.DISCARD)
        manifest = _read_manifest(self.sessions_root / "session_001")
        self.assertEqual(manifest["state"], "created")
        self.assertIsNone(manifest["finalized_at"])

    def test_resume_writes_created_state_like_discard(self) -> None:
        # §11.4 Resume normalizes the manifest to `created` so the
        # downstream `SessionManager.adopt_session` call sees a
        # consistent on-disk state. The bootstrap is what actually
        # adopts the session; resolve_dirty_session itself just writes
        # the manifest.
        resolve_dirty_session(self.sessions_root, "session_001", RecoveryAction.RESUME)
        manifest = _read_manifest(self.sessions_root / "session_001")
        self.assertEqual(manifest["state"], "created")
        self.assertIsNone(manifest["finalized_at"])

    def test_missing_manifest_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            resolve_dirty_session(
                self.sessions_root, "session_999", RecoveryAction.FINALIZE
            )


class RunStartupScanTests(unittest.TestCase):
    """End-to-end check that mark + validate run together correctly."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.sessions_root = self.tmp / "sessions"
        self.sessions_root.mkdir()
        self.db = MetadataDb(self.tmp / "metadata.db")

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_marks_dirty_then_validates_segments(self) -> None:
        # Seed a session that crashed mid-recording: state=recording,
        # finalized_at=null, with one valid in-progress segment file.
        session_dir = self.sessions_root / "session_001"
        session_dir.mkdir()
        _write_manifest(
            session_dir,
            session_id="session_001",
            state="recording",
            finalized_at=None,
        )
        feed_dir = session_dir / "recording" / "ndi_main"
        feed_dir.mkdir(parents=True)
        (feed_dir / "segment_00000.mkv").write_bytes(b"\0")
        self.db.create_session(
            session_id="session_001",
            source_name="Test",
            started_at="2026-04-28T00:00:00+00:00",
        )
        report = run_startup_scan(self.sessions_root, self.db)
        # Session was marked dirty.
        self.assertEqual(report.dirty_sessions_marked, ["session_001"])
        manifest = _read_manifest(session_dir)
        self.assertEqual(manifest["state"], "dirty")
        # The segment validator (default cv2) probably rejects the
        # zero-byte stub file → quarantined. Check the file moved.
        original = feed_dir / "segment_00000.mkv"
        quarantine = session_dir / "quarantine" / "ndi_main"
        # Either the file is in quarantine OR (if cv2 reports valid for
        # zero-byte) we have a dirty DB row. Both are fine; what matters
        # is the original location is no longer authoritative.
        if original.exists():
            recovered = self.db.get_segment_by_path(str(original))
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.state, SEGMENT_STATE_DIRTY)
        else:
            self.assertTrue((quarantine / "segment_00000.mkv").exists())

    def test_no_dirty_sessions_returns_empty_report(self) -> None:
        # Pre-existing finalized session — scan finds nothing to do.
        session_dir = self.sessions_root / "session_001"
        session_dir.mkdir()
        _write_manifest(
            session_dir,
            session_id="session_001",
            state="finalized",
            finalized_at="2026-04-28T02:00:00+00:00",
        )
        report = run_startup_scan(self.sessions_root, self.db)
        self.assertEqual(report.dirty_sessions_marked, [])


class FindNextFragmentIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.feed_dir = self.tmp / "recording" / "ndi_main"
        self.feed_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_empty_dir_returns_zero(self) -> None:
        self.assertEqual(find_next_fragment_index(self.feed_dir), 0)

    def test_missing_dir_returns_zero(self) -> None:
        self.assertEqual(
            find_next_fragment_index(self.tmp / "does_not_exist"), 0
        )

    def test_returns_max_plus_one_from_disk(self) -> None:
        for i in (0, 1, 2):
            (self.feed_dir / f"segment_{i:05d}.mkv").write_bytes(b"\0")
        self.assertEqual(find_next_fragment_index(self.feed_dir), 3)

    def test_skips_non_segment_files(self) -> None:
        (self.feed_dir / "segment_00000.mkv").write_bytes(b"\0")
        (self.feed_dir / "stray.txt").write_bytes(b"\0")
        (self.feed_dir / "segment_99999.mkv.tmp").write_bytes(b"\0")
        self.assertEqual(find_next_fragment_index(self.feed_dir), 1)

    def test_db_consult_picks_max_when_higher(self) -> None:
        # Disk has 0, 1, 2 but DB has a quarantined row at 5.
        for i in (0, 1, 2):
            (self.feed_dir / f"segment_{i:05d}.mkv").write_bytes(b"\0")
        db = MetadataDb(self.tmp / "metadata.db")
        try:
            db.create_session(
                session_id="session_001",
                source_name="Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            db.insert_segment(
                _segment(file_path="/q/5.mkv", fragment_index=5, state="quarantined")
            )
            self.assertEqual(
                find_next_fragment_index(
                    self.feed_dir,
                    db=db,
                    session_id="session_001",
                    feed_id="ndi_main",
                ),
                6,
            )
        finally:
            db.close()


class SessionManagerAdoptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_adopt_session_loads_existing_paths_and_transitions_to_created(self) -> None:
        from app.config.settings import AppSettings
        from app.core.session_state import SessionState
        from app.storage.file_manager import FileManager
        from app.storage.session_manager import SessionManager

        # Build a settings object pointed at the tmp dir.
        settings = AppSettings(base_data_dir=self.tmp)
        file_manager = FileManager(settings)
        # Pre-create the session on disk with a DIRTY manifest.
        session_dir = settings.sessions_root / "session_001"
        _write_manifest(
            session_dir,
            session_id="session_001",
            state="dirty",
            finalized_at=None,
        )
        for sub in ("recording", "rolling", "clips"):
            (session_dir / sub).mkdir(parents=True, exist_ok=True)
        db = MetadataDb(settings.metadata_db_path)
        try:
            db.create_session(
                session_id="session_001",
                source_name="Resume Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            sm = SessionManager(file_manager, db)
            paths = sm.adopt_session("session_001")
            self.assertEqual(paths.session_id, "session_001")
            self.assertEqual(paths.root_dir, session_dir)
            # State machine should have driven DIRTY → CREATED.
            state = sm.get_active_session_state()
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.state, SessionState.CREATED)
            # Manifest reflects the transition.
            manifest = _read_manifest(session_dir)
            self.assertEqual(manifest["state"], "created")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
