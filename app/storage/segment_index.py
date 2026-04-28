"""In-memory `Segment` index for hot-path replay queries.

Phase 4.B. The SQLite `segments` table is the durable source of truth;
this class is the fast lookup layer for `(feed_id, t_start, t_end)` range
queries that fire on every "rewind 10 seconds" / replay-clock tick once
4.C is wired up. Per §6.4 — "SQLite: durable source of truth; In-memory
index: fast lookup for replay operations."

The index is feed-keyed; within each feed, segments are stored sorted by
`start_pts_ns`. Range queries use `bisect` for log-N lookup. `add()` is
expected at the rate of new segments (every ~4 seconds while recording),
not per buffer — no need to optimize for high-frequency insert.
"""

from __future__ import annotations

import bisect
import threading

from app.core.models import Segment, SEGMENT_STATE_COMPLETE


class SegmentIndex:
    """Per-feed timestamp-sorted view of `Segment` rows."""

    def __init__(self) -> None:
        self._by_feed: dict[str, list[Segment]] = {}
        self._lock = threading.Lock()

    def add(self, segment: Segment) -> None:
        """Insert `segment` into the per-feed list, keeping start_pts order."""
        with self._lock:
            feed_list = self._by_feed.setdefault(segment.feed_id, [])
            # Find the insertion point keyed on start_pts_ns.
            keys = [s.start_pts_ns for s in feed_list]
            idx = bisect.bisect_left(keys, segment.start_pts_ns)
            feed_list.insert(idx, segment)

    def remove_by_path(self, file_path: str) -> bool:
        """Remove the segment with the given file_path; return True if found."""
        with self._lock:
            for feed_id, feed_list in self._by_feed.items():
                for i, seg in enumerate(feed_list):
                    if seg.file_path == file_path:
                        feed_list.pop(i)
                        return True
        return False

    def all_for_feed(self, feed_id: str) -> list[Segment]:
        """Return every segment registered for `feed_id`, sorted by start_pts."""
        with self._lock:
            return list(self._by_feed.get(feed_id, []))

    def feed_ids(self) -> list[str]:
        """Return the set of feeds that have at least one segment registered."""
        with self._lock:
            return [fid for fid, segs in self._by_feed.items() if segs]

    def segments_overlapping(
        self, feed_id: str, t_start_ns: int, t_end_ns: int
    ) -> list[Segment]:
        """Return all segments whose `[start_pts_ns, end_pts_ns]` interval
        overlaps `[t_start_ns, t_end_ns]`. Sorted by start_pts."""
        if t_end_ns < t_start_ns:
            return []
        with self._lock:
            feed_list = self._by_feed.get(feed_id, [])
            if not feed_list:
                return []
            # Walk forward from the first segment that could overlap.
            # A segment with end_pts < t_start_ns can't overlap.
            # A segment with start_pts > t_end_ns can't overlap.
            results: list[Segment] = []
            for seg in feed_list:
                if seg.end_pts_ns < t_start_ns:
                    continue
                if seg.start_pts_ns > t_end_ns:
                    break
                results.append(seg)
            return results

    def earliest_pts(self, feed_id: str) -> int | None:
        """Return the earliest `start_pts_ns` recorded for `feed_id`, or None."""
        with self._lock:
            feed_list = self._by_feed.get(feed_id, [])
            return feed_list[0].start_pts_ns if feed_list else None

    def latest_replayable_pts(self, feed_id: str) -> int | None:
        """Return the latest `end_pts_ns` of any *complete* segment.

        Per §6.6 / §15.7, the in-progress (writing) segment is not
        replayable. Only `state == "complete"` segments count.
        """
        with self._lock:
            feed_list = self._by_feed.get(feed_id, [])
            latest: int | None = None
            for seg in feed_list:
                if seg.state != SEGMENT_STATE_COMPLETE:
                    continue
                if latest is None or seg.end_pts_ns > latest:
                    latest = seg.end_pts_ns
            return latest

    def clear(self) -> None:
        """Drop all segments. For test isolation."""
        with self._lock:
            self._by_feed.clear()
