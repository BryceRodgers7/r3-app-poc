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
    GAME_DIR_FORMAT,
    DirtySessionInfo,
    RecoveryAction,
    SegmentValidationResult,
    find_dirty_sessions,
    find_next_fragment_index,
    find_next_game_index,
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
    quarantine = session_root / "quarantine"
    for d in (session_root, recording):
        d.mkdir(parents=True, exist_ok=True)
    return SessionPaths(
        session_id=session_id,
        root_dir=session_root,
        recording_dir=recording,
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

    def test_per_game_scope_returns_zero_for_fresh_game_folder(self) -> None:
        # Phase 7.B-ext: the coordinator scopes find_next_fragment_index
        # to the per-game folder (`recording/<game_NNN>/<feed_id>/`) so
        # each new game starts at segment_00000 even when prior games
        # in the same session wrote later indices.
        recording_root = self.tmp / "recording"
        # Pre-existing game 1 with three segments.
        game1_feed = recording_root / "game_001" / "ndi_main"
        game1_feed.mkdir(parents=True)
        for i in (0, 1, 2):
            (game1_feed / f"segment_{i:05d}.mkv").write_bytes(b"\0")
        # Fresh game 2 folder doesn't exist yet — that's the path the
        # coordinator passes on the next Start press.
        game2_feed = recording_root / "game_002" / "ndi_main"
        self.assertEqual(find_next_fragment_index(game2_feed), 0)

    def test_per_game_scope_continues_existing_game_folder(self) -> None:
        # Resume case: a game's folder already has files (e.g. from
        # before a crash). Scoping to the game folder picks the right
        # next index past whatever's on disk.
        recording_root = self.tmp / "recording"
        game1_feed = recording_root / "game_001" / "ndi_main"
        game1_feed.mkdir(parents=True)
        for i in (0, 1, 2, 3):
            (game1_feed / f"segment_{i:05d}.mkv").write_bytes(b"\0")
        self.assertEqual(find_next_fragment_index(game1_feed), 4)

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
        (session_dir / "recording").mkdir(parents=True, exist_ok=True)
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


class FindNextGameIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.recording_root = self.tmp / "recording"
        self.recording_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_empty_dir_returns_one(self) -> None:
        self.assertEqual(find_next_game_index(self.recording_root), 1)

    def test_missing_dir_returns_one(self) -> None:
        self.assertEqual(find_next_game_index(self.tmp / "does_not_exist"), 1)

    def test_returns_max_plus_one(self) -> None:
        for i in (1, 2, 3):
            (self.recording_root / GAME_DIR_FORMAT.format(i)).mkdir()
        self.assertEqual(find_next_game_index(self.recording_root), 4)

    def test_skips_non_game_dirs(self) -> None:
        # Legacy per-feed dirs at the same level as game subdirs should
        # not be treated as games.
        (self.recording_root / GAME_DIR_FORMAT.format(1)).mkdir()
        (self.recording_root / "ndi_main").mkdir()
        (self.recording_root / "game_007_extra").mkdir()  # bad suffix
        self.assertEqual(find_next_game_index(self.recording_root), 2)

    def test_handles_gaps_in_numbering(self) -> None:
        (self.recording_root / GAME_DIR_FORMAT.format(1)).mkdir()
        (self.recording_root / GAME_DIR_FORMAT.format(5)).mkdir()
        self.assertEqual(find_next_game_index(self.recording_root), 6)


class FindNextFragmentIndexNestedTests(unittest.TestCase):
    """Recursive walk over `<recording>/<game_NNN>/<feed_id>/segment_*.mkv`.

    The coordinator now passes `session_paths.recording_dir` (not the
    per-feed dir) so the scan covers every game subdir. Filtering by
    `feed_id` keeps multi-feed sessions correct.
    """

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.recording_root = self.tmp / "recording"
        self.recording_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _seed(self, game: int, feed: str, indices: list[int]) -> None:
        feed_dir = self.recording_root / GAME_DIR_FORMAT.format(game) / feed
        feed_dir.mkdir(parents=True, exist_ok=True)
        for i in indices:
            (feed_dir / f"segment_{i:05d}.mkv").write_bytes(b"\0")

    def test_recursive_walk_aggregates_across_games(self) -> None:
        self._seed(1, "ndi_main", [0, 1, 2, 3])
        self._seed(2, "ndi_main", [4, 5])
        self.assertEqual(
            find_next_fragment_index(self.recording_root, feed_id="ndi_main"),
            6,
        )

    def test_feed_id_filter_excludes_other_feeds(self) -> None:
        # ndi_main has segments 0-3; ndi_aux has segments 0-9. The
        # filter should ignore ndi_aux when asking about ndi_main.
        self._seed(1, "ndi_main", [0, 1, 2, 3])
        self._seed(1, "ndi_aux", list(range(10)))
        self.assertEqual(
            find_next_fragment_index(self.recording_root, feed_id="ndi_main"),
            4,
        )
        self.assertEqual(
            find_next_fragment_index(self.recording_root, feed_id="ndi_aux"),
            10,
        )

    def test_legacy_flat_layout_still_works(self) -> None:
        # Pre-game-subdir layout: `recording/<feed>/segment_NNNNN.mkv`.
        flat = self.recording_root / "ndi_main"
        flat.mkdir()
        for i in (0, 1, 2):
            (flat / f"segment_{i:05d}.mkv").write_bytes(b"\0")
        self.assertEqual(
            find_next_fragment_index(self.recording_root, feed_id="ndi_main"),
            3,
        )


class ValidateSessionSegmentsNestedTests(unittest.TestCase):
    """Recovery scan walks game subdirs; feed_id is inferred from parent dir."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.session_paths = _build_session_paths(self.tmp / "sessions", "session_001")
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

    def test_inserts_dirty_rows_for_segments_in_game_subdirs(self) -> None:
        # Two game subdirs with monotonic segment indexes (matches the
        # coordinator's `find_next_fragment_index` behavior across Start
        # cycles — game_002's first segment picks up past game_001's last).
        layout = [
            (1, "ndi_main", 0),
            (1, "ndi_main", 1),
            (2, "ndi_main", 2),
        ]
        for game, feed, idx in layout:
            feed_dir = (
                self.session_paths.recording_dir
                / GAME_DIR_FORMAT.format(game)
                / feed
            )
            feed_dir.mkdir(parents=True, exist_ok=True)
            (feed_dir / f"segment_{idx:05d}.mkv").write_bytes(b"\0")

        always_valid = lambda _p: SegmentValidationResult(
            is_valid=True, duration_seconds=4.0, frame_count=120
        )
        report = validate_session_segments(
            self.session_paths, self.db, validator=always_valid
        )
        self.assertEqual(report.files_scanned, 3)
        self.assertEqual(len(report.files_marked_dirty), 3)
        # All rows landed in the DB with feed_id derived from parent dir.
        rows = list(self.db.segments_for_feed("session_001", "ndi_main"))
        self.assertEqual(len(rows), 3)
        # The recovered rows reflect the recursive walk — file paths
        # span both game subdirs.
        paths = {row.file_path for row in rows}
        self.assertTrue(any("game_001" in p for p in paths))
        self.assertTrue(any("game_002" in p for p in paths))


class FormatLocationGameSubdirTests(unittest.TestCase):
    """Slice 4.A + game-subdir: `format-location` writes nested per-game files."""

    def _build_pm_stub(self):
        from app.media.pipeline_manager import PipelineManager
        from unittest.mock import MagicMock

        pm = PipelineManager.__new__(PipelineManager)
        pm._recording_session_paths = None
        pm._recording_feed_id = None
        pm._recording_codec = "mjpeg"
        pm._recording_container = "mkv"
        pm._recording_segment_counter = 0
        pm._recording_game_subdir = None
        pm._pending_segment = None
        pm._metadata_db = None
        pm._segment_index = None
        pm._splitmuxsink = None
        pm._recording_running = False
        pm._recording_was_disabled = False
        return pm

    def _session_paths(self, root: Path) -> SessionPaths:
        recording = root / "recording"
        for d in (root, recording):
            d.mkdir(parents=True, exist_ok=True)
        return SessionPaths(
            session_id="session_001",
            root_dir=root,
            recording_dir=recording,
        )

    def test_path_includes_game_subdir_when_set(self) -> None:
        from app.media.pipeline_manager import PipelineManager
        with TemporaryDirectory() as tmp:
            session = self._session_paths(Path(tmp))
            pm = self._build_pm_stub()
            pm._recording_session_paths = session
            pm._recording_feed_id = "ndi_main"
            pm._recording_game_subdir = GAME_DIR_FORMAT.format(3)
            path = PipelineManager._on_splitmuxsink_format_location(pm, None, 0)
            self.assertTrue(
                path.endswith("recording/game_003/ndi_main/segment_00000.mkv")
                or path.endswith("recording\\game_003\\ndi_main\\segment_00000.mkv"),
                f"unexpected path: {path}",
            )
            # Per-game per-feed dir is created on demand.
            game_feed_dir = (
                session.recording_dir / GAME_DIR_FORMAT.format(3) / "ndi_main"
            )
            self.assertTrue(game_feed_dir.exists())

    def test_falls_back_to_flat_layout_without_game_subdir(self) -> None:
        from app.media.pipeline_manager import PipelineManager
        with TemporaryDirectory() as tmp:
            session = self._session_paths(Path(tmp))
            pm = self._build_pm_stub()
            pm._recording_session_paths = session
            pm._recording_feed_id = "ndi_main"
            pm._recording_game_subdir = None
            path = PipelineManager._on_splitmuxsink_format_location(pm, None, 0)
            self.assertTrue(
                path.endswith("recording/ndi_main/segment_00000.mkv")
                or path.endswith("recording\\ndi_main\\segment_00000.mkv"),
                f"unexpected path: {path}",
            )


if __name__ == "__main__":
    unittest.main()
