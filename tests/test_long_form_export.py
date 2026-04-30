"""Phase 8.B — long-form MP4 export tests.

Real ffmpeg invocation isn't required to lock in the encoder's
contract. These tests instead:

  - Verify ffmpeg arg construction by inspecting `_build_ffmpeg_args`.
  - Verify concat-list format by reading the temp file ffmpeg would
    receive (via a `subprocess.run` patch that captures the args).
  - Verify success / failure / missing-ffmpeg paths against a stubbed
    `subprocess.run`.

A live ffmpeg integration test is skipped here — that lives outside
unit-test scope (manual verification or a future end-to-end harness).
"""

from __future__ import annotations

import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.tools.long_form_export import (
    ExportResult,
    FfmpegNotFoundError,
    LongFormExporter,
    _format_concat_line,
    export_all,
)
from app.tools.post_session_processor import LongFormPlanItem


def _plan_item(
    *,
    game_subdir: str = "game_001",
    feed_id: str = "ndi_main",
    output_path: Path | None = None,
    segment_paths: tuple[Path, ...] = (),
) -> LongFormPlanItem:
    return LongFormPlanItem(
        game_subdir=game_subdir,
        feed_id=feed_id,
        segment_count=len(segment_paths),
        total_duration_ns=4_000_000_000 * len(segment_paths),
        output_path=output_path or Path("/tmp/out.mp4"),
        segment_paths=segment_paths,
    )


class FfmpegResolutionTests(unittest.TestCase):
    def test_explicit_path_used_when_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            ffmpeg = Path(tmp) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"")
            exporter = LongFormExporter(ffmpeg_path=ffmpeg)
            self.assertEqual(exporter.ffmpeg_path, ffmpeg.resolve())

    def test_explicit_path_missing_raises(self) -> None:
        with self.assertRaises(FfmpegNotFoundError) as cm:
            LongFormExporter(ffmpeg_path=Path("/nonexistent/ffmpeg.exe"))
        self.assertIn("does not exist", str(cm.exception))

    def test_path_lookup_when_no_override(self) -> None:
        # `shutil.which` returns whatever the OS PATH lookup yielded;
        # `Path()` normalizes, so compare via Path equality (avoids
        # "/" vs "\\" portability issues on Windows).
        with mock.patch("app.tools.long_form_export.shutil.which") as which:
            which.return_value = "/fake/bin/ffmpeg"
            exporter = LongFormExporter()
            self.assertEqual(exporter.ffmpeg_path, Path("/fake/bin/ffmpeg"))

    def test_missing_ffmpeg_raises_with_install_hint(self) -> None:
        with mock.patch("app.tools.long_form_export.shutil.which") as which:
            which.return_value = None
            with self.assertRaises(FfmpegNotFoundError) as cm:
                LongFormExporter()
            self.assertIn("not found", str(cm.exception))
            self.assertIn("pacman", str(cm.exception))


class FfmpegArgsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Build an exporter without touching the filesystem; bypass
        # the resolver since we just want to inspect arg construction.
        self.exporter = LongFormExporter.__new__(LongFormExporter)
        self.exporter._ffmpeg_path = Path("/fake/ffmpeg")

    def test_args_include_concat_demuxer_and_codecs(self) -> None:
        concat_path = Path("/tmp/concat.txt")
        output_path = Path("/tmp/out.mp4")
        args = self.exporter._build_ffmpeg_args(concat_path, output_path)
        self.assertEqual(args[0], str(Path("/fake/ffmpeg")))
        self.assertIn("-f", args)
        self.assertIn("concat", args)
        # `-safe 0` allows absolute paths in the concat list.
        self.assertIn("-safe", args)
        idx = args.index("-safe")
        self.assertEqual(args[idx + 1], "0")
        # H.264 + AAC are the MP4 codec defaults.
        self.assertIn("libx264", args)
        self.assertIn("aac", args)
        # `-y` overwrites existing output (8.C will skip via DB instead).
        self.assertIn("-y", args)
        # Output path is the last positional.
        self.assertEqual(args[-1], str(output_path))

    def test_args_include_input_concat_list(self) -> None:
        concat_path = Path("/tmp/concat.txt")
        args = self.exporter._build_ffmpeg_args(
            concat_path,
            Path("/tmp/out.mp4"),
        )
        # `-i <list>` must appear between the concat demuxer flags
        # and the codec flags.
        idx = args.index("-i")
        self.assertEqual(args[idx + 1], str(concat_path))


class ConcatListFormatTests(unittest.TestCase):
    def test_simple_path(self) -> None:
        # `Path.__str__` uses the platform separator; the concat-line
        # format wraps that string verbatim. Test against the Path's
        # string form so the assertion holds on Windows + POSIX.
        path = Path("/tmp/segment_00000.mkv")
        self.assertEqual(_format_concat_line(path), f"file '{path}'\n")

    def test_path_with_apostrophe_escaped(self) -> None:
        # Defensive: ffmpeg's concat demuxer wraps paths in single
        # quotes; embedded apostrophes must be escaped as `'\''`.
        path = Path("/tmp/it's a path.mkv")
        expected = f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
        self.assertEqual(_format_concat_line(path), expected)


class ExportTests(unittest.TestCase):
    """Drive `LongFormExporter.export` against a stubbed subprocess.run."""

    def _make_exporter(self) -> LongFormExporter:
        ex = LongFormExporter.__new__(LongFormExporter)
        ex._ffmpeg_path = Path("/fake/ffmpeg")
        return ex

    def test_no_segments_returns_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.mp4"
            item = _plan_item(output_path=out, segment_paths=())
            with mock.patch(
                "app.tools.long_form_export.subprocess.run"
            ) as run:
                result = self._make_exporter().export(item, [])
            run.assert_not_called()  # short-circuits before invoking ffmpeg
            self.assertEqual(result.status, "failed")
            self.assertIn("no segments", result.error_message)

    def test_success_path_returns_success_with_size(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "out.mp4"
            seg1 = tmp_path / "seg_00.mkv"
            seg2 = tmp_path / "seg_01.mkv"
            for seg in (seg1, seg2):
                seg.write_bytes(b"")
            item = _plan_item(output_path=out, segment_paths=(seg1, seg2))

            captured_args: list[list[str]] = []
            captured_concat: list[str] = []

            def fake_run(args, capture_output, text, check):
                captured_args.append(list(args))
                # Find the `-i` arg → the concat list path.
                concat_path = Path(args[args.index("-i") + 1])
                captured_concat.append(concat_path.read_text(encoding="utf-8"))
                # Simulate successful ffmpeg: write a fake output file.
                Path(args[-1]).write_bytes(b"x" * 100)
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "app.tools.long_form_export.subprocess.run", fake_run
            ):
                result = self._make_exporter().export(
                    item, [seg1, seg2]
                )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.output_path, out)
            self.assertEqual(result.size_bytes, 100)
            self.assertIsNone(result.error_message)
            # Concat list listed both segments in order.
            self.assertEqual(len(captured_concat), 1)
            lines = [l for l in captured_concat[0].splitlines() if l.strip()]
            self.assertEqual(len(lines), 2)
            self.assertIn(str(seg1), lines[0])
            self.assertIn(str(seg2), lines[1])
            # Source files unmodified.
            self.assertEqual(seg1.read_bytes(), b"")
            self.assertEqual(seg2.read_bytes(), b"")

    def test_failure_path_captures_stderr(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "out.mp4"
            seg = tmp_path / "seg_00.mkv"
            seg.write_bytes(b"")
            item = _plan_item(output_path=out, segment_paths=(seg,))

            with mock.patch(
                "app.tools.long_form_export.subprocess.run"
            ) as run:
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr="ffmpeg: invalid file foo.mkv",
                )
                result = self._make_exporter().export(item, [seg])

            self.assertEqual(result.status, "failed")
            self.assertIn("returncode=1", result.error_message)
            self.assertIn("invalid file foo.mkv", result.error_message)

    def test_concat_list_cleaned_up_after_run(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "out.mp4"
            seg = tmp_path / "seg.mkv"
            seg.write_bytes(b"")
            item = _plan_item(output_path=out, segment_paths=(seg,))

            captured_concat_path: list[Path] = []

            def fake_run(args, capture_output, text, check):
                concat_path = Path(args[args.index("-i") + 1])
                captured_concat_path.append(concat_path)
                Path(args[-1]).write_bytes(b"")
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "app.tools.long_form_export.subprocess.run", fake_run
            ):
                self._make_exporter().export(item, [seg])

            # The concat list file is removed after export, win or lose.
            self.assertEqual(len(captured_concat_path), 1)
            self.assertFalse(captured_concat_path[0].exists())


class ExportAllTests(unittest.TestCase):
    def test_iterates_all_items_collecting_results(self) -> None:
        items = [
            _plan_item(game_subdir=f"game_{i:03d}")
            for i in range(1, 4)
        ]
        exporter = mock.Mock(spec=LongFormExporter)
        exporter.export.side_effect = [
            ExportResult(plan_item=items[0], status="success", output_path=items[0].output_path),
            ExportResult(plan_item=items[1], status="failed", output_path=items[1].output_path, error_message="boom"),
            ExportResult(plan_item=items[2], status="success", output_path=items[2].output_path),
        ]
        results = export_all(exporter, items, segment_paths_for=lambda i: [])
        self.assertEqual([r.status for r in results], ["success", "failed", "success"])
        self.assertEqual(exporter.export.call_count, 3)


class MainCliEncodingTests(unittest.TestCase):
    """End-to-end main(): verifies dry-run, ffmpeg-missing, and the
    success/failure exit codes via stubbed ffmpeg."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        base = Path(self._temp_dir.name)
        self.sessions_root = base / "sessions"
        self.session_dir = self.sessions_root / "session_001"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = base / "metadata.db"

        # Create a finalized session manifest + DB with one segment
        # so build_plan returns one artifact.
        manifest = {
            "session_id": "session_001",
            "state": "finalized",
            "created_at": "2026-04-28T00:00:00+00:00",
            "finalized_at": "2026-04-28T01:00:00+00:00",
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        from app.storage.metadata_db import MetadataDb
        from test_metadata_db_segments import _make_segment  # type: ignore[import-not-found]

        recording = self.session_dir / "recording"
        seg_path = recording / "game_001" / "ndi_main" / "segment_00000.mkv"
        seg_path.parent.mkdir(parents=True, exist_ok=True)
        seg_path.write_bytes(b"")

        db = MetadataDb(self.db_path)
        try:
            db.create_session(
                session_id="session_001",
                source_name="Test",
                started_at="2026-04-28T00:00:00+00:00",
            )
            db.insert_segment(_make_segment(
                fragment_index=0, file_path=str(seg_path)
            ))
        finally:
            db.close()

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_dry_run_returns_zero_without_invoking_ffmpeg(self) -> None:
        from app.tools.post_session_processor import main
        with mock.patch(
            "app.tools.long_form_export.subprocess.run"
        ) as run:
            rc = main([
                str(self.session_dir),
                "--metadata-db", str(self.db_path),
                "--dry-run",
            ])
        run.assert_not_called()
        self.assertEqual(rc, 0)

    def test_missing_ffmpeg_returns_two(self) -> None:
        from app.tools.post_session_processor import main
        with mock.patch(
            "app.tools.long_form_export.shutil.which", return_value=None
        ):
            rc = main([
                str(self.session_dir),
                "--metadata-db", str(self.db_path),
            ])
        self.assertEqual(rc, 2)

    def test_main_returns_zero_when_all_succeed(self) -> None:
        from app.tools.post_session_processor import main
        with TemporaryDirectory() as fakebin_dir:
            fake_ffmpeg = Path(fakebin_dir) / "ffmpeg.exe"
            fake_ffmpeg.write_bytes(b"")

            def fake_run(args, capture_output, text, check):
                # Write a dummy output file so the size lookup succeeds.
                Path(args[-1]).write_bytes(b"\0" * 256)
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )

            with mock.patch(
                "app.tools.long_form_export.subprocess.run", fake_run
            ):
                rc = main([
                    str(self.session_dir),
                    "--metadata-db", str(self.db_path),
                    "--ffmpeg-path", str(fake_ffmpeg),
                ])
        self.assertEqual(rc, 0)

    def test_main_returns_one_when_some_fail(self) -> None:
        from app.tools.post_session_processor import main
        with TemporaryDirectory() as fakebin_dir:
            fake_ffmpeg = Path(fakebin_dir) / "ffmpeg.exe"
            fake_ffmpeg.write_bytes(b"")

            def fake_run(args, capture_output, text, check):
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr="encoding broke"
                )

            with mock.patch(
                "app.tools.long_form_export.subprocess.run", fake_run
            ):
                rc = main([
                    str(self.session_dir),
                    "--metadata-db", str(self.db_path),
                    "--ffmpeg-path", str(fake_ffmpeg),
                ])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
