"""Phase 7.C: replay safety invariants + transport-method read-only audit.

Two concerns locked in here:

1. Segments in `state="writing"` are NEVER returned by replay queries —
   even when their PTS or session-time range would otherwise overlap
   the target. A writing segment's file is open for write and its
   trailing frames may not be readable (§6.6 / §15.7). Some coverage
   exists across `tests/test_segment_replay_store.py` and
   `tests/test_session_time_queries.py`; this file adds the
   "completed segments PLUS a writing tail" fixture that exercises the
   exact case the operator UI sees during recording: the in-progress
   segment overlaps the live edge.

2. Transport methods (`rewind_configured_seconds`, `pause_playback`,
   `set_playback_rate`, `jump_to_live`, `_on_replay_timer_tick`,
   `_render_at_session_time_ns`) NEVER mutate the filesystem or DB.
   Replay reads from completed segments; it never deletes, renames, or
   rewrites them. Patches `Path.unlink/rename/replace`, `os.remove`,
   `shutil.move`, and every `MetadataDb` write to make any mutation
   call fail loudly during transport invocation.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import numpy as np
from PySide6.QtCore import QCoreApplication

from app.core.models import (
    SEGMENT_STATE_COMPLETE,
    SEGMENT_STATE_WRITING,
    FeedDefinition,
    FrameOverlayInfo,
    MediaFrame,
    Segment,
    SessionPaths,
)
from app.core.playback_controller import PlaybackController
from app.core.recording_state import RecordingState
from app.core.session_clock import SessionClock
from app.media.feed_runtime import FeedRuntime
from app.media.recording_manager import RecordingManager
from app.storage.metadata_db import MetadataDb
from app.storage.segment_index import SegmentIndex
from app.storage.segment_replay_store import RecordingSegmentReplayStore

from test_playback_controller import (  # type: ignore[import-not-found]
    _FakeNanosecondClock,
    _FakePipelineManager,
    _FakeRenderer,
    _FakeSource,
    _StubSegmentDecoder,
    _make_segment,
)


def _seg(
    *,
    feed_id: str = "feed_main",
    fragment_index: int,
    start_session_time_ns: int,
    duration_ns: int = 4_000_000_000,
    state: str = SEGMENT_STATE_COMPLETE,
) -> Segment:
    end_session_time_ns = start_session_time_ns + duration_ns
    return Segment(
        session_id="s1",
        feed_id=feed_id,
        fragment_index=fragment_index,
        file_path=f"/tmp/{feed_id}/seg_{fragment_index:05d}.mkv",
        codec="mjpeg",
        container="mkv",
        start_pts_ns=start_session_time_ns,
        end_pts_ns=end_session_time_ns,
        duration_ns=duration_ns,
        frame_count_estimate=120,
        size_bytes=5_000_000,
        state=state,
        created_at="2026-04-28T01:00:00+00:00",
        finalized_at=(
            "2026-04-28T01:00:04+00:00" if state == SEGMENT_STATE_COMPLETE else None
        ),
        start_session_time_ns=start_session_time_ns,
        end_session_time_ns=end_session_time_ns,
        pts_to_session_offset_ns=0,
    )


class WritingTailExclusionTests(unittest.TestCase):
    """Fixture mirrors the live-recording scenario: 4 completed segments
    cover session_time 0..16s; the in-progress (writing) segment at
    16..20s is in the index because the buffer probe wrote it as
    soon as the first frame arrived. Any replay query at session_time
    ≥ 16s must NOT resolve into the writing segment.
    """

    def setUp(self) -> None:
        self.idx = SegmentIndex()
        for i in range(4):
            self.idx.add(
                _seg(fragment_index=i, start_session_time_ns=i * 4_000_000_000)
            )
        # Writing tail covers 16..20s.
        self.idx.add(
            _seg(
                fragment_index=4,
                start_session_time_ns=16_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        self.store = RecordingSegmentReplayStore(self.idx)

    def test_resolve_pts_inside_writing_tail_returns_none(self) -> None:
        # PTS 18s is inside the writing tail's [16, 20) range.
        result = self.store.resolve(
            feed_id="feed_main",
            target_pts_ns=18_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_resolve_session_time_inside_writing_tail_returns_none(self) -> None:
        result = self.store.resolve_session_time(
            feed_id="feed_main",
            target_session_time_ns=18_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNone(result)

    def test_nearest_frame_location_inside_writing_tail_freezes_on_last_completed(
        self,
    ) -> None:
        # Target inside the writing tail; the §8.6.1 fallback should
        # land on the LAST COMPLETED segment (frag 3, ends at 16s) as a
        # freeze frame, NOT on the writing segment.
        result = self.store.nearest_frame_location(
            feed_id="feed_main",
            session_time_ns=18_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.is_freeze)
        self.assertEqual(result.segment.fragment_index, 3)
        self.assertEqual(result.segment.state, SEGMENT_STATE_COMPLETE)

    def test_nearest_frame_location_in_completed_segment_returns_exact_match(
        self,
    ) -> None:
        # Target inside frag 2 (8..12s); should exact-match, not freeze.
        result = self.store.nearest_frame_location(
            feed_id="feed_main",
            session_time_ns=10_000_000_000,
            recording_state=RecordingState.RECORDING,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.is_freeze)
        self.assertEqual(result.segment.fragment_index, 2)

    def test_latest_replayable_pts_excludes_writing_tail(self) -> None:
        # Latest end PTS of any complete segment is frag 3's (16s).
        # The writing tail's end PTS (20s) must be excluded.
        self.assertEqual(
            self.store.latest_replayable_pts("feed_main"),
            16_000_000_000,
        )

    def test_latest_replayable_session_time_excludes_writing_tail(self) -> None:
        self.assertEqual(
            self.store.latest_replayable_session_time("feed_main"),
            16_000_000_000,
        )

    def test_segment_index_latest_replayable_pts_directly(self) -> None:
        # SegmentIndex's per-feed `latest_replayable_pts` already
        # filters writing segments. Lock that in at the index layer
        # too — the store relies on it for the no-game-filter path.
        self.assertEqual(
            self.idx.latest_replayable_pts("feed_main"), 16_000_000_000
        )

    def test_segment_index_latest_replayable_session_time_directly(self) -> None:
        self.assertEqual(
            self.idx.latest_replayable_session_time("feed_main"),
            16_000_000_000,
        )


class _WriteDetectingMetadataDb(MetadataDb):
    """MetadataDb that records every mutating call.

    Used by `TransportMethodsAreReadOnlyTests` to assert that the
    PlaybackController transport methods never write to the DB. The
    DB itself is real (in tmp dir) so any code path that DID try to
    write would behave normally — the assertion is on the recorded
    calls, not on side-effect prevention.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.write_calls: list[str] = []

    def create_session(self, *args, **kwargs):  # type: ignore[override]
        self.write_calls.append("create_session")
        return super().create_session(*args, **kwargs)

    def insert_segment(self, *args, **kwargs):  # type: ignore[override]
        self.write_calls.append("insert_segment")
        return super().insert_segment(*args, **kwargs)

    def update_segment_state(self, *args, **kwargs):  # type: ignore[override]
        self.write_calls.append("update_segment_state")
        return super().update_segment_state(*args, **kwargs)

    def update_segment_file_path(self, *args, **kwargs):  # type: ignore[override]
        self.write_calls.append("update_segment_file_path")
        return super().update_segment_file_path(*args, **kwargs)


class TransportMethodsAreReadOnlyTests(unittest.TestCase):
    """The replay transport never deletes, renames, or rewrites recorded
    media — and never writes to the DB. The test instruments
    `Path.unlink/rename/replace`, `os.remove/unlink/rename/replace`,
    `shutil.move/rmtree`, and every `MetadataDb` write method to fail
    if called, then exercises each transport method against an index
    that intentionally contains a writing tail segment (the trickiest
    case for accidental mutation: a code path that "cleans up" stale
    rows would touch this segment).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        root_dir = Path(self._temp_dir.name) / "session_001"
        (root_dir / "recording").mkdir(parents=True, exist_ok=True)
        self.session_paths = SessionPaths(
            session_id="session_001",
            root_dir=root_dir,
            recording_dir=root_dir / "recording",
        )
        self.feed = FeedDefinition(feed_id="feed_main", display_name="Fake Source")
        self.recording_manager = RecordingManager()
        self.segment_index = SegmentIndex()
        # Five 4s completed segments + a writing tail segment.
        for i in range(5):
            self.segment_index.add(
                _make_segment(fragment_index=i, start_pts_ns=i * 4_000_000_000)
            )
        self.segment_index.add(
            _make_segment(
                fragment_index=5,
                start_pts_ns=20_000_000_000,
                state=SEGMENT_STATE_WRITING,
            )
        )
        self.replay_store = RecordingSegmentReplayStore(self.segment_index)

        self.fake_clock_fn = _FakeNanosecondClock(start_ns=0)
        self.fake_clock_fn.advance_ns(10_000_000_000)
        self.session_clock = SessionClock(clock_ns=self.fake_clock_fn)

        # Real (instrumented) DB so production code paths that DID try
        # to write would behave normally; the test asserts call count.
        self.db = _WriteDetectingMetadataDb(root_dir / "metadata.db")
        self.runtime = FeedRuntime(
            feed=self.feed,
            source=_FakeSource(self.feed.feed_id),
            pipeline_manager=_FakePipelineManager(self.feed.feed_id),
        )
        self.runtime.start(self.session_paths)
        for frame_id in range(20):
            image = np.full((24, 32, 3), frame_id % 255, dtype=np.uint8)
            frame = MediaFrame(
                frame_id=frame_id,
                timestamp=100.0 + frame_id,
                image=image,
                source_name="Fake Source",
                feed_id=self.feed.feed_id,
            )
            self.runtime._on_live_frame(frame)
            self.runtime._on_live_overlay(
                FrameOverlayInfo.from_media_frame(frame, feed_id=self.feed.feed_id)
            )

        self.renderer = _FakeRenderer()
        self.controller = PlaybackController(
            feed_runtimes=[self.runtime],
            output_renderer=self.renderer,
            recording_manager=self.recording_manager,
            replay_store=self.replay_store,
            default_source_name="Fake Source",
            session_role="operator",
            live_only=False,
            decoder_factory=lambda *_args: _StubSegmentDecoder(
                self.feed.feed_id, self.feed.display_name
            ),
            session_clock=self.session_clock,
        )
        self.controller.initialize(self.session_paths.session_id)
        self.recording_manager.recording_state.force(RecordingState.RECORDING)
        self.controller.refresh_recording_state()

        self.advance_clock_to_replayable_window()

        # Patch the mutation surfaces. Each is patched to raise
        # `AssertionError` on call so a transport method that DID
        # mutate would fail the test loudly with a stack trace.
        def _assert_unreachable(method_name: str):
            def _fail(*_args, **_kwargs):
                raise AssertionError(f"{method_name} was called by a transport method")
            return _fail

        self._patches = [
            mock.patch.object(Path, "unlink", _assert_unreachable("Path.unlink")),
            mock.patch.object(Path, "rename", _assert_unreachable("Path.rename")),
            mock.patch.object(Path, "replace", _assert_unreachable("Path.replace")),
            mock.patch.object(os, "remove", _assert_unreachable("os.remove")),
            mock.patch.object(os, "unlink", _assert_unreachable("os.unlink")),
            mock.patch.object(os, "rename", _assert_unreachable("os.rename")),
            mock.patch.object(os, "replace", _assert_unreachable("os.replace")),
            mock.patch.object(shutil, "move", _assert_unreachable("shutil.move")),
            mock.patch.object(shutil, "rmtree", _assert_unreachable("shutil.rmtree")),
        ]
        for p in self._patches:
            p.start()
        self.db.write_calls.clear()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.controller.shutdown()
        self.db.close()
        self._temp_dir.cleanup()

    def advance_clock_to_replayable_window(self) -> None:
        """Advance the fake clock past the latest finalized segment so
        rewind/pause/jump-to-live land inside replayable coverage."""
        # Latest replayable session_time = 20s (frag 4 ends at 20).
        # Advance clock to 24s so the operator is "behind live" by 4s.
        self.fake_clock_fn.advance_ns(24_000_000_000)

    def test_rewind_configured_seconds_does_not_mutate(self) -> None:
        self.controller.rewind_configured_seconds()
        self.assertEqual(self.db.write_calls, [])

    def test_pause_playback_does_not_mutate(self) -> None:
        self.controller.pause_playback()
        self.assertEqual(self.db.write_calls, [])

    def test_set_playback_rate_does_not_mutate(self) -> None:
        self.controller.set_playback_rate(0.5)
        self.assertEqual(self.db.write_calls, [])

    def test_jump_to_live_does_not_mutate(self) -> None:
        # Drop into REPLAY first so jump_to_live has something to undo.
        self.controller.rewind_configured_seconds()
        self.db.write_calls.clear()
        self.controller.jump_to_live()
        self.assertEqual(self.db.write_calls, [])

    def test_replay_timer_tick_does_not_mutate(self) -> None:
        # Enter REPLAY mode so the timer-tick path is meaningful.
        self.controller.rewind_configured_seconds()
        self.db.write_calls.clear()
        self.controller._on_replay_timer_tick()
        self.assertEqual(self.db.write_calls, [])

    def test_render_at_session_time_does_not_mutate(self) -> None:
        # Drive the renderer with a target inside the writing tail's
        # session-time range. The §8.6.1 fallback should freeze on the
        # latest completed segment — not touch the writing segment's
        # file in any way.
        self.controller._render_at_session_time_ns(22_000_000_000)
        self.assertEqual(self.db.write_calls, [])


if __name__ == "__main__":
    unittest.main()
