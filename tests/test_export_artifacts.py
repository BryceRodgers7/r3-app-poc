"""Phase 8.C — `export_artifacts` table + idempotent re-runs.

Two surfaces locked in:

  - SQLite schema + CRUD (`insert_export_artifact`,
    `export_artifacts_for_session`, `successful_artifact_keys`).
  - `export_all`'s idempotency layer: prior `success` rows make the
    next run skip; `--force` ignores the skip set; failed attempts
    don't block retries.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.core.models import (
    EXPORT_KIND_LONG_FORM,
    EXPORT_STATUS_FAILED,
    EXPORT_STATUS_SUCCESS,
    ExportArtifact,
)
from app.storage.metadata_db import MetadataDb
from app.tools.long_form_export import (
    ExportResult,
    LongFormExporter,
    export_all,
)
from app.tools.post_session_processor import LongFormPlanItem


def _plan_item(
    *,
    game_subdir: str = "game_001",
    feed_id: str = "ndi_main",
    output_path: Path | None = None,
    segment_paths: tuple[Path, ...] = (),
    duration_ns: int = 4_000_000_000,
) -> LongFormPlanItem:
    return LongFormPlanItem(
        game_subdir=game_subdir,
        feed_id=feed_id,
        segment_count=len(segment_paths) or 1,
        total_duration_ns=duration_ns,
        output_path=output_path or Path(f"/tmp/{game_subdir}/{feed_id}.mp4"),
        segment_paths=segment_paths or (Path("/tmp/seg.mkv"),),
    )


class ExportArtifactSchemaTests(unittest.TestCase):
    """SQLite round-trip for the `export_artifacts` table."""

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

    def _artifact(self, **overrides) -> ExportArtifact:
        defaults = dict(
            session_id="session_001",
            kind=EXPORT_KIND_LONG_FORM,
            game_subdir="game_001",
            feed_id="ndi_main",
            output_path="/tmp/out.mp4",
            status=EXPORT_STATUS_SUCCESS,
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:01:00+00:00",
            size_bytes=1234,
            duration_ns=4_000_000_000,
            error_message=None,
        )
        defaults.update(overrides)
        return ExportArtifact(**defaults)

    def test_insert_and_round_trip(self) -> None:
        artifact_id = self.db.insert_export_artifact(self._artifact())
        self.assertGreater(artifact_id, 0)
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(len(rows), 1)
        a = rows[0]
        self.assertEqual(a.session_id, "session_001")
        self.assertEqual(a.kind, EXPORT_KIND_LONG_FORM)
        self.assertEqual(a.game_subdir, "game_001")
        self.assertEqual(a.feed_id, "ndi_main")
        self.assertEqual(a.status, EXPORT_STATUS_SUCCESS)
        self.assertEqual(a.size_bytes, 1234)
        self.assertEqual(a.duration_ns, 4_000_000_000)

    def test_insert_failed_artifact_with_error_message(self) -> None:
        self.db.insert_export_artifact(self._artifact(
            status=EXPORT_STATUS_FAILED,
            error_message="ffmpeg returncode=1; stderr: bad caps",
            size_bytes=None,
            finalized_at="2026-04-29T00:00:30+00:00",
        ))
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, EXPORT_STATUS_FAILED)
        self.assertIn("returncode=1", rows[0].error_message)
        self.assertIsNone(rows[0].size_bytes)

    def test_successful_artifact_keys_returns_only_success_rows(self) -> None:
        # Two failures + one success for game_001; nothing for game_002.
        self.db.insert_export_artifact(self._artifact(
            status=EXPORT_STATUS_FAILED, error_message="boom"
        ))
        self.db.insert_export_artifact(self._artifact(
            status=EXPORT_STATUS_FAILED, error_message="boom 2"
        ))
        self.db.insert_export_artifact(self._artifact(
            status=EXPORT_STATUS_SUCCESS
        ))
        keys = self.db.successful_artifact_keys("session_001")
        self.assertEqual(
            keys, {(EXPORT_KIND_LONG_FORM, "game_001", "ndi_main")}
        )

    def test_successful_artifact_keys_distinguishes_kinds_and_feeds(self) -> None:
        self.db.insert_export_artifact(self._artifact(
            game_subdir="game_001", feed_id="ndi_a"
        ))
        self.db.insert_export_artifact(self._artifact(
            game_subdir="game_001", feed_id="ndi_b"
        ))
        self.db.insert_export_artifact(self._artifact(
            game_subdir="game_002", feed_id="ndi_a"
        ))
        keys = self.db.successful_artifact_keys("session_001")
        self.assertEqual(keys, {
            (EXPORT_KIND_LONG_FORM, "game_001", "ndi_a"),
            (EXPORT_KIND_LONG_FORM, "game_001", "ndi_b"),
            (EXPORT_KIND_LONG_FORM, "game_002", "ndi_a"),
        })

    def test_empty_session_yields_empty_keyset(self) -> None:
        self.assertEqual(
            self.db.successful_artifact_keys("session_999"), set()
        )


class IdempotencyTests(unittest.TestCase):
    """`export_all` skips items with a prior success row, retries
    failures, and `--force` re-encodes everything."""

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

    def _exporter_returning(
        self, status_per_call: list[str]
    ) -> LongFormExporter:
        ex = mock.Mock(spec=LongFormExporter)
        results: list[ExportResult] = []
        ex.export.side_effect = lambda plan_item, _segs: ExportResult(
            plan_item=plan_item,
            status=status_per_call.pop(0),
            output_path=plan_item.output_path,
            error_message=("simulated") if status_per_call and status_per_call[0] == "skip" else None,
            size_bytes=100,
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:01:00+00:00",
        )
        return ex

    def test_first_run_encodes_all_and_persists_success_rows(self) -> None:
        items = [
            _plan_item(game_subdir="game_001"),
            _plan_item(game_subdir="game_002"),
        ]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = lambda plan_item, _segs: ExportResult(
            plan_item=plan_item,
            status="success",
            output_path=plan_item.output_path,
            size_bytes=100,
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:01:00+00:00",
        )
        results = export_all(
            exporter, items, segment_paths_for=lambda i: [],
            db=self.db, session_id="session_001",
        )
        self.assertEqual([r.status for r in results], ["success", "success"])
        self.assertEqual(exporter.export.call_count, 2)
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.status == EXPORT_STATUS_SUCCESS for r in rows))

    def test_second_run_skips_already_successful_artifacts(self) -> None:
        items = [_plan_item(game_subdir="game_001")]
        # Pre-seed a success row.
        self.db.insert_export_artifact(ExportArtifact(
            session_id="session_001",
            kind=EXPORT_KIND_LONG_FORM,
            game_subdir="game_001",
            feed_id="ndi_main",
            output_path="/tmp/out.mp4",
            status=EXPORT_STATUS_SUCCESS,
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:01:00+00:00",
            size_bytes=100,
            duration_ns=4_000_000_000,
        ))
        exporter = mock.Mock(spec=LongFormExporter)
        results = export_all(
            exporter, items, segment_paths_for=lambda i: [],
            db=self.db, session_id="session_001",
        )
        # No encoding happened.
        exporter.export.assert_not_called()
        self.assertEqual([r.status for r in results], ["skipped"])
        # No new DB row was inserted (the pre-existing one stays).
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(len(rows), 1)

    def test_force_re_encodes_even_with_prior_success(self) -> None:
        items = [_plan_item(game_subdir="game_001")]
        self.db.insert_export_artifact(ExportArtifact(
            session_id="session_001",
            kind=EXPORT_KIND_LONG_FORM,
            game_subdir="game_001",
            feed_id="ndi_main",
            output_path="/tmp/out.mp4",
            status=EXPORT_STATUS_SUCCESS,
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:01:00+00:00",
        ))
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = lambda plan_item, _segs: ExportResult(
            plan_item=plan_item,
            status="success",
            output_path=plan_item.output_path,
            size_bytes=200,
            started_at="2026-04-29T01:00:00+00:00",
            finalized_at="2026-04-29T01:01:00+00:00",
        )
        results = export_all(
            exporter, items, segment_paths_for=lambda i: [],
            db=self.db, session_id="session_001", force=True,
        )
        # Force re-encodes.
        exporter.export.assert_called_once()
        self.assertEqual([r.status for r in results], ["success"])
        # Now there are TWO success rows (history is preserved).
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.status == EXPORT_STATUS_SUCCESS for r in rows))

    def test_failed_artifact_can_be_retried(self) -> None:
        # Prior failure should NOT block a retry — only successes do.
        items = [_plan_item(game_subdir="game_001")]
        self.db.insert_export_artifact(ExportArtifact(
            session_id="session_001",
            kind=EXPORT_KIND_LONG_FORM,
            game_subdir="game_001",
            feed_id="ndi_main",
            output_path="/tmp/out.mp4",
            status=EXPORT_STATUS_FAILED,
            error_message="boom",
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:00:30+00:00",
        ))
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = lambda plan_item, _segs: ExportResult(
            plan_item=plan_item,
            status="success",
            output_path=plan_item.output_path,
            size_bytes=300,
            started_at="2026-04-29T01:00:00+00:00",
            finalized_at="2026-04-29T01:01:00+00:00",
        )
        results = export_all(
            exporter, items, segment_paths_for=lambda i: [],
            db=self.db, session_id="session_001",
        )
        self.assertEqual([r.status for r in results], ["success"])
        rows = self.db.export_artifacts_for_session("session_001")
        # Both attempts persist: the original failure + the new success.
        self.assertEqual(len(rows), 2)
        statuses = sorted(r.status for r in rows)
        self.assertEqual(statuses, [EXPORT_STATUS_FAILED, EXPORT_STATUS_SUCCESS])

    def test_failed_attempts_persist_with_error_message(self) -> None:
        items = [_plan_item(game_subdir="game_001")]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = lambda plan_item, _segs: ExportResult(
            plan_item=plan_item,
            status="failed",
            output_path=plan_item.output_path,
            error_message="ffmpeg blew up",
            started_at="2026-04-29T00:00:00+00:00",
            finalized_at="2026-04-29T00:00:30+00:00",
        )
        export_all(
            exporter, items, segment_paths_for=lambda i: [],
            db=self.db, session_id="session_001",
        )
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, EXPORT_STATUS_FAILED)
        self.assertIn("ffmpeg blew up", rows[0].error_message)

    def test_no_db_means_no_persistence_or_skip(self) -> None:
        # Backward-compat: callers that don't supply a DB get the
        # 8.B encode-and-collect behavior with no skip layer.
        items = [_plan_item(game_subdir="game_001")]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = lambda plan_item, _segs: ExportResult(
            plan_item=plan_item,
            status="success",
            output_path=plan_item.output_path,
            size_bytes=100,
        )
        results = export_all(exporter, items, segment_paths_for=lambda i: [])
        self.assertEqual([r.status for r in results], ["success"])
        # DB has nothing — no insert happened.
        rows = self.db.export_artifacts_for_session("session_001")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
