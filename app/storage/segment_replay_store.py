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

    `offset_in_segment_ns` is the seek offset within the resolved
    segment file, clamped to >= 0.

    `is_freeze` is `True` when this location is the result of the
    §8.6.1 clamping rule (the requested time fell outside the feed's
    coverage and the resolver returned the nearest available frame as
    a freeze) and `False` for exact-coverage matches. Phase 6 uses
    this to surface "FROZEN" badges on operator tiles whose feed is
    currently in freeze-frame mode (§15.5 degraded-replay indicator).
    The strict resolvers (`resolve`, `resolve_session_time`) only
    return on exact coverage, so they always emit `is_freeze=False`.

    `frame_index` is the 0-based frame within the file, set when the
    segment's bounds and frame count are exact (`Segment.end_exclusive`).
    The decoder then reads that frame directly instead of deriving one
    from the offset and the container's declared frame rate.
    """

    segment: Segment
    offset_in_segment_ns: int
    is_freeze: bool = False
    frame_index: int | None = None


def _frame_period_ns(segment: Segment) -> float | None:
    """Average frame spacing of an exact-bounds segment, else None.

    Only `end_exclusive` rows qualify: their duration runs from the first
    frame to the start of the next segment and their frame count is the
    number of frames in the file, so `duration / count` is the period.
    """
    if (
        not segment.end_exclusive
        or segment.frame_count_estimate <= 0
        or segment.duration_ns <= 0
    ):
        return None
    return segment.duration_ns / segment.frame_count_estimate


def _last_frame_session_time(segment: Segment) -> int | None:
    """Session time of a segment's last frame (its end, for older rows)."""
    if segment.end_session_time_ns is None:
        return None
    period = _frame_period_ns(segment)
    if period is None:
        return segment.end_session_time_ns
    return segment.end_session_time_ns - int(round(period))


def _frame_location(
    segment: Segment, frame_index: int, period_ns: float, *, is_freeze: bool
) -> SegmentReplayLocation:
    frame_index = min(max(0, frame_index), segment.frame_count_estimate - 1)
    return SegmentReplayLocation(
        segment=segment,
        offset_in_segment_ns=int(round(frame_index * period_ns)),
        is_freeze=is_freeze,
        frame_index=frame_index,
    )


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
        # Phase 7.B-ext: per-game scoping. When set, all session-time
        # queries exclude segments whose `start_session_time_ns` is
        # before this value. The coordinator sets this on each Start
        # press of "Start game recording" so timing surfaces (rewind
        # bounds, status-bar coverage range, "behind live" counter)
        # reflect the current game only — not segments left over in
        # the index from a prior Stop/Start cycle within the same
        # session. None = include all segments (fresh-session path,
        # tests, ad-hoc tooling).
        self._current_game_start_session_time_ns: int | None = None

    def is_replay_available(self, *, recording_state: RecordingState) -> bool:
        return recording_state == RecordingState.RECORDING

    def set_current_game_start_session_time(
        self, session_time_ns: int | None
    ) -> None:
        """Scope subsequent session-time queries to segments at or after `session_time_ns`.

        Pass `None` to clear the filter (after Stop, between games — the
        recording-state gate keeps queries idle anyway, but clearing
        avoids stale filter state if recording starts again without
        going through `set_current_game_start_session_time` first).
        """
        self._current_game_start_session_time_ns = session_time_ns

    def _segment_in_current_game(self, segment: Segment) -> bool:
        """Per-game filter applied to every session-time query."""
        if self._current_game_start_session_time_ns is None:
            return True
        if segment.start_session_time_ns is None:
            return False
        return (
            segment.start_session_time_ns
            >= self._current_game_start_session_time_ns
        )

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
            and self._segment_in_current_game(s)
            # An exclusive end belongs to the next segment.
            and not (
                s.end_exclusive and s.end_session_time_ns == target_session_time_ns
            )
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
        3. `session_time_ns` falls inside a segment → exact match.
           - Exact-bounds segments (`end_exclusive`) cover the half-open
             `[start, end)`, and the answer is the frame *nearest* to
             `session_time_ns`: frame k is shown from half a frame before
             its timestamp to half a frame after it. So the lookup uses
             `session_time + half a frame`, and a time in the last half
             frame of a segment shows the next segment's first frame.
             Consecutive segments meet exactly, so stepping across a seam
             neither repeats nor skips a frame
             (docs/REPLAY_SEAM_GLITCH.md §7.4 step 2).
           - Older segments: the inclusive `[start, end]`, offset =
             `session_time - start_session_time`.
        4. `session_time_ns` is **before** the feed's earliest segment →
           that earliest segment's first frame (freeze).
        5. `session_time_ns` is **after** all segments **or** in a gap
           between segments → the last frame of the latest segment that
           ends at or before it (freeze). If no such segment exists
           (defensive — would imply the in-coverage check above missed)
           fall back to the earliest segment's first frame.
        """
        if not self.is_replay_available(recording_state=recording_state):
            return None
        completed = [
            s
            for s in self._index.all_for_feed(feed_id)
            if s.state == SEGMENT_STATE_COMPLETE
            and s.start_session_time_ns is not None
            and s.end_session_time_ns is not None
            and self._segment_in_current_game(s)
        ]
        if not completed:
            return None
        # The index orders by PTS, which restarts at 0 when a feed's
        # pipeline is rebuilt mid-game; session time does not.
        completed.sort(key=lambda s: s.start_session_time_ns)
        # Case 3 — exact in-coverage match. is_freeze=False.
        for seg in completed:
            assert seg.start_session_time_ns is not None
            assert seg.end_session_time_ns is not None
            period = _frame_period_ns(seg)
            if period is not None:
                lookup = session_time_ns + period / 2
                if seg.start_session_time_ns <= lookup < seg.end_session_time_ns:
                    frame_index = int((lookup - seg.start_session_time_ns) // period)
                    return _frame_location(seg, frame_index, period, is_freeze=False)
            elif seg.start_session_time_ns <= session_time_ns <= seg.end_session_time_ns:
                offset = session_time_ns - seg.start_session_time_ns
                return SegmentReplayLocation(
                    segment=seg, offset_in_segment_ns=offset, is_freeze=False
                )
        # Case 4 — before the earliest segment. Freeze on first frame.
        earliest = completed[0]
        assert earliest.start_session_time_ns is not None
        if session_time_ns < earliest.start_session_time_ns:
            return self._first_frame(earliest)
        # Case 5 — after all coverage or in a gap. Pick the segment
        # whose end_session_time is the highest at-or-before
        # `session_time_ns`. Freeze on its last frame.
        nearest_before: Segment | None = None
        for seg in completed:
            assert seg.end_session_time_ns is not None
            period = _frame_period_ns(seg)
            lookup = session_time_ns + period / 2 if period is not None else session_time_ns
            if seg.end_session_time_ns <= lookup:
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
            return self._first_frame(earliest)
        period = _frame_period_ns(nearest_before)
        if period is not None:
            return _frame_location(
                nearest_before,
                nearest_before.frame_count_estimate - 1,
                period,
                is_freeze=True,
            )
        return SegmentReplayLocation(
            segment=nearest_before,
            offset_in_segment_ns=max(0, nearest_before.duration_ns),
            is_freeze=True,
        )

    @staticmethod
    def frame_session_time(location: SegmentReplayLocation) -> int | None:
        """Session time of the frame `location` points at, or None.

        Known only for exact-bounds segments (`frame_index` set).
        `PlaybackController.step_frames` snaps each step onto it so a run of
        steps can't drift off the recorded frames.
        """
        segment = location.segment
        period = _frame_period_ns(segment)
        if (
            period is None
            or location.frame_index is None
            or segment.start_session_time_ns is None
        ):
            return None
        return segment.start_session_time_ns + int(round(location.frame_index * period))

    @staticmethod
    def _first_frame(segment: Segment) -> SegmentReplayLocation:
        period = _frame_period_ns(segment)
        if period is not None:
            return _frame_location(segment, 0, period, is_freeze=True)
        return SegmentReplayLocation(segment=segment, offset_in_segment_ns=0, is_freeze=True)

    def earliest_session_time(self, feed_id: str) -> int | None:
        """Return the earliest `start_session_time_ns` for `feed_id`, or None.

        Phase 7.B-ext: when a per-game filter is active, only segments
        from the current game are considered.
        """
        if self._current_game_start_session_time_ns is None:
            return self._index.earliest_session_time(feed_id)
        for seg in self._index.all_for_feed(feed_id):
            if not self._segment_in_current_game(seg):
                continue
            if seg.start_session_time_ns is not None:
                return seg.start_session_time_ns
        return None

    def latest_replayable_session_time(self, feed_id: str) -> int | None:
        """Return the session time of the latest replayable frame, or None.

        That is a complete segment's `end_session_time_ns` for older rows
        (an inclusive end, the start of the last frame) and one frame
        before it for exact-bounds rows (whose end is exclusive), so the
        live edge is a real frame either way.

        Phase 7.B-ext: when a per-game filter is active, only segments
        from the current game are considered.
        """
        latest: int | None = None
        for seg in self._index.all_for_feed(feed_id):
            if seg.state != SEGMENT_STATE_COMPLETE:
                continue
            if not self._segment_in_current_game(seg):
                continue
            last_frame = _last_frame_session_time(seg)
            if last_frame is None:
                continue
            if latest is None or last_frame > latest:
                latest = last_frame
        return latest

    def available_session_time_range(self) -> tuple[int | None, int | None]:
        """Return `(earliest_start, latest_replayable_frame)` across all feeds.

        Operator-UI bounds for the replay timeline. Crosses feeds by
        design — the timeline is shared per §8.1. The upper bound is the
        latest replayable frame (see `latest_replayable_session_time`).
        Phase 7.B-ext: when a per-game filter is active, only segments
        from the current game are considered, so the operator can't
        rewind into a prior game's recording within the same session.
        """
        earliest: int | None = None
        latest: int | None = None
        for feed_id in self._index.feed_ids():
            for seg in self._index.all_for_feed(feed_id):
                if seg.start_session_time_ns is None or seg.end_session_time_ns is None:
                    continue
                if not self._segment_in_current_game(seg):
                    continue
                if earliest is None or seg.start_session_time_ns < earliest:
                    earliest = seg.start_session_time_ns
                if seg.state == SEGMENT_STATE_COMPLETE:
                    last_frame = _last_frame_session_time(seg)
                    if last_frame is not None and (latest is None or last_frame > latest):
                        latest = last_frame
        return earliest, latest

    def feeds_with_coverage_at(self, session_time_ns: int) -> list[str]:
        """Return feed_ids that have a completed segment covering this
        session time. Convenience pass-through for the multi-feed
        render path in 5.C. Phase 7.B-ext: filtered by the per-game
        scope when active.
        """
        if self._current_game_start_session_time_ns is None:
            return self._index.feeds_with_coverage_at(session_time_ns)
        out: list[str] = []
        for feed_id in self._index.feed_ids():
            for seg in self._index.all_for_feed(feed_id):
                if seg.state != SEGMENT_STATE_COMPLETE:
                    continue
                if seg.start_session_time_ns is None or seg.end_session_time_ns is None:
                    continue
                if not self._segment_in_current_game(seg):
                    continue
                if seg.start_session_time_ns <= session_time_ns <= seg.end_session_time_ns:
                    out.append(feed_id)
                    break
        return out
