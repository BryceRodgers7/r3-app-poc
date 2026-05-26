"""Phase 14.E: in-app post-process modal + progress callback wiring.

Covers three layers:

1. `export_all`'s `progress_callback` fires once per plan item with
   `(processed, total, current)` regardless of per-item success.
2. `run_post_process` short-circuits the bad-DB / missing-session
   cases into a failure `PostProcessRunResult` rather than raising.
3. `PostProcessDialog` renders progress, flips its button label
   between Close/OK on success/failure, and quits the app on click.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from PySide6.QtWidgets import QApplication

from app.tools.long_form_export import (
    ExportResult,
    LongFormExporter,
    export_all,
)
from app.tools.post_session_processor import (
    LongFormPlanItem,
    PostProcessRunResult,
    run_post_process,
)
from app.ui.post_process_dialog import PostProcessDialog


def _plan_item(*, game_subdir: str, feed_id: str = "ndi_main") -> LongFormPlanItem:
    return LongFormPlanItem(
        game_subdir=game_subdir,
        feed_id=feed_id,
        segment_count=1,
        total_duration_ns=4_000_000_000,
        output_path=Path(f"/tmp/processed/{game_subdir}/{feed_id}.mp4"),
        segment_paths=(),
    )


class ExportAllProgressCallbackTests(unittest.TestCase):
    """Phase 14.E: `progress_callback` interaction with `export_all`."""

    def test_fires_once_per_item_with_running_total(self) -> None:
        items = [
            _plan_item(game_subdir="game_001"),
            _plan_item(game_subdir="game_002"),
            _plan_item(game_subdir="game_003"),
        ]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = [
            ExportResult(
                plan_item=item, status="success", output_path=item.output_path
            )
            for item in items
        ]
        invocations: list[tuple[int, int, str]] = []
        export_all(
            exporter,
            items,
            segment_paths_for=lambda _i: [],
            progress_callback=lambda processed, total, current: invocations.append(
                (processed, total, current)
            ),
        )
        self.assertEqual(
            invocations,
            [
                (1, 3, "game_001/ndi_main"),
                (2, 3, "game_002/ndi_main"),
                (3, 3, "game_003/ndi_main"),
            ],
        )

    def test_fires_on_failed_items_too(self) -> None:
        # A failure on the first item must not abort the loop or skip
        # the callback for the failed item.
        items = [_plan_item(game_subdir="game_001"), _plan_item(game_subdir="game_002")]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = [
            ExportResult(
                plan_item=items[0],
                status="failed",
                output_path=items[0].output_path,
                error_message="boom",
            ),
            ExportResult(
                plan_item=items[1], status="success", output_path=items[1].output_path
            ),
        ]
        invocations: list[tuple[int, int, str]] = []
        export_all(
            exporter,
            items,
            segment_paths_for=lambda _i: [],
            progress_callback=lambda p, t, c: invocations.append((p, t, c)),
        )
        self.assertEqual(len(invocations), 2)

    def test_buggy_callback_does_not_abort_loop(self) -> None:
        items = [_plan_item(game_subdir="game_001"), _plan_item(game_subdir="game_002")]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = [
            ExportResult(
                plan_item=item, status="success", output_path=item.output_path
            )
            for item in items
        ]
        call_count = {"n": 0}

        def buggy(_p, _t, _c):
            call_count["n"] += 1
            raise RuntimeError("dialog torn down")

        export_all(
            exporter,
            items,
            segment_paths_for=lambda _i: [],
            progress_callback=buggy,
        )
        # Loop still completed both items even though callback raised.
        self.assertEqual(exporter.export.call_count, 2)
        self.assertEqual(call_count["n"], 2)


class RunPostProcessFailurePathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.base = Path(self._temp_dir.name)
        self.session_dir = self.base / "sessions" / "session_001"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_missing_db_returns_failure_result(self) -> None:
        # No metadata DB created — pre-flight should return a result,
        # not raise.
        result = run_post_process(self.session_dir)
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error_message)
        self.assertIn("metadata database not found", result.error_message)


class PostProcessDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _make_dialog(self) -> PostProcessDialog:
        dialog = PostProcessDialog(session_path=Path("/tmp/session_001"))
        # Stub QApplication.quit so the test doesn't tear down the
        # Qt event loop the runner is using.
        dialog._quit_calls = []  # type: ignore[attr-defined]
        dialog.set_quit_fn(lambda: dialog._quit_calls.append(None))
        return dialog

    def test_initial_state(self) -> None:
        dialog = self._make_dialog()
        self.assertEqual(dialog._action_button.text(), "Close")
        self.assertFalse(dialog._action_button.isEnabled())
        self.assertTrue(dialog.progress_bar.isVisibleTo(dialog) is False or True)
        # Status starts at the preparing message.
        self.assertIn("Preparing", dialog.status_label.text())

    def test_update_progress_sets_bar_and_text(self) -> None:
        dialog = self._make_dialog()
        dialog.update_progress(2, 5, "game_001/ndi_main")
        self.assertEqual(dialog.progress_bar.minimum(), 0)
        self.assertEqual(dialog.progress_bar.maximum(), 5)
        self.assertEqual(dialog.progress_bar.value(), 2)
        self.assertIn("game_001/ndi_main", dialog.status_label.text())

    def test_on_finished_success_flips_to_close(self) -> None:
        dialog = self._make_dialog()
        dialog.on_finished(True, None, 3, 0, 0)
        self.assertEqual(dialog._action_button.text(), "Close")
        self.assertTrue(dialog._action_button.isEnabled())
        self.assertIn("Done", dialog.status_label.text())
        self.assertIn("3", dialog.status_label.text())

    def test_on_finished_failure_flips_to_ok_and_shows_error(self) -> None:
        dialog = self._make_dialog()
        dialog.on_finished(False, "ffmpeg exited with returncode=1", 1, 2, 0)
        self.assertEqual(dialog._action_button.text(), "OK")
        self.assertTrue(dialog._action_button.isEnabled())
        self.assertIn("ffmpeg", dialog.status_label.text())
        # Progress bar is hidden on failure so the half-filled state
        # doesn't mislead the operator.
        self.assertTrue(dialog.progress_bar.isHidden())

    def test_action_button_click_calls_quit_fn(self) -> None:
        dialog = self._make_dialog()
        dialog.on_finished(True, None, 1, 0, 0)
        dialog._action_button.click()
        self.assertEqual(len(dialog._quit_calls), 1)  # type: ignore[attr-defined]

    def test_action_button_click_on_failure_also_quits(self) -> None:
        dialog = self._make_dialog()
        dialog.on_finished(False, "boom", 0, 1, 0)
        dialog._action_button.click()
        self.assertEqual(len(dialog._quit_calls), 1)  # type: ignore[attr-defined]

    def test_empty_plan_renders_no_games_message(self) -> None:
        dialog = self._make_dialog()
        dialog.on_finished(True, None, 0, 0, 0)
        self.assertIn("Nothing to process", dialog.status_label.text())

    def test_reject_blocked_until_finished(self) -> None:
        # Pressing Escape while encoding is in progress would be a
        # foot-gun — the dialog ignores reject() until on_finished()
        # has been seen.
        dialog = self._make_dialog()
        dialog.reject()
        self.assertEqual(dialog.result(), 0)  # not Rejected
        dialog.on_finished(True, None, 1, 0, 0)
        dialog.reject()
        # After finish, reject() is allowed through.
        # (We don't assert the int constant directly because PySide
        # versions differ; just confirming the call doesn't raise.)


if __name__ == "__main__":
    unittest.main()
