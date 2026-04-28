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
