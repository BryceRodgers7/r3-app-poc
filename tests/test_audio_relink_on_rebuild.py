"""Phase 7.G — audio re-link on splitmuxsink rebuild.

Bug: `_rebuild_splitmuxsink_locked` only re-attached the video chain
(jpegenc → splitmuxsink). The audio_record chain (opusenc →
splitmuxsink.audio_%u) was not re-attached, so the second-and-later
games of a session recorded video-only segment files. The MP4 export
then naturally inherited the missing audio.

Fix: a shared `_link_audio_encoder_to_splitmuxsink_locked` helper
that requests `audio_%u` on the target splitmuxsink and links the
encoder. Both initial wiring and rebuild call it. These tests stub
GStreamer elements (no real plumbing) and verify the helper's
contract.
"""

from __future__ import annotations

import unittest

from app.media.pipeline_manager import PipelineManager


_PAD_LINK_OK = object()  # Singleton sentinel matching `Gst.PadLinkReturn.OK`.


class _StubPad:
    """Minimal stand-in for a GStreamer pad."""

    def __init__(self, name: str = "pad") -> None:
        self.name = name
        self._peer: "_StubPad | None" = None
        self.link_calls: list["_StubPad"] = []
        self.unlink_calls: list["_StubPad"] = []

    def get_peer(self) -> "_StubPad | None":
        return self._peer

    def link(self, other: "_StubPad") -> object:
        self.link_calls.append(other)
        self._peer = other
        other._peer = self
        return _PAD_LINK_OK

    def unlink(self, other: "_StubPad") -> None:
        self.unlink_calls.append(other)
        if self._peer is other:
            self._peer = None
        if other._peer is self:
            other._peer = None


class _StubGst:
    class PadLinkReturn:
        OK = _PAD_LINK_OK


class _StubEncoder:
    """A `Gst.Element`-shaped stand-in with a single static `src` pad."""

    def __init__(self) -> None:
        self.src_pad = _StubPad("opusenc.src")

    def get_static_pad(self, name: str) -> _StubPad:
        assert name == "src"
        return self.src_pad


class _StubSplitmuxsink:
    """Tracks request-pad calls so we can assert the helper asks for
    a fresh `audio_%u` on each rebuild."""

    def __init__(self, name: str = "splitmuxsink") -> None:
        self.name = name
        self.audio_pad = _StubPad(f"{name}.audio_0")
        self.request_pad_calls: list[str] = []

    def request_pad_simple(self, template: str) -> _StubPad:
        self.request_pad_calls.append(template)
        return self.audio_pad


def _build_pm_with_audio() -> tuple[PipelineManager, _StubEncoder]:
    pm = PipelineManager.__new__(PipelineManager)
    pm._Gst = _StubGst
    encoder = _StubEncoder()
    pm._audio_record_encoder = encoder
    return pm, encoder


class LinkAudioEncoderHelperTests(unittest.TestCase):
    """`_link_audio_encoder_to_splitmuxsink_locked` is the common
    helper used by both initial wiring and rebuild. Locking its
    contract means the rebuild can't regress to forgetting audio."""

    def test_links_encoder_src_to_new_splitmuxsink_audio_pad(self) -> None:
        pm, encoder = _build_pm_with_audio()
        new_sink = _StubSplitmuxsink()
        # Helper hasn't run yet — no link.
        self.assertIsNone(encoder.src_pad.get_peer())
        # Run the helper.
        pm._link_audio_encoder_to_splitmuxsink_locked(new_sink)
        # Helper requested an audio pad on the new sink.
        self.assertEqual(new_sink.request_pad_calls, ["audio_%u"])
        # And linked encoder.src to it.
        self.assertIs(encoder.src_pad.get_peer(), new_sink.audio_pad)

    def test_unlinks_old_peer_before_relinking(self) -> None:
        # Simulate the rebuild path: encoder.src is already linked to
        # the OLD splitmuxsink's audio pad. Helper must unlink before
        # linking to the new one.
        pm, encoder = _build_pm_with_audio()
        old_sink = _StubSplitmuxsink(name="old")
        old_sink.audio_pad._peer = encoder.src_pad
        encoder.src_pad._peer = old_sink.audio_pad

        new_sink = _StubSplitmuxsink(name="new")
        pm._link_audio_encoder_to_splitmuxsink_locked(new_sink)

        # Old peer unlinked.
        self.assertEqual(encoder.src_pad.unlink_calls, [old_sink.audio_pad])
        # New link established.
        self.assertIs(encoder.src_pad.get_peer(), new_sink.audio_pad)

    def test_no_op_when_audio_chain_was_never_wired(self) -> None:
        # `recording_audio_enabled=False` path: `_audio_record_encoder`
        # stays None. The helper must NOT request an audio pad —
        # otherwise a video-only deployment would force splitmuxsink
        # to wait for audio buffers that never arrive (the
        # Phase 7.E audio-stall scenario).
        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = _StubGst
        pm._audio_record_encoder = None
        new_sink = _StubSplitmuxsink()
        pm._link_audio_encoder_to_splitmuxsink_locked(new_sink)
        self.assertEqual(new_sink.request_pad_calls, [])


if __name__ == "__main__":
    unittest.main()
