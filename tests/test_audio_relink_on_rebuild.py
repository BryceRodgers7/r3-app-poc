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
        self._release_calls: list[_StubPad] = []
        self.sync_state_calls: int = 0

    def request_pad_simple(self, template: str) -> _StubPad:
        self.request_pad_calls.append(template)
        return self.audio_pad

    def release_request_pad(self, pad: _StubPad) -> None:
        self._release_calls.append(pad)

    def sync_state_with_parent(self) -> bool:
        self.sync_state_calls += 1
        return True

    def set_state(self, _state: object) -> object:
        return _state


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


class _StubPipeline:
    """Minimal stand-in for `Gst.Pipeline` that records add/remove."""

    def __init__(self) -> None:
        self.removed: list[object] = []
        self.added: list[object] = []

    def remove(self, element: object) -> bool:
        self.removed.append(element)
        return True

    def add(self, element: object) -> bool:
        self.added.append(element)
        return True


class _StubJpegenc:
    """Minimal jpegenc stub for the rebuild flow."""

    def __init__(self) -> None:
        self.unlink_calls: list[object] = []
        self.link_calls: list[object] = []

    def unlink(self, other: object) -> None:
        self.unlink_calls.append(other)

    def link(self, other: object) -> bool:
        self.link_calls.append(other)
        return True


class RebuildAudioUnlinkContractTests(unittest.TestCase):
    """Bug regression test (session_145):

    `_rebuild_splitmuxsink_locked` MUST explicitly unlink the audio
    encoder's src pad from the OLD splitmuxsink's audio request pad
    BEFORE the old element is removed from the pipeline. Without
    this, the encoder src pad stays internally linked to the
    orphaned old audio pad past `pipeline.remove(old)` (gst doesn't
    auto-unlink across bin removal), `get_peer()` later returns
    `None` (despite the linkage still existing), the defensive
    unlink in `_link_audio_encoder_to_splitmuxsink_locked` is
    skipped, and the new audio link returns `WAS_LINKED` — which
    aborts the rebuild before the video link runs, leaving game N+1
    with zero segment files.
    """

    def test_rebuild_unlinks_audio_encoder_before_pipeline_remove(self) -> None:
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = _StubGst
        pm._pipeline = _StubPipeline()
        pm._record_branch_encoder = _StubJpegenc()
        pm._record_branch_name = "record"
        pm._recording_feed_id = "ndi_1"

        # Set up the audio chain as it would exist after game 1's
        # initial wiring: encoder.src linked to old_sink.audio_pad.
        encoder = _StubEncoder()
        pm._audio_record_encoder = encoder

        old_sink = _StubSplitmuxsink(name="old_splitmuxsink")
        old_audio_pad = old_sink.audio_pad
        old_audio_pad._peer = encoder.src_pad
        encoder.src_pad._peer = old_audio_pad
        pm._splitmuxsink = old_sink

        # Stubbed factories so the rebuild doesn't reach real GStreamer.
        new_sink = _StubSplitmuxsink(name="new_splitmuxsink")
        pm._build_splitmuxsink_element = lambda _branch: new_sink
        pm._ensure_audio_record_branch_built_locked = lambda: None
        link_helper_calls: list[object] = []
        def fake_link(target_sink: object) -> None:
            link_helper_calls.append(target_sink)
        pm._link_audio_encoder_to_splitmuxsink_locked = fake_link

        # Run the rebuild.
        pm._rebuild_splitmuxsink_locked()

        # Contract: encoder.src was unlinked from the old audio pad
        # BEFORE pipeline.remove was called.
        self.assertIn(
            old_audio_pad, encoder.src_pad.unlink_calls,
            "rebuild must explicitly unlink encoder.src from old audio pad",
        )
        # And the old request pad was released back to the old element
        # (avoids leaking request pads).
        self.assertIn(
            old_audio_pad, old_sink._release_calls,
            "rebuild must release the old audio request pad",
        )
        # pipeline.remove fired against the old sink.
        self.assertEqual(pm._pipeline.removed, [old_sink])
        # The audio re-link helper was still called for the new sink,
        # so the encoder will be linked to the fresh audio pad.
        self.assertEqual(link_helper_calls, [new_sink])


class _StubFlushPad:
    """Tracks `send_event` calls so the flush helper test can verify
    the right event sequence was sent."""

    def __init__(self) -> None:
        self.events_sent: list[object] = []

    def send_event(self, event: object) -> bool:
        self.events_sent.append(event)
        return True


class _StubQueue:
    def __init__(self, name: str = "audio_record_mux_queue") -> None:
        self.name = name
        self.sink_pad = _StubFlushPad()

    def get_static_pad(self, name: str) -> _StubFlushPad:
        assert name == "sink"
        return self.sink_pad


class _StubPipelineWithGetByName:
    """`get_by_name` returns a registered element. Used by the flush
    helper test that needs `_pipeline.get_by_name("audio_record_mux_queue")`."""

    def __init__(self) -> None:
        self._by_name: dict[str, object] = {}

    def add(self, element: object) -> bool:
        return True

    def remove(self, element: object) -> bool:
        return True

    def get_by_name(self, name: str) -> object:
        return self._by_name.get(name)

    def register(self, name: str, element: object) -> None:
        self._by_name[name] = element


class _StubGstWithFlushEvents:
    class PadLinkReturn:
        OK = _PAD_LINK_OK

    class Event:
        @staticmethod
        def new_flush_start() -> object:
            return ("flush_start",)

        @staticmethod
        def new_flush_stop(reset_time: bool) -> object:
            return ("flush_stop", reset_time)


class AudioMuxBranchFlushTests(unittest.TestCase):
    """Bug regression test (post-session-148-game-2 audio misalignment):

    `disable_file_recording` MUST flush the audio_record_mux chain
    after closing the valve, so no opus packet survives the gap
    between games. Without the flush, opusenc's window-encoding
    buffer can leave one stranded packet between encoder.src and
    the (now-NULL) old splitmuxsink. On rebuild, that packet flows
    through the new splitmuxsink first, carrying the previous game's
    PTS — which surfaces in the post-processed MP4 as audio playing
    for ~15s before any video appears.

    Contract: when called with an audio chain in place, the flush
    helper sends `FLUSH_START` then `FLUSH_STOP` (with
    `reset_time=False`) on the audio_record_mux_queue's sink pad.
    """

    def test_flush_helper_sends_flush_start_then_stop_on_head_queue(self) -> None:
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = _StubGstWithFlushEvents
        pm._pipeline = _StubPipelineWithGetByName()
        pm._audio_record_encoder = _StubEncoder()
        pm._recording_feed_id = "ndi_1"

        head_queue = _StubQueue()
        pm._pipeline.register("audio_record_mux_queue", head_queue)

        pm._flush_audio_mux_branch_locked()

        # Two events sent: flush-start, then flush-stop(reset_time=False).
        self.assertEqual(len(head_queue.sink_pad.events_sent), 2)
        self.assertEqual(
            head_queue.sink_pad.events_sent[0],
            ("flush_start",),
        )
        self.assertEqual(
            head_queue.sink_pad.events_sent[1],
            ("flush_stop", False),
        )

    def test_flush_helper_no_op_when_audio_chain_was_never_wired(self) -> None:
        # `recording_audio_enabled=False` path or video-only source —
        # `_audio_record_encoder` stays None, helper must NOT touch the
        # pipeline.
        from app.media.pipeline_manager import PipelineManager

        pm = PipelineManager.__new__(PipelineManager)
        pm._Gst = _StubGstWithFlushEvents
        pm._pipeline = _StubPipelineWithGetByName()
        pm._audio_record_encoder = None
        pm._recording_feed_id = "ndi_1"

        head_queue = _StubQueue()
        pm._pipeline.register("audio_record_mux_queue", head_queue)

        pm._flush_audio_mux_branch_locked()

        self.assertEqual(head_queue.sink_pad.events_sent, [])


class AudioRecordBranchNameCollisionTests(unittest.TestCase):
    """Bug regression test (session_147):

    `_add_audio_record_branch_to_splitmuxsink` MUST use element names
    distinct from the audio drain appsink branch built unconditionally
    by `_add_audio_appsink_branch("record", ...)` at pipeline-init.

    The drain owns these names (see `_add_audio_appsink_branch`):
      `audio_record_queue` / `audio_record_valve` / `audio_record_sink`

    If the mux-branch reuses any of those, `pipeline.add(...)` silently
    fails for the colliding element (GStreamer rejects duplicate names
    in a bin), the new element is orphaned, and `queue.link(valve)`
    fails (or silently succeeds while orphaned, then the orphan-encoder
    raises WRONG_HIERARCHY when the link helper tries to bind it to
    the pipelined splitmuxsink). Either path: zero segments for game 2+.
    """

    def test_mux_branch_element_names_do_not_collide_with_drain(self) -> None:
        # Read the actual element-name string literals from the source
        # file so this test catches a future rename that re-introduces
        # the collision.
        from pathlib import Path

        source = (
            Path(__file__).parent.parent
            / "app"
            / "media"
            / "pipeline_manager.py"
        ).read_text(encoding="utf-8")

        drain_names = {
            '"audio_record_queue"',
            '"audio_record_valve"',
            '"audio_record_sink"',
        }
        # Find the body of _add_audio_record_branch_to_splitmuxsink and
        # confirm none of the drain names appear as element-name args
        # to `_make_element` calls inside it.
        start = source.index("def _add_audio_record_branch_to_splitmuxsink")
        # Next def at the same indent level marks end of body.
        end = source.index("\n    def ", start + 1)
        mux_body = source[start:end]
        for name in drain_names:
            self.assertNotIn(
                name,
                mux_body,
                f"_add_audio_record_branch_to_splitmuxsink must not "
                f"reuse drain element name {name} — see session_147 "
                f"bug investigation",
            )


if __name__ == "__main__":
    unittest.main()
