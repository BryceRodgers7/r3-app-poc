"""Replay query layer over recorded segment files (slice 4.C).

Builds on the `SegmentIndex` (slice 4.B) to translate timestamps into
concrete segment file + in-segment offset locations. Intended consumer:
the operator's `PlaybackController` transport methods (rewind, pause,
slow, jump-to-live) once slice 4.C.tail wires them in.

Per §10.4 / §15.2: replay is unavailable while `recording_state !=
RECORDING`. The store enforces this — callers pass the current
`RecordingState` and the store refuses non-recording requests.

Per §6.6 / §15.7: the in-progress (writing) segment is NOT replayable —
its file is open for write and its trailing frames may not be readable.
`SegmentIndex.latest_replayable_pts` already filters those out, and
`resolve` only returns segments whose state == "complete".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import SEGMENT_STATE_COMPLETE, Segment
from app.core.recording_state import RecordingState
from app.storage.segment_index import SegmentIndex


@dataclass(frozen=True, slots=True)
class SegmentReplayLocation:
    """A resolved replay target: the segment file + how far into it to seek.

    `offset_in_segment_ns` is `target_pts_ns - segment.start_pts_ns`,
    clamped to >= 0. The replay player should seek the segment file by
    this offset once it has been opened.
    """

    segment: Segment
    offset_in_segment_ns: int


class RecordingSegmentReplayStore:
    """Resolves replay targets to concrete segment files using `SegmentIndex`.

    Wraps the index and adds the eligibility / boundary checks that the
    operator's transport methods need:

    - `is_replay_available(recording_state)` mirrors the §10.4 rule.
    - `resolve(feed_id, target_pts_ns, recording_state)` returns the
      segment containing `target_pts_ns`, or `None` if out of range or
      replay is unavailable.
    - `available_pts_range(feed_id)` returns `(earliest_pts,
      latest_replayable_pts)` so the UI can clamp seek targets and show
      "rewind back to" coverage.
    """

    def __init__(self, segment_index: SegmentIndex) -> None:
        self._index = segment_index

    def is_replay_available(self, *, recording_state: RecordingState) -> bool:
        return recording_state == RecordingState.RECORDING

    def resolve(
        self,
        *,
        feed_id: str,
        target_pts_ns: int,
        recording_state: RecordingState,
    ) -> SegmentReplayLocation | None:
        """Resolve `target_pts_ns` to a (segment, offset) location.

        Returns `None` when:
        - Replay is unavailable (recording not active).
        - The target is before the earliest recorded segment.
        - The target is after the latest replayable (i.e. completed) segment.
        - No completed segment contains the target timestamp.
        """
        if not self.is_replay_available(recording_state=recording_state):
            return None
        # Only consider completed segments — the in-progress segment is
        # excluded per §6.6 even if its tracked PTS span would otherwise
        # include the target.
        candidates = [
            s
            for s in self._index.segments_overlapping(
                feed_id, target_pts_ns, target_pts_ns
            )
            if s.state == SEGMENT_STATE_COMPLETE
        ]
        if not candidates:
            return None
        # `segments_overlapping` returns segments sorted by start_pts; the
        # first one is the right answer for a point query.
        segment = candidates[0]
        offset = max(0, target_pts_ns - segment.start_pts_ns)
        return SegmentReplayLocation(segment=segment, offset_in_segment_ns=offset)

    def earliest_pts(self, feed_id: str) -> int | None:
        """Return the earliest `start_pts_ns` for `feed_id`, or `None`."""
        return self._index.earliest_pts(feed_id)

    def latest_replayable_pts(self, feed_id: str) -> int | None:
        """Return the latest `end_pts_ns` of any completed segment, or `None`."""
        return self._index.latest_replayable_pts(feed_id)

    def available_pts_range(self, feed_id: str) -> tuple[int | None, int | None]:
        """Return `(earliest_pts, latest_replayable_pts)` for the operator UI."""
        return (self.earliest_pts(feed_id), self.latest_replayable_pts(feed_id))

    # ------------------------------------------------------------
    # Slice 5.B: session-time replay queries (§8.6 / §8.6.1).
    # ------------------------------------------------------------
    # `resolve_session_time` mirrors `resolve` but in session-time
    # space — strict, returns None when out of coverage. The
    # operator-UI rewind-target picker uses this to find the actual
    # coverage edge.
    #
    # `nearest_frame_location` implements the §8.6.1 catch-up rule —
    # always returns a location for any feed that has at least one
    # replayable segment, falling back to a freeze frame at the feed's
    # earliest (when target is before any coverage), latest (when
    # target is after all coverage), or last-segment-before-target
    # (when target is in a gap between segments). The multi-feed
    # render path in 5.C uses this so every tile renders something on
    # every tick.

    def resolve_session_time(
        self,
        *,
        feed_id: str,
        target_session_time_ns: int,
        recording_state: RecordingState,
    ) -> SegmentReplayLocation | None:
        """Strict session-time resolver. Returns None when out of coverage.

        Same eligibility / completed-only filter as `resolve`. Used by
        the rewind-target picker which wants to know if there's actual
        coverage at the target — not the freeze-frame fallback.
        """
        if not self.is_replay_available(recording_state=recording_state):
            return None
        candidates = [
            s
            for s in self._index.segments_overlapping_session_time(
                feed_id, target_session_time_ns, target_session_time_ns
            )
            if s.state == SEGMENT_STATE_COMPLETE
        ]
        if not candidates:
            return None
        segment = candidates[0]
        if segment.start_session_time_ns is None:
            return None
        offset = max(0, target_session_time_ns - segment.start_session_time_ns)
        return SegmentReplayLocation(segment=segment, offset_in_segment_ns=offset)

    def nearest_frame_location(
        self,
        *,
        feed_id: str,
        session_time_ns: int,
        recording_state: RecordingState,
    ) -> SegmentReplayLocation | None:
        """§8.6.1 clamping rule: always return a location when the feed
        has any replayable segment, falling back to a freeze frame.

        Decision tree:

        1. Replay not available → None.
        2. Feed has no completed segments with session-time → None.
        3. `session_time_ns` falls inside a segment → exact match,
           offset = `session_time - start_session_time`.
        4. `session_time_ns` is **before** the feed's earliest segment →
           that earliest segment, offset 0 (freeze on first frame).
        5. `session_time_ns` is **after** all segments **or** in a gap
           between segments → the latest segment whose
           `end_session_time_ns <= session_time_ns`, offset clamped to
           that segment's `duration_ns` (freeze on last frame). If no
           such segment exists (defensive — would imply the in-coverage
           check above missed) fall back to the earliest with offset 0.
        """
        if not self.is_replay_available(recording_state=recording_state):
            return None
        completed = [
            s
            for s in self._index.all_for_feed(feed_id)
            if s.state == SEGMENT_STATE_COMPLETE
            and s.start_session_time_ns is not None
            and s.end_session_time_ns is not None
        ]
        if not completed:
            return None
        # Case 3 — exact in-coverage match.
        for seg in completed:
            assert seg.start_session_time_ns is not None
            assert seg.end_session_time_ns is not None
            if seg.start_session_time_ns <= session_time_ns <= seg.end_session_time_ns:
                offset = session_time_ns - seg.start_session_time_ns
                return SegmentReplayLocation(segment=seg, offset_in_segment_ns=offset)
        # Case 4 — before the earliest segment.
        earliest = completed[0]
        assert earliest.start_session_time_ns is not None
        if session_time_ns < earliest.start_session_time_ns:
            return SegmentReplayLocation(segment=earliest, offset_in_segment_ns=0)
        # Case 5 — after all coverage or in a gap. Pick the segment
        # whose end_session_time is the highest at-or-before
        # `session_time_ns`.
        nearest_before: Segment | None = None
        for seg in completed:
            assert seg.end_session_time_ns is not None
            if seg.end_session_time_ns <= session_time_ns:
                if (
                    nearest_before is None
                    or (
                        nearest_before.end_session_time_ns is not None
                        and seg.end_session_time_ns > nearest_before.end_session_time_ns
                    )
                ):
                    nearest_before = seg
        if nearest_before is None:
            # Defensive fallback — the case 4 guard above should have
            # caught it, but if for some reason we land here, freeze
            # on the earliest segment's first frame.
            return SegmentReplayLocation(segment=earliest, offset_in_segment_ns=0)
        return SegmentReplayLocation(
            segment=nearest_before,
            offset_in_segment_ns=max(0, nearest_before.duration_ns),
        )

    def earliest_session_time(self, feed_id: str) -> int | None:
        """Return the earliest `start_session_time_ns` for `feed_id`, or None."""
        return self._index.earliest_session_time(feed_id)

    def latest_replayable_session_time(self, feed_id: str) -> int | None:
        """Return the latest `end_session_time_ns` of any complete segment, or None."""
        return self._index.latest_replayable_session_time(feed_id)

    def available_session_time_range(self) -> tuple[int | None, int | None]:
        """Return `(earliest_start, latest_replayable_end)` across all feeds.

        Operator-UI bounds for the replay timeline. Crosses feeds by
        design — the timeline is shared per §8.1.
        """
        return self._index.cross_feed_session_time_range()

    def feeds_with_coverage_at(self, session_time_ns: int) -> list[str]:
        """Return feed_ids that have a completed segment covering this
        session time. Convenience pass-through for the multi-feed
        render path in 5.C."""
        return self._index.feeds_with_coverage_at(session_time_ns)
