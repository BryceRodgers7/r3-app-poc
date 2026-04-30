"""Tests for the Phase 8.A post-session MP4 processor CLI."""

from __future__ import annotations

import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_WRITING,
    Segment,
)
from app.core.session_state import SESSION_MANIFEST_FILENAME, SessionState
from app.storage.metadata_db import MetadataDb
from app.tools.post_session_processor import (
    LOCK_FILENAME,
    LongFormPlanItem,
    ValidationError,
    acquire_lock,
    build_plan,
    main,
    print_plan,
    validate_session_state,
)


def _write_manifest(session_dir: Path, *, state: str) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_dir.name,
        "state": state,
        "created_at": "2026-04-28T00:00:00+00:00",
        "finalized_at": (
            "2026-04-28T01:00:00+00:00"
            if state == SessionState.FINALIZED.value
            else None
        ),
    }
    (session_dir / SESSION_MANIFEST_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _segment(
    *,
    session_id: str,
    feed_id: str,
    fragment_index: int,
    file_path: str,
    state: str = SEGMENT_STATE_COMPLETE,
    duration_ns: int = 4_000_000_000,
) -> Segment:
    return Segment(
        session_id=session_id,
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=file_path,
        codec="mjpeg",
        container="mkv",
        start_pts_ns=fragment_index * duration_ns,
        end_pts_ns=(fragment_index + 1) * duration_ns,
        duration_ns=duration_ns,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at=(
            "2026-04-28T01:00:04+00:00"
            if state == SEGMENT_STATE_COMPLETE
            else None
        ),
    )


class ValidateSessionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.session_dir = Path(self._temp_dir.name) / "session_001"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_finalized_state_accepted(self) -> None:
        _write_manifest(self.session_dir, state="finalized")
        manifest = validate_session_state(self.session_dir)
        self.assertEqual(manifest["state"], "finalized")

    def test_each_invalid_state_rejected_with_named_state(self) -> None:
        # Each state in the SessionState enum that isn't `finalized`.
        for invalid in ("created", "recording", "stopped", "dirty", "archived"):
            with self.subTest(state=invalid):
                _write_manifest(self.session_dir, state=invalid)
                with self.assertRaises(ValidationError) as cm:
                    validate_session_state(self.session_dir)
                # Error message should name the actual state we got.
                self.assertIn(repr(invalid), str(cm.exception))

    def test_unknown_state_rejected(self) -> None:
        _write_manifest(self.session_dir, state="zoinks")
        with self.assertRaises(ValidationError):
            validate_session_state(self.session_dir)

    def test_missing_manifest_rejected(self) -> None:
        # No session.json written.
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValidationError) as cm:
            validate_session_state(self.session_dir)
        self.assertIn(SESSION_MANIFEST_FILENAME, str(cm.exception))

    def test_invalid_json_rejected(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / SESSION_MANIFEST_FILENAME).write_text(
            "{ this is not json", encoding="utf-8"
        )
        with self.assertRaises(ValidationError) as cm:
            validate_session_state(self.session_dir)
        self.assertIn("not valid JSON", str(cm.exception))


class AcquireLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.session_dir = Path(self._temp_dir.name) / "session_001"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.session_dir / LOCK_FILENAME

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_lock_created_and_removed(self) -> None:
        self.assertFalse(self.lock_path.exists())
        with acquire_lock(self.session_dir) as lock_path:
            self.assertEqual(lock_path, self.lock_path)
            self.assertTrue(lock_path.exists())
            # Lock file contains a PID.
            content = lock_path.read_text(encoding="utf-8")
            self.assertEqual(int(content), os.getpid())
        # Released after context exit.
        self.assertFalse(self.lock_path.exists())

    def test_concurrent_lock_rejected(self) -> None:
        with acquire_lock(self.session_dir):
            with self.assertRaises(ValidationError) as cm:
                with acquire_lock(self.session_dir):
                    self.fail("inner lock should not have been acquired")
            self.assertIn(str(self.lock_path), str(cm.exception))

    def test_lock_released_on_exception(self) -> None:
        # Body of the `with` raises — finally must still remove the lock.
        with self.assertRaises(RuntimeError):
            with acquire_lock(self.session_dir):
                raise RuntimeError("simulated failure")
        self.assertFalse(self.lock_path.exists())


class BuildPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.tmp = Path(self._temp_dir.name)
        self.session_dir = self.tmp / "session_001"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db = MetadataDb(self.tmp / "metadata.db")
        self.db.create_session(
            session_id="session_001",
            source_name="Test",
            started_at="2026-04-28T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def _add(self, **kwargs) -> None:
        self.db.insert_segment(_segment(session_id="session_001", **kwargs))

    def test_groups_by_game_and_feed(self) -> None:
        # game_001: 3 segments for ndi_main (idx 0..2), 2 for ndi_angle.
        # game_002: 5 segments for ndi_main, restarting at fragment_index=0
        # per the Phase 7.B-ext per-game reset (and the Phase 7.F schema
        # fix that allows it).
        recording = self.session_dir / "recording"
        for i in range(3):
            self._add(
                feed_id="ndi_main",
                fragment_index=i,
                file_path=str(recording / "game_001" / "ndi_main" / f"segment_{i:05d}.mkv"),
            )
        for i in range(2):
            self._add(
                feed_id="ndi_angle",
                fragment_index=i,
                file_path=str(recording / "game_001" / "ndi_angle" / f"segment_{i:05d}.mkv"),
            )
        for i in range(5):
            self._add(
                feed_id="ndi_main",
                fragment_index=i,
                file_path=str(recording / "game_002" / "ndi_main" / f"segment_{i:05d}.mkv"),
            )

        plan = build_plan(self.db, self.session_dir)

        self.assertEqual(plan.session_id, "session_001")
        self.assertEqual(len(plan.long_form), 3)
        # Sorted by (game_subdir, feed_id).
        items = {(i.game_subdir, i.feed_id): i for i in plan.long_form}
        self.assertEqual(items[("game_001", "ndi_main")].segment_count, 3)
        self.assertEqual(items[("game_001", "ndi_angle")].segment_count, 2)
        self.assertEqual(items[("game_002", "ndi_main")].segment_count, 5)
        # Total durations sum cleanly.
        self.assertEqual(
            items[("game_001", "ndi_main")].total_duration_ns, 12_000_000_000
        )
        self.assertEqual(
            items[("game_002", "ndi_main")].total_duration_ns, 20_000_000_000
        )
        # Output paths under <session>/processed/<game>/<feed>.mp4.
        expected = self.session_dir / "processed" / "game_001" / "ndi_main.mp4"
        self.assertEqual(items[("game_001", "ndi_main")].output_path, expected)

    def test_skips_writing_segments(self) -> None:
        recording = self.session_dir / "recording"
        self._add(
            feed_id="ndi_main",
            fragment_index=0,
            file_path=str(recording / "game_001" / "ndi_main" / "segment_00000.mkv"),
        )
        self._add(
            feed_id="ndi_main",
            fragment_index=1,
            file_path=str(recording / "game_001" / "ndi_main" / "segment_00001.mkv"),
            state=SEGMENT_STATE_WRITING,
        )

        plan = build_plan(self.db, self.session_dir)
        self.assertEqual(len(plan.long_form), 1)
        self.assertEqual(plan.long_form[0].segment_count, 1)

    def test_skips_legacy_flat_layout_segments(self) -> None:
        # Flat layout: <recording>/<feed_id>/segment_*.mkv (no game_NNN/).
        recording = self.session_dir / "recording"
        self._add(
            feed_id="ndi_main",
            fragment_index=0,
            file_path=str(recording / "ndi_main" / "segment_00000.mkv"),
        )
        plan = build_plan(self.db, self.session_dir)
        self.assertEqual(plan.long_form, [])

    def test_substring_collision_does_not_pick_wrong_game(self) -> None:
        # `game_001` is a substring of `game_0011`. Path-component
        # match must distinguish.
        recording = self.session_dir / "recording"
        self._add(
            feed_id="ndi_main",
            fragment_index=0,
            file_path=str(recording / "game_0011" / "ndi_main" / "segment_00000.mkv"),
        )
        # The `_game_subdir_for_segment` matcher accepts only
        # `game_NNN` style names where the suffix is all-digits, so a
        # 4-digit name still matches as `game_0011`. Confirm we get
        # `game_0011`, not `game_001`.
        plan = build_plan(self.db, self.session_dir)
        self.assertEqual(len(plan.long_form), 1)
        self.assertEqual(plan.long_form[0].game_subdir, "game_0011")

    def test_empty_session_yields_empty_plan(self) -> None:
        plan = build_plan(self.db, self.session_dir)
        self.assertEqual(plan.long_form, [])


class PrintPlanTests(unittest.TestCase):
    def test_renders_session_id_and_each_item(self) -> None:
        plan_session_id = "session_042"
        output_path = Path("/tmp/out.mp4")
        item = LongFormPlanItem(
            game_subdir="game_001",
            feed_id="ndi_main",
            segment_count=12,
            total_duration_ns=48_000_000_000,
            output_path=output_path,
        )
        from app.tools.post_session_processor import ProcessingPlan
        plan = ProcessingPlan(session_id=plan_session_id, long_form=[item])

        buf = StringIO()
        with redirect_stdout(buf):
            print_plan(plan)
        out = buf.getvalue()
        self.assertIn("session_042", out)
        self.assertIn("game_001/ndi_main", out)
        self.assertIn("12 segments", out)
        self.assertIn("48.0s", out)
        # Path renders with the platform-native separator (Path.__str__).
        self.assertIn(str(output_path), out)

    def test_renders_no_segments_message(self) -> None:
        from app.tools.post_session_processor import ProcessingPlan
        plan = ProcessingPlan(session_id="session_042", long_form=[])
        buf = StringIO()
        with redirect_stdout(buf):
            print_plan(plan)
        out = buf.getvalue()
        self.assertIn("session_042", out)
        self.assertIn("no exportable", out)


class MainEndToEndTests(unittest.TestCase):
    """Drive `main()` against a fully synthesized session on disk."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        base = Path(self._temp_dir.name)
        self.sessions_root = base / "sessions"
        self.session_dir = self.sessions_root / "session_001"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = base / "metadata.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _populate_db(self) -> None:
        db = MetadataDb(self.db_path)
        try:
            db.create_session(
                session_id="session_001",
                source_name="Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            recording = self.session_dir / "recording"
            for i in range(2):
                db.insert_segment(_segment(
                    session_id="session_001",
                    feed_id="ndi_main",
                    fragment_index=i,
                    file_path=str(
                        recording / "game_001" / "ndi_main" / f"segment_{i:05d}.mkv"
                    ),
                ))
        finally:
            db.close()

    def test_main_returns_zero_on_finalized_session(self) -> None:
        _write_manifest(self.session_dir, state="finalized")
        self._populate_db()
        rc = main([str(self.session_dir), "--metadata-db", str(self.db_path)])
        self.assertEqual(rc, 0)

    def test_main_returns_two_on_dirty_session(self) -> None:
        _write_manifest(self.session_dir, state="dirty")
        self._populate_db()
        rc = main([str(self.session_dir), "--metadata-db", str(self.db_path)])
        self.assertEqual(rc, 2)

    def test_main_returns_two_when_db_missing(self) -> None:
        _write_manifest(self.session_dir, state="finalized")
        # Don't create the DB.
        rc = main([str(self.session_dir), "--metadata-db", str(self.db_path)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
