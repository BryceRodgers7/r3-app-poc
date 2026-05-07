"""Phase 11.D — perf acceptance harness tests.

Cover the testable surface of `tools/perf_acceptance.py`:
  * `Profile` / `FeedStats` JSON round-trip
  * `evaluate_pass_fail` against synthesized stats hitting each §16.3 rule
  * `enforce_retention` prunes oldest artifacts when over the cap
  * `compute_feed_stats` aggregation (p50/p95/min)
  * `_percentile` edge cases
  * CLI argparse smoke-mode override
  * `profile_filename` filesystem-safe formatting
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.perf_acceptance import (
    DISALLOWED_HEALTH_CATEGORIES,
    QUEUE_SATURATION_MAX_PCT,
    FeedStats,
    Profile,
    PROFILE_SCHEMA_VERSION,
    _percentile,
    build_arg_parser,
    compute_feed_stats,
    enforce_retention,
    evaluate_pass_fail,
    find_latest_profile,
    profile_filename,
)


def _make_feed_stats(
    *,
    feed_id: str = "perf_0",
    source_fps_p50: float = 30.0,
    recording_fps_p50: float = 30.0,
    preview_sat: float = 10.0,
    recording_sat: float = 20.0,
) -> FeedStats:
    return FeedStats(
        feed_id=feed_id,
        display_name=f"Perf Feed {feed_id}",
        pipeline_mode="python_push",
        recording_encoder="jpegenc",
        sample_count=10,
        source_fps_p50=source_fps_p50,
        source_fps_p95=source_fps_p50,
        source_fps_min=source_fps_p50,
        recording_fps_p50=recording_fps_p50,
        recording_fps_p95=recording_fps_p50,
        recording_fps_min=recording_fps_p50,
        dropped_per_sec_max=0.0,
        queue_saturation_preview_max_pct=preview_sat,
        queue_saturation_recording_max_pct=recording_sat,
    )


def _make_profile(*, feeds: list[FeedStats], events: list[dict] | None = None) -> Profile:
    return Profile(
        schema_version=PROFILE_SCHEMA_VERSION,
        hostname="testhost",
        utc_iso="2026-05-06T12:34:56Z",
        feed_count=len(feeds),
        resolution="1280x720",
        target_fps=30.0,
        duration_seconds=30.0,
        warmup_seconds=3.0,
        disk_budget_mb_s=200.0,
        disk_write_rate_p95_mb_s=42.5,
        feeds=feeds,
        health_events=events or [],
        passed=True,
        failures=[],
    )


class PercentileTests(unittest.TestCase):
    def test_empty_list_returns_zero(self) -> None:
        self.assertEqual(_percentile([], 50), 0.0)

    def test_single_value_returns_value(self) -> None:
        self.assertEqual(_percentile([7.0], 50), 7.0)
        self.assertEqual(_percentile([7.0], 95), 7.0)

    def test_p50_is_median(self) -> None:
        self.assertEqual(_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50), 3.0)

    def test_p95_picks_high_value(self) -> None:
        # Nearest-rank: 95th of 20 items is rank 19 -> index 18 (sorted).
        values = [float(i) for i in range(20)]
        self.assertEqual(_percentile(values, 95), 18.0)

    def test_p0_and_p100_clamp(self) -> None:
        self.assertEqual(_percentile([2.0, 5.0, 9.0], 0), 2.0)
        self.assertEqual(_percentile([2.0, 5.0, 9.0], 100), 9.0)


class FeedStatsAggregationTests(unittest.TestCase):
    def test_aggregates_min_p50_p95_max(self) -> None:
        source_fps = [29.0, 30.0, 30.0, 30.0, 31.0]
        recording_fps = [28.0, 29.0, 30.0, 30.0, 30.0]
        dropped = [0.0, 0.0, 1.0, 0.0]
        prev_sat = [10.0, 20.0, 30.0]
        rec_sat = [5.0, 80.0, 70.0]
        stats = compute_feed_stats(
            feed_id="perf_0",
            display_name="Perf Feed 0",
            pipeline_mode="python_push",
            recording_encoder="jpegenc",
            source_fps=source_fps,
            recording_fps=recording_fps,
            dropped_per_sec=dropped,
            preview_saturation_pct=prev_sat,
            recording_saturation_pct=rec_sat,
        )
        self.assertEqual(stats.sample_count, 5)
        self.assertEqual(stats.source_fps_min, 29.0)
        self.assertEqual(stats.source_fps_p50, 30.0)
        self.assertEqual(stats.recording_fps_min, 28.0)
        self.assertEqual(stats.dropped_per_sec_max, 1.0)
        self.assertEqual(stats.queue_saturation_preview_max_pct, 30.0)
        self.assertEqual(stats.queue_saturation_recording_max_pct, 80.0)


class ProfileRoundTripTests(unittest.TestCase):
    def test_to_dict_from_dict_roundtrip(self) -> None:
        feeds = [_make_feed_stats(feed_id="perf_0"), _make_feed_stats(feed_id="perf_1")]
        events = [{"category": "feed_lost", "message": "ndi gone", "feed_id": "perf_0"}]
        original = _make_profile(feeds=feeds, events=events)
        original.passed = False
        original.failures = ["sample failure"]

        as_dict = original.to_dict()
        as_json = json.loads(json.dumps(as_dict))
        restored = Profile.from_dict(as_json)

        self.assertEqual(restored.schema_version, original.schema_version)
        self.assertEqual(restored.hostname, original.hostname)
        self.assertEqual(restored.feed_count, original.feed_count)
        self.assertEqual(restored.passed, original.passed)
        self.assertEqual(restored.failures, original.failures)
        self.assertEqual(len(restored.feeds), 2)
        self.assertEqual(restored.feeds[0].feed_id, "perf_0")
        self.assertEqual(restored.health_events, events)


class EvaluatePassFailTests(unittest.TestCase):
    def test_all_within_budget_passes(self) -> None:
        feeds = [_make_feed_stats()]
        passed, failures = evaluate_pass_fail(feeds, [], target_fps=30.0)
        self.assertTrue(passed)
        self.assertEqual(failures, [])

    def test_empty_feeds_fails(self) -> None:
        passed, failures = evaluate_pass_fail([], [], target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(any("no feeds" in f for f in failures))

    def test_source_fps_below_target_minus_one_percent_fails(self) -> None:
        # 29.6 < 30 * 0.99 = 29.7
        feeds = [_make_feed_stats(source_fps_p50=29.6, recording_fps_p50=29.6)]
        passed, failures = evaluate_pass_fail(feeds, [], target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(any("source_fps_p50" in f for f in failures))

    def test_source_fps_just_within_tolerance_passes(self) -> None:
        # 29.7 == 30 * 0.99
        feeds = [_make_feed_stats(source_fps_p50=29.7, recording_fps_p50=29.7)]
        passed, _ = evaluate_pass_fail(feeds, [], target_fps=30.0)
        self.assertTrue(passed)

    def test_recording_fps_below_source_fails(self) -> None:
        # source 30; recording 29.6 < 30 * 0.99 = 29.7
        feeds = [_make_feed_stats(source_fps_p50=30.0, recording_fps_p50=29.6)]
        passed, failures = evaluate_pass_fail(feeds, [], target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(any("recording_fps_p50" in f for f in failures))

    def test_preview_queue_saturation_over_75_fails(self) -> None:
        feeds = [_make_feed_stats(preview_sat=QUEUE_SATURATION_MAX_PCT + 0.1)]
        passed, failures = evaluate_pass_fail(feeds, [], target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(any("preview queue saturation" in f for f in failures))

    def test_recording_queue_saturation_over_75_fails(self) -> None:
        feeds = [_make_feed_stats(recording_sat=QUEUE_SATURATION_MAX_PCT + 0.1)]
        passed, failures = evaluate_pass_fail(feeds, [], target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(any("recording queue saturation" in f for f in failures))

    def test_recording_branch_saturated_event_fails(self) -> None:
        feeds = [_make_feed_stats()]
        events = [
            {
                "category": "recording_branch_saturated",
                "message": "queue full",
                "feed_id": "perf_0",
            }
        ]
        passed, failures = evaluate_pass_fail(feeds, events, target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(
            any("recording_branch_saturated" in f for f in failures)
        )

    def test_disk_full_event_fails(self) -> None:
        feeds = [_make_feed_stats()]
        events = [
            {"category": "disk_full", "message": "ENOSPC", "feed_id": None}
        ]
        passed, failures = evaluate_pass_fail(feeds, events, target_fps=30.0)
        self.assertFalse(passed)
        self.assertTrue(any("disk_full" in f for f in failures))

    def test_unrelated_health_events_dont_fail(self) -> None:
        feeds = [_make_feed_stats()]
        events = [
            {"category": "feed_lost", "message": "x", "feed_id": "perf_0"},
            {"category": "disk_low", "message": "x", "feed_id": None},
            {"category": "audio_missing", "message": "x", "feed_id": "perf_0"},
        ]
        passed, _ = evaluate_pass_fail(feeds, events, target_fps=30.0)
        self.assertTrue(passed)

    def test_disallowed_categories_constant_matches_evaluator(self) -> None:
        # Defensive: if the spec adds a new disallowed category, the
        # tests above should be extended.
        self.assertEqual(
            set(DISALLOWED_HEALTH_CATEGORIES),
            {"recording_branch_saturated", "disk_full", "disk_full_imminent"},
        )


class RetentionTests(unittest.TestCase):
    def _write(self, profile_dir: Path, names: list[str]) -> list[Path]:
        profile_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in names:
            p = profile_dir / name
            p.write_text("{}", encoding="utf-8")
            paths.append(p)
        return paths

    def test_keeps_all_when_under_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, ["a.json", "b.json"])
            removed = enforce_retention(d, max_count=5)
            self.assertEqual(removed, [])
            self.assertEqual(len(list(d.iterdir())), 2)

    def test_prunes_oldest_when_over_cap(self) -> None:
        # Filenames are alphabetically sorted; "001" is oldest.
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, [f"{i:03d}.json" for i in range(7)])
            removed = enforce_retention(d, max_count=3)
            removed_names = sorted(p.name for p in removed)
            self.assertEqual(removed_names, ["000.json", "001.json", "002.json", "003.json"])
            kept = sorted(p.name for p in d.iterdir())
            self.assertEqual(kept, ["004.json", "005.json", "006.json"])

    def test_zero_cap_removes_all(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, ["a.json", "b.json"])
            removed = enforce_retention(d, max_count=0)
            self.assertEqual(len(removed), 2)
            self.assertEqual(list(d.iterdir()), [])

    def test_negative_cap_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                enforce_retention(Path(tmp), max_count=-1)

    def test_missing_directory_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist"
            self.assertEqual(enforce_retention(missing, max_count=5), [])

    def test_ignores_non_json_files(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write(d, ["a.json"])
            (d / "readme.txt").write_text("x", encoding="utf-8")
            removed = enforce_retention(d, max_count=0)
            self.assertEqual(len(removed), 1)
            # readme.txt survives.
            self.assertTrue((d / "readme.txt").exists())


class FindLatestProfileTests(unittest.TestCase):
    def test_returns_alphabetically_last(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "001.json").write_text("{}", encoding="utf-8")
            (d / "002.json").write_text("{}", encoding="utf-8")
            (d / "000.json").write_text("{}", encoding="utf-8")
            latest = find_latest_profile(d)
            self.assertIsNotNone(latest)
            assert latest is not None  # for type-checker
            self.assertEqual(latest.name, "002.json")

    def test_returns_none_when_empty_or_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(find_latest_profile(d))
            self.assertIsNone(find_latest_profile(d / "missing"))


class ProfileFilenameTests(unittest.TestCase):
    def test_replaces_colons_for_windows_safety(self) -> None:
        name = profile_filename("2026-05-06T12:34:56Z", 2, "1280x720")
        self.assertNotIn(":", name)
        self.assertTrue(name.endswith("_2x1280x720.json"))


class CliTests(unittest.TestCase):
    def test_smoke_overrides_feeds_and_duration(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--smoke"])
        # The CLI applies smoke overrides in main(), but the parser
        # itself just toggles the flag — verify the flag is set so
        # downstream main() can act on it.
        self.assertTrue(args.smoke)

    def test_default_resolution_is_720p(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        self.assertEqual(args.resolution, (1280, 720))

    def test_resolution_parses_widthxheight(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--resolution", "1920x1080"])
        self.assertEqual(args.resolution, (1920, 1080))

    def test_invalid_resolution_rejected(self) -> None:
        parser = build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--resolution", "not-a-resolution"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--resolution", "0x720"])


if __name__ == "__main__":
    unittest.main()
