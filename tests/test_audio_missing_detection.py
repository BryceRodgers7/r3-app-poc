"""Phase 7.E: tests for the audio-missing health-event detection.

`_maybe_warn_audio_missing` runs from the video record-branch buffer
probe at video-buffer cadence. It emits a `category=audio_missing`
health event once when:

  - `recording_audio_enabled` is True AND
  - the audio chain is wired into splitmuxsink AND
  - no audio buffer has arrived within `AUDIO_MISSING_GRACE_SECONDS`
    of the pipeline going PLAYING.

These tests stub PipelineManager via `__new__` and call the helper
directly with various pre-conditions to lock the four no-fire paths
in addition to the fire path.
"""

from __future__ import annotations

import time
import unittest

from app.core.health_events import HealthEventLog
from app.media.pipeline_manager import PipelineManager


class _FakeSource:
    """Minimal stand-in for SourceInterface — only get_feed_id is read."""

    def __init__(self, feed_id: str = "feed_main") -> None:
        self._feed_id = feed_id

    def get_feed_id(self) -> str:
        return self._feed_id


def _build_pm_stub(
    *,
    recording_audio_enabled: bool = True,
    pipeline_started_ns: int | None = 0,
    audio_present_observed: bool = False,
    audio_missing_warned: bool = False,
) -> PipelineManager:
    """Build a PipelineManager with just the fields `_maybe_warn_audio_missing` reads."""
    pm = PipelineManager.__new__(PipelineManager)
    pm._recording_audio_enabled = recording_audio_enabled
    pm._pipeline_started_monotonic_ns = pipeline_started_ns
    pm._audio_present_observed = audio_present_observed
    # Phase 7.E field still exists for the encoder probe but is no
    # longer the gating signal — Phase 9.C moved presence detection
    # to the audio tee's sink pad (`_audio_present_observed`).
    pm._audio_record_first_buffer_at_ns = None
    pm._audio_missing_warned = audio_missing_warned
    pm._source = _FakeSource()
    return pm


class AudioMissingDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Use a private health log; monkey-patch the module reference
        # so `record_health_event` writes into our log instead of the
        # process-wide default. tearDown restores it.
        from app.core import health_events
        from app.media import pipeline_manager

        self._log = HealthEventLog()
        self._real_default_log = health_events._DEFAULT_LOG
        health_events._DEFAULT_LOG = self._log
        # `pipeline_manager` imported `record_health_event` at module
        # load time — that binding still points at the real function,
        # which calls `_DEFAULT_LOG.record(...)`. Swapping the module's
        # `_DEFAULT_LOG` is enough to redirect.
        self._pipeline_manager_module = pipeline_manager

    def tearDown(self) -> None:
        from app.core import health_events
        health_events._DEFAULT_LOG = self._real_default_log

    def test_fires_after_grace_with_no_audio_buffer(self) -> None:
        # Pipeline started 6s ago, grace is 5s — should fire.
        pipeline_started = time.monotonic_ns() - 6_000_000_000
        pm = _build_pm_stub(pipeline_started_ns=pipeline_started)
        pm._maybe_warn_audio_missing()
        self.assertEqual(self._log.category_count("audio_missing"), 1)
        self.assertTrue(pm._audio_missing_warned)

    def test_does_not_fire_inside_grace_window(self) -> None:
        # Pipeline started 2s ago, well inside the 5s grace.
        pipeline_started = time.monotonic_ns() - 2_000_000_000
        pm = _build_pm_stub(pipeline_started_ns=pipeline_started)
        pm._maybe_warn_audio_missing()
        self.assertEqual(self._log.category_count("audio_missing"), 0)
        self.assertFalse(pm._audio_missing_warned)

    def test_does_not_fire_when_audio_buffer_received(self) -> None:
        # Phase 9.C: presence is now signaled by `_audio_present_observed`
        # (set by the audio-tee buffer probe).
        pipeline_started = time.monotonic_ns() - 6_000_000_000
        pm = _build_pm_stub(
            pipeline_started_ns=pipeline_started,
            audio_present_observed=True,
        )
        pm._maybe_warn_audio_missing()
        self.assertEqual(self._log.category_count("audio_missing"), 0)

    def test_does_not_fire_when_recording_audio_disabled(self) -> None:
        # Operator opted out of audio in segments — silence is expected.
        pipeline_started = time.monotonic_ns() - 6_000_000_000
        pm = _build_pm_stub(
            recording_audio_enabled=False,
            pipeline_started_ns=pipeline_started,
        )
        pm._maybe_warn_audio_missing()
        self.assertEqual(self._log.category_count("audio_missing"), 0)

    def test_fires_only_once(self) -> None:
        pipeline_started = time.monotonic_ns() - 6_000_000_000
        pm = _build_pm_stub(pipeline_started_ns=pipeline_started)
        # Probe runs at video-buffer cadence — many invocations.
        for _ in range(50):
            pm._maybe_warn_audio_missing()
        self.assertEqual(self._log.category_count("audio_missing"), 1)

    def test_does_not_fire_before_pipeline_starts(self) -> None:
        # Pipeline hasn't gone PLAYING yet.
        pm = _build_pm_stub(pipeline_started_ns=None)
        pm._maybe_warn_audio_missing()
        self.assertEqual(self._log.category_count("audio_missing"), 0)


if __name__ == "__main__":
    unittest.main()
