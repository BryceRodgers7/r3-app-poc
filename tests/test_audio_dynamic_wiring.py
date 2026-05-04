"""Phase 9.C: dynamic audio-record-branch wiring tests.

The audio_record branch (the chain that feeds opus into splitmuxsink's
`audio_%u` request pad) is now built lazily — only when an audio buffer
has actually been observed flowing through the source's audio tee. This
eliminates the no-audio-source preview-stall foot-gun that Phase 7.E
could only WARN about.

These tests exercise `_ensure_audio_record_branch_built_locked` directly
on a stubbed `PipelineManager` so we can simulate the various
"observed/not observed" combinations without spinning up a real
GStreamer pipeline. End-to-end real-pipeline behavior is verified
manually against an NDI source.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.media.pipeline_manager import PipelineManager


def _build_pm(
    *,
    recording_audio_enabled: bool = True,
    audio_format=object(),
    audio_record_encoder=None,
    audio_present_observed: bool = False,
    splitmuxsink=object(),
    audio_record_permanently_disabled: bool = False,
) -> PipelineManager:
    pm = PipelineManager.__new__(PipelineManager)
    pm._recording_audio_enabled = recording_audio_enabled
    pm._audio_format = audio_format
    pm._audio_record_encoder = audio_record_encoder
    pm._audio_present_observed = audio_present_observed
    pm._splitmuxsink = splitmuxsink
    # Phase 11.B follow-up: latch that prevents the gate from
    # retrying audio chain build after a prior failure.
    pm._audio_record_permanently_disabled = audio_record_permanently_disabled
    return pm


class EnsureAudioRecordBranchBuiltTests(unittest.TestCase):
    """Each gating condition produces a no-op."""

    def test_no_op_when_recording_audio_disabled(self) -> None:
        # `[recording] audio_enabled = false` is the manual override.
        pm = _build_pm(
            recording_audio_enabled=False,
            audio_present_observed=True,
        )
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_not_called()

    def test_no_op_when_source_has_no_audio_capability(self) -> None:
        # Source's `_build_audio_path_locked` early-returned because
        # the source doesn't support embedded audio — `_audio_format`
        # stays None so no tee or chain exists to attach to.
        pm = _build_pm(
            audio_format=None,
            audio_present_observed=True,
        )
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_not_called()

    def test_no_op_when_branch_already_built(self) -> None:
        # Idempotency: an existing encoder means the branch was built
        # on a prior Start; the rebuild path's `_link_audio_encoder_…`
        # handles re-attachment, not this helper.
        pm = _build_pm(
            audio_record_encoder=object(),
            audio_present_observed=True,
        )
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_not_called()

    def test_no_op_when_audio_not_observed(self) -> None:
        # The source has audio capability AND `audio_enabled = true`
        # AND no encoder built yet — but no buffers have flowed.
        # Helper waits.
        pm = _build_pm(audio_present_observed=False)
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_not_called()

    def test_no_op_when_splitmuxsink_missing(self) -> None:
        # Defensive: rebuild path nulls out `_splitmuxsink` briefly
        # between teardown and new-instance attach. Helper must
        # handle the gap.
        pm = _build_pm(
            audio_present_observed=True,
            splitmuxsink=None,
        )
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_not_called()

    def test_builds_branch_when_all_conditions_met(self) -> None:
        # The happy path: audio observed, encoder not yet built,
        # splitmuxsink ready, audio_enabled, audio_format known.
        pm = _build_pm(audio_present_observed=True)
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_called_once()

    def test_construction_failure_logged_not_raised(self) -> None:
        # If GStreamer can't build the chain (e.g., opusenc missing),
        # `_add_audio_record_branch_to_splitmuxsink` raises. The
        # helper must catch — recording proceeds video-only.
        pm = _build_pm(audio_present_observed=True)
        with mock.patch.object(
            pm,
            "_add_audio_record_branch_to_splitmuxsink",
            side_effect=RuntimeError("opusenc missing"),
        ):
            # Must NOT raise.
            pm._ensure_audio_record_branch_built_locked()

    def test_no_op_when_permanently_disabled_after_failure(self) -> None:
        # Phase 11.B follow-up: once the audio chain build has failed
        # once (e.g. qtmux refusing late audio_%u), the latch keeps
        # subsequent rebuilds from retrying. This avoids the failure
        # mode where game 2's fresh qtmux DOES accept the audio pad
        # but the resulting audio+video interleaving overwhelms the
        # ProRes encoder pipeline within seconds.
        pm = _build_pm(
            audio_present_observed=True,
            audio_record_permanently_disabled=True,
        )
        with mock.patch.object(
            pm, "_add_audio_record_branch_to_splitmuxsink"
        ) as add:
            pm._ensure_audio_record_branch_built_locked()
        add.assert_not_called()


class AudioPresenceProbeTests(unittest.TestCase):
    """The probe is the single source of truth for `_audio_present_observed`."""

    def test_first_buffer_sets_observed(self) -> None:
        pm = PipelineManager.__new__(PipelineManager)
        pm._audio_present_observed = False
        # Stub Gst with a PadProbeReturn enum containing OK.
        pm._Gst = mock.Mock()
        pm._Gst.PadProbeReturn.OK = "ok"
        pm._source = mock.Mock()
        pm._source.get_feed_id.return_value = "feed_main"
        result = pm._on_audio_presence_probe(None, None, None)
        self.assertTrue(pm._audio_present_observed)
        self.assertEqual(result, "ok")

    def test_subsequent_buffers_idempotent(self) -> None:
        # The flag is sticky — the probe stays no-op-cheap on every
        # subsequent buffer. Explicit test because the probe runs at
        # audio-rate (~1000Hz typical).
        pm = PipelineManager.__new__(PipelineManager)
        pm._audio_present_observed = False
        pm._Gst = mock.Mock()
        pm._Gst.PadProbeReturn.OK = "ok"
        pm._source = mock.Mock()
        pm._source.get_feed_id.return_value = "feed_main"
        for _ in range(100):
            pm._on_audio_presence_probe(None, None, None)
        self.assertTrue(pm._audio_present_observed)


if __name__ == "__main__":
    unittest.main()
