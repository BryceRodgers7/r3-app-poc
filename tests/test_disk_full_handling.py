"""Phase 10.C — disk-full enforcement.

Three layers covered here:

1. `evaluate_disk_preflight` — pure function over a fake `disk_usage_fn`.
2. `_evaluate_disk_health` — two-tier disk_low / disk_critical logic
   driven through `TelemetryHub`'s sampler with a controllable snapshot.
3. `_is_enospc_error` — ENOSPC classification on the bus-error path,
   exercised against fake GLib.Error stand-ins.

Pre-flight wired into `ApplicationCoordinator.toggle_long_session_recording`
is covered indirectly by an integration-style test that constructs a
coordinator with a fake `disk_usage_fn`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import unittest
from unittest import mock

from app.config.settings import AppSettings
from app.core.disk_budget import (
    PreflightResult,
    evaluate_disk_preflight,
)
from app.core.health_events import HealthEventLog, HealthSeverity
import app.core.health_events as health_events
from app.core.models import FeedDefinition
from app.core.telemetry import (
    DISK_CRITICAL_FRACTION,
    DISK_CRITICAL_MIN_FREE_BYTES,
    DISK_LOW_FRACTION,
    DiskSnapshot,
    TelemetryHub,
)
from app.media.pipeline_manager import _is_enospc_error


@dataclass
class FakeUsage:
    free: int
    total: int = 1_000_000_000_000  # 1 TB by default


class _TestLogPatch:
    def __init__(self) -> None:
        self.log = HealthEventLog()
        self._saved = None

    def __enter__(self) -> HealthEventLog:
        self._saved = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = self.log
        return self.log

    def __exit__(self, *exc) -> None:
        health_events._DEFAULT_LOG = self._saved


def _settings(grace_seconds: float = 60.0, target_fps: float = 30.0) -> AppSettings:
    s = AppSettings()
    s.target_frame_width = 1280
    s.target_frame_height = 720
    s.target_fps = target_fps
    s.recording_codec = "mjpeg"
    s.disk_full_grace_seconds = grace_seconds
    return s


def _feeds(n: int) -> list[FeedDefinition]:
    return [
        FeedDefinition(
            feed_id=f"cam_{i}",
            display_name=f"Cam {i}",
            source_kind="ndi",
            enabled=True,
        )
        for i in range(n)
    ]


class EvaluateDiskPreflightTests(unittest.TestCase):
    def test_sufficient_returns_true(self) -> None:
        # 2 feeds × 1280×720×30fps × 0.10 ratio × 3 bytes ≈ 8.3 MB/s/feed
        # × 60s = ~995 MB needed. Provide 5 GB free.
        result = evaluate_disk_preflight(
            Path("."),
            _feeds(2),
            _settings(),
            disk_usage_fn=lambda _: FakeUsage(free=5 * 1024 * 1024 * 1024),
        )
        self.assertTrue(result.sufficient)
        self.assertGreater(result.required_bytes, 0)

    def test_insufficient_returns_false(self) -> None:
        # Same setup, 100 MB free — under the ~995 MB requirement.
        result = evaluate_disk_preflight(
            Path("."),
            _feeds(2),
            _settings(),
            disk_usage_fn=lambda _: FakeUsage(free=100 * 1024 * 1024),
        )
        self.assertFalse(result.sufficient)
        self.assertEqual(result.free_bytes, 100 * 1024 * 1024)

    def test_zero_feeds_required_bytes_is_zero(self) -> None:
        result = evaluate_disk_preflight(
            Path("."),
            [],
            _settings(),
            disk_usage_fn=lambda _: FakeUsage(free=0),
        )
        self.assertEqual(result.required_bytes, 0)
        # Free bytes (0) >= required bytes (0) → sufficient.
        self.assertTrue(result.sufficient)

    def test_grace_seconds_zero_required_bytes_is_zero(self) -> None:
        result = evaluate_disk_preflight(
            Path("."),
            _feeds(4),
            _settings(grace_seconds=0.0),
            disk_usage_fn=lambda _: FakeUsage(free=0),
        )
        self.assertEqual(result.required_bytes, 0)
        self.assertTrue(result.sufficient)

    def test_probe_failure_refuses_start(self) -> None:
        def boom(_):
            raise OSError("removable volume detached")

        result = evaluate_disk_preflight(
            Path("."), _feeds(1), _settings(), disk_usage_fn=boom
        )
        self.assertFalse(result.sufficient)
        self.assertEqual(result.free_bytes, 0)


class TwoTierDiskHealthTests(unittest.TestCase):
    """Drive `_evaluate_disk_health` directly with synthesized snapshots."""

    def setUp(self) -> None:
        self._patch = _TestLogPatch()
        self.log = self._patch.__enter__()
        self.hub = TelemetryHub(health_log=self.log)

    def tearDown(self) -> None:
        self._patch.__exit__(None, None, None)

    def _snap(self, free_fraction: float, total: int = 1_000_000_000_000) -> DiskSnapshot:
        free = int(total * free_fraction)
        return DiskSnapshot(
            path="C:\\test", available=True, free_bytes=free, total_bytes=total
        )

    def test_above_low_threshold_no_events(self) -> None:
        self.hub._evaluate_disk_health(self._snap(0.5))
        self.assertFalse(self.log.has_open_event(category="disk_low", feed_id=None))
        self.assertFalse(
            self.log.has_open_event(category="disk_critical", feed_id=None)
        )

    def test_low_only_below_low_above_critical(self) -> None:
        # 4% free: < DISK_LOW_FRACTION (5%), > DISK_CRITICAL_FRACTION (2%)
        # on a 1TB disk free_bytes = 40 GB > 1 GB floor.
        self.hub._evaluate_disk_health(self._snap(0.04))
        self.assertTrue(self.log.has_open_event(category="disk_low", feed_id=None))
        self.assertFalse(
            self.log.has_open_event(category="disk_critical", feed_id=None)
        )

    def test_critical_below_critical_fraction(self) -> None:
        # 1% free, on 1TB → 10 GB. Critical fraction triggers.
        self.hub._evaluate_disk_health(self._snap(0.01))
        self.assertTrue(self.log.has_open_event(category="disk_low", feed_id=None))
        self.assertTrue(
            self.log.has_open_event(category="disk_critical", feed_id=None)
        )

    def test_critical_byte_floor_triggers_on_huge_disk(self) -> None:
        # 100 TB disk, 0.5 GB free — 0.0005% free. Below byte floor
        # (1 GB) AND below critical fraction.
        snap = DiskSnapshot(
            path="X:",
            available=True,
            free_bytes=500 * 1024 * 1024,
            total_bytes=100 * 1024 ** 4,
        )
        self.hub._evaluate_disk_health(snap)
        self.assertTrue(
            self.log.has_open_event(category="disk_critical", feed_id=None)
        )

    def test_recovery_clears_critical_then_low(self) -> None:
        self.hub._evaluate_disk_health(self._snap(0.01))  # critical
        self.assertTrue(
            self.log.has_open_event(category="disk_critical", feed_id=None)
        )
        # Free space recovers to between thresholds: critical clears,
        # low stays open.
        self.hub._evaluate_disk_health(self._snap(0.04))
        self.assertFalse(
            self.log.has_open_event(category="disk_critical", feed_id=None)
        )
        self.assertTrue(self.log.has_open_event(category="disk_low", feed_id=None))
        # And then back above low: both gone.
        self.hub._evaluate_disk_health(self._snap(0.20))
        self.assertFalse(self.log.has_open_event(category="disk_low", feed_id=None))

    def test_critical_event_severity_is_error(self) -> None:
        self.hub._evaluate_disk_health(self._snap(0.01))
        events = [
            e
            for e in self.log.open_events()
            if e.category == "disk_critical"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].severity, HealthSeverity.ERROR.value)


class IsEnospcErrorTests(unittest.TestCase):
    def test_no_space_left_in_message(self) -> None:
        error = mock.Mock()
        error.__str__ = lambda self: "Could not write to file"
        error.matches = lambda *args, **kwargs: False
        # Bypass the matches-based path by making it not match; debug
        # contains the ENOSPC clue.
        Gst = mock.Mock()
        Gst.ResourceError.quark.return_value = "rq"
        Gst.ResourceError.NO_SPACE_LEFT = 28
        debug = "GstFileSink: No space left on device"
        self.assertTrue(_is_enospc_error(error, debug, Gst))

    def test_enospc_text_in_message(self) -> None:
        error = mock.Mock()
        error.__str__ = lambda self: "ENOSPC writing"
        error.matches = lambda *args, **kwargs: False
        Gst = mock.Mock()
        self.assertTrue(_is_enospc_error(error, "", Gst))

    def test_unrelated_error_is_not_enospc(self) -> None:
        error = mock.Mock()
        error.__str__ = lambda self: "Internal data flow error"
        error.matches = lambda *args, **kwargs: False
        Gst = mock.Mock()
        self.assertFalse(_is_enospc_error(error, "more details", Gst))

    def test_glib_quark_match_path(self) -> None:
        # Simulate the well-typed path: error.matches() returns True.
        error = mock.Mock()
        error.matches = lambda quark, code: True
        Gst = mock.Mock()
        Gst.ResourceError.quark.return_value = "rq"
        Gst.ResourceError.NO_SPACE_LEFT = 28
        # Even with a non-ENOSPC text, the typed path wins.
        self.assertTrue(_is_enospc_error(error, "no debug", Gst))

    def test_glib_quark_path_exception_falls_back_to_text(self) -> None:
        error = mock.Mock()
        error.__str__ = lambda self: "no space left on device"
        # matches raises — common when Gst stub differs across bindings.
        def boom(*a, **k):
            raise RuntimeError("no matches method")
        error.matches = boom
        Gst = mock.Mock()
        Gst.ResourceError.quark.side_effect = RuntimeError
        self.assertTrue(_is_enospc_error(error, "", Gst))


class CoordinatorPreflightIntegrationTests(unittest.TestCase):
    """End-to-end: refusing Start through the coordinator's
    `toggle_long_session_recording`."""

    def _build_minimal_coordinator(self, free_bytes: int):
        from app.core.application_coordinator import ApplicationCoordinator

        coord = ApplicationCoordinator.__new__(ApplicationCoordinator)
        coord._settings = _settings()
        coord._feed_runtimes = {
            "cam_a": mock.Mock(feed=_feeds(1)[0]),
            "cam_b": mock.Mock(feed=_feeds(2)[1]),
        }
        coord._disk_usage_fn = lambda _: FakeUsage(free=free_bytes)
        coord.operator_controller = mock.Mock()
        return coord

    def test_pre_flight_refuses_start_with_low_free_space(self) -> None:
        with _TestLogPatch() as log:
            coord = self._build_minimal_coordinator(free_bytes=10 * 1024 * 1024)
            recording_dir = Path("C:\\fake\\session\\recording")
            ok = coord._check_disk_preflight(recording_dir)
            self.assertFalse(ok)
            self.assertTrue(
                log.has_open_event(category="disk_full_blocked", feed_id=None)
            )
            coord.operator_controller.signals.status_message.emit.assert_called_once()

    def test_pre_flight_passes_with_ample_free_space(self) -> None:
        with _TestLogPatch() as log:
            coord = self._build_minimal_coordinator(
                free_bytes=10 * 1024 * 1024 * 1024  # 10 GB
            )
            ok = coord._check_disk_preflight(Path("C:\\anywhere"))
            self.assertTrue(ok)
            self.assertFalse(
                log.has_open_event(category="disk_full_blocked", feed_id=None)
            )

    def test_clear_marker_drops_open_event(self) -> None:
        with _TestLogPatch() as log:
            coord = self._build_minimal_coordinator(free_bytes=10 * 1024 * 1024)
            coord._check_disk_preflight(Path("C:\\anywhere"))
            self.assertTrue(
                log.has_open_event(category="disk_full_blocked", feed_id=None)
            )
            coord._clear_disk_full_blocked_marker()
            self.assertFalse(
                log.has_open_event(category="disk_full_blocked", feed_id=None)
            )


class AlertBannerCategoryWiringTests(unittest.TestCase):
    """Lock in that the new 10.C categories are operator-visible."""

    def test_disk_full_blocked_in_allowlist(self) -> None:
        from app.ui.alert_banner import _OPERATOR_VISIBLE_CATEGORIES
        self.assertIn("disk_full_blocked", _OPERATOR_VISIBLE_CATEGORIES)

    def test_disk_full_during_record_in_allowlist(self) -> None:
        from app.ui.alert_banner import _OPERATOR_VISIBLE_CATEGORIES
        self.assertIn("disk_full_during_record", _OPERATOR_VISIBLE_CATEGORIES)

    def test_disk_critical_in_allowlist(self) -> None:
        from app.ui.alert_banner import _OPERATOR_VISIBLE_CATEGORIES
        self.assertIn("disk_critical", _OPERATOR_VISIBLE_CATEGORIES)


class SettingsLoadingTests(unittest.TestCase):
    def test_default_is_60_seconds(self) -> None:
        s = AppSettings()
        self.assertEqual(s.disk_full_grace_seconds, 60.0)

    def test_loads_from_toml(self) -> None:
        import tempfile
        import textwrap

        toml_text = textwrap.dedent(
            """
            [recording]
            disk_full_grace_seconds = 120.0
            """
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(toml_text)
            path = Path(fh.name)
        try:
            settings = AppSettings.load(path)
            self.assertEqual(settings.disk_full_grace_seconds, 120.0)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
