"""Tests for Phase 7.A disk-budget validation."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config.settings import AppSettings
from app.core.disk_budget import (
    BudgetVerdict,
    DiskBudgetAssessment,
    assess_disk_budget,
    estimate_per_feed_mb_s,
    estimate_total_mb_s,
    validate_budget,
)
from app.core.health_events import HealthEventLog, HealthSeverity
from app.core.models import FeedDefinition


class EstimatePerFeedTests(unittest.TestCase):
    """Per-feed bitrate fixtures.

    The coefficient is the compression ratio against raw 24-bit RGB,
    so expected bytes/sec is `width * height * fps * 3 * ratio`. For
    MJPEG that's a 10:1 ratio (~0.10). These fixtures lock the math
    in as a regression check on the coefficient table.
    """

    def test_720p30_mjpeg(self) -> None:
        # 1280 * 720 * 30 * 3 * 0.10 = 8,294,400 bytes/s = ~7.91 MB/s
        mb_s = estimate_per_feed_mb_s(1280, 720, 30.0, "mjpeg")
        self.assertAlmostEqual(mb_s, 8_294_400.0 / (1024 * 1024), places=3)

    def test_1080p30_mjpeg(self) -> None:
        # 1920 * 1080 * 30 * 3 * 0.10 = 18,662,400 bytes/s = ~17.80 MB/s
        mb_s = estimate_per_feed_mb_s(1920, 1080, 30.0, "mjpeg")
        self.assertAlmostEqual(mb_s, 18_662_400.0 / (1024 * 1024), places=3)

    def test_codec_normalization(self) -> None:
        same = estimate_per_feed_mb_s(1280, 720, 30.0, "MJPEG")
        baseline = estimate_per_feed_mb_s(1280, 720, 30.0, "mjpeg")
        self.assertAlmostEqual(same, baseline, places=6)

    def test_zero_fps_yields_zero(self) -> None:
        self.assertEqual(estimate_per_feed_mb_s(1280, 720, 0.0, "mjpeg"), 0.0)

    def test_unsupported_codec_raises(self) -> None:
        with self.assertRaises(NotImplementedError):
            estimate_per_feed_mb_s(1280, 720, 30.0, "h264")


class EstimateTotalTests(unittest.TestCase):
    def _settings(self) -> AppSettings:
        s = AppSettings()
        s.target_frame_width = 1280
        s.target_frame_height = 720
        s.target_fps = 30.0
        s.recording_codec = "mjpeg"
        return s

    def _feed(self, feed_id: str) -> FeedDefinition:
        return FeedDefinition(feed_id=feed_id, display_name=feed_id)

    def test_total_scales_linearly_with_feed_count(self) -> None:
        settings = self._settings()
        per_feed = estimate_per_feed_mb_s(1280, 720, 30.0, "mjpeg")
        for n in (1, 2, 4, 8):
            feeds = [self._feed(f"feed_{i}") for i in range(n)]
            total = estimate_total_mb_s(feeds, settings)
            self.assertAlmostEqual(total, per_feed * n, places=6)

    def test_zero_feeds_yields_zero(self) -> None:
        self.assertEqual(estimate_total_mb_s([], self._settings()), 0.0)


class ValidateBudgetTests(unittest.TestCase):
    """Threshold matrix: < 80% OK, ≥ 80% WARN, ≥ 100% OVER_BUDGET."""

    def test_ok_below_warn_threshold(self) -> None:
        # 79% of 200 = 158 MB/s
        self.assertEqual(validate_budget(158.0, 200.0), BudgetVerdict.OK)

    def test_warn_at_exactly_80_percent(self) -> None:
        self.assertEqual(validate_budget(160.0, 200.0), BudgetVerdict.WARN)

    def test_warn_between_80_and_100(self) -> None:
        self.assertEqual(validate_budget(180.0, 200.0), BudgetVerdict.WARN)

    def test_over_at_exactly_100_percent(self) -> None:
        self.assertEqual(validate_budget(200.0, 200.0), BudgetVerdict.OVER_BUDGET)

    def test_over_above_100_percent(self) -> None:
        self.assertEqual(validate_budget(250.0, 200.0), BudgetVerdict.OVER_BUDGET)

    def test_zero_or_negative_budget_treated_as_over(self) -> None:
        # A misconfigured budget should surface, not silently pass.
        self.assertEqual(validate_budget(10.0, 0.0), BudgetVerdict.OVER_BUDGET)
        self.assertEqual(validate_budget(10.0, -1.0), BudgetVerdict.OVER_BUDGET)


class AssessDiskBudgetTests(unittest.TestCase):
    def _settings(self, *, budget: float = 200.0) -> AppSettings:
        s = AppSettings()
        s.target_frame_width = 1280
        s.target_frame_height = 720
        s.target_fps = 30.0
        s.recording_codec = "mjpeg"
        s.disk_budget_mb_s = budget
        return s

    def test_one_feed_720p30_is_ok(self) -> None:
        settings = self._settings()
        feeds = [FeedDefinition(feed_id="f1", display_name="f1")]
        a = assess_disk_budget(feeds, settings)
        self.assertEqual(a.verdict, BudgetVerdict.OK)
        self.assertEqual(a.feed_count, 1)
        self.assertEqual(a.budget_mb_s, 200.0)

    def test_warn_at_tight_budget(self) -> None:
        # 4 feeds @ 720p30 ≈ 31.6 MB/s. Budget 35 → ratio ~90% → WARN.
        settings = self._settings(budget=35.0)
        feeds = [FeedDefinition(feed_id=f"f{i}", display_name=f"f{i}") for i in range(4)]
        a = assess_disk_budget(feeds, settings)
        self.assertEqual(a.verdict, BudgetVerdict.WARN)

    def test_over_at_tight_budget(self) -> None:
        # 4 feeds @ 1080p30 ≈ 71.2 MB/s. Budget 70 → ratio > 100% → OVER.
        settings = self._settings(budget=70.0)
        settings.target_frame_width = 1920
        settings.target_frame_height = 1080
        feeds = [FeedDefinition(feed_id=f"f{i}", display_name=f"f{i}") for i in range(4)]
        a = assess_disk_budget(feeds, settings)
        self.assertEqual(a.verdict, BudgetVerdict.OVER_BUDGET)


class DiskBudgetTomlParsingTests(unittest.TestCase):
    def test_default_when_omitted(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "app_settings.toml"
            path.write_text("[recording]\nenabled = true\n", encoding="utf-8")
            s = AppSettings.load(path)
        self.assertEqual(s.disk_budget_mb_s, 200.0)

    def test_override_from_toml(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "app_settings.toml"
            path.write_text(
                """
[recording]
disk_budget_mb_s = 450.0
""".strip(),
                encoding="utf-8",
            )
            s = AppSettings.load(path)
        self.assertEqual(s.disk_budget_mb_s, 450.0)


class DiskBudgetHealthEventEmissionTests(unittest.TestCase):
    """The coordinator emits a `disk_budget_warn` / `disk_budget_over`
    event during `initialize()`. We test the helper directly with a
    stubbed coordinator + a private health log to avoid spinning up
    the full app graph."""

    def _coordinator_with_assessment(
        self, assessment: DiskBudgetAssessment, log: HealthEventLog
    ) -> object:
        from app.core.application_coordinator import ApplicationCoordinator

        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord.disk_budget = assessment
        # Patch the helper to use our log instance instead of the
        # process-wide default. The helper calls `default_health_log()`
        # internally — we monkey-patch the module reference.
        import app.core.application_coordinator as coord_mod
        coord_mod.default_health_log = lambda: log  # type: ignore[assignment]
        return coord

    def setUp(self) -> None:
        # Reset the default health log import each test so monkey-
        # patching doesn't bleed across cases.
        import app.core.application_coordinator as coord_mod
        from app.core.health_events import default_log as real_default
        self._real_default = real_default
        self._coord_mod = coord_mod

    def tearDown(self) -> None:
        self._coord_mod.default_health_log = self._real_default  # type: ignore[assignment]

    def test_ok_emits_no_event(self) -> None:
        log = HealthEventLog()
        assessment = DiskBudgetAssessment(
            estimated_mb_s=50.0, budget_mb_s=200.0, feed_count=1, verdict=BudgetVerdict.OK
        )
        coord = self._coordinator_with_assessment(assessment, log)
        coord._emit_disk_budget_health_event()
        self.assertEqual(log.category_count("disk_budget_warn"), 0)
        self.assertEqual(log.category_count("disk_budget_over"), 0)

    def test_warn_emits_disk_budget_warn(self) -> None:
        log = HealthEventLog()
        assessment = DiskBudgetAssessment(
            estimated_mb_s=180.0, budget_mb_s=200.0, feed_count=3, verdict=BudgetVerdict.WARN
        )
        coord = self._coordinator_with_assessment(assessment, log)
        coord._emit_disk_budget_health_event()
        self.assertEqual(log.category_count("disk_budget_warn"), 1)
        self.assertEqual(log.category_count("disk_budget_over"), 0)

    def test_over_emits_disk_budget_over(self) -> None:
        log = HealthEventLog()
        assessment = DiskBudgetAssessment(
            estimated_mb_s=240.0,
            budget_mb_s=200.0,
            feed_count=4,
            verdict=BudgetVerdict.OVER_BUDGET,
        )
        coord = self._coordinator_with_assessment(assessment, log)
        coord._emit_disk_budget_health_event()
        self.assertEqual(log.category_count("disk_budget_warn"), 0)
        self.assertEqual(log.category_count("disk_budget_over"), 1)

    def test_none_assessment_emits_nothing(self) -> None:
        # Older test paths construct the coordinator without budget
        # validation; the helper must no-op rather than raise.
        log = HealthEventLog()
        coord = self._coordinator_with_assessment(None, log)  # type: ignore[arg-type]
        coord._emit_disk_budget_health_event()
        self.assertEqual(log.category_count("disk_budget_warn"), 0)
        self.assertEqual(log.category_count("disk_budget_over"), 0)


if __name__ == "__main__":
    unittest.main()
