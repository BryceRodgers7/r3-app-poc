"""Exact recording-segment bounds from splitmuxsink's own reports.

docs/REPLAY_SEAM_GLITCH.md §7.4 step 1. A segment's bounds used to come
from a Python probe sampling the session clock at the segment's first
buffer. That sample is late by however long the encode took (jittery
under load), the probe ran two buffers ahead of splitmuxsink's cut, and
buffers seen while one segment was being swapped for the next belonged
to neither. Replay then found a hole at every seam and repeated or
skipped frames there.

This module keeps the bookkeeping in GStreamer's running time instead:

- A segment **starts** at the running time of the first buffer that
  splitmuxsink hands to `format-location-full` for its file.
- It **ends** (exclusive) where the next segment starts. That is the
  same value splitmuxsink reports as the `running-time` of
  `splitmuxsink-fragment-closed` for a file it rotated away from.
  Consecutive segments therefore meet exactly.
- Its **frame count** is the number of buffers whose running time falls
  in `[start, end)`, attributed by timestamp, not by when they passed.
- A segment is **finished** (ready for the database and replay) only
  once its file is closed: on its `fragment-closed` message, or when
  splitmuxsink is set to NULL (which closes every file).

The class has no GStreamer dependency, so the rules above are unit
tested directly. `PipelineManager` feeds it from the format-location
callback (streaming thread), the encoder probe (streaming thread) and
the bus thread, so every method takes the lock.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import os
import statistics
import threading

LOGGER = logging.getLogger(__name__)

# A fragment-closed `running-time` further than this from the end the
# next fragment's first buffer implied is logged (both should be equal;
# measured 0.0000 ms in docs/REPLAY_SEAM_GLITCH.md §7.2 E3 / E5).
_CLOSE_MISMATCH_WARN_NS = 1_000_000

# Upper bound on remembered buffer timestamps. A 30 s segment at 60 fps is
# 1,800 buffers; pruning normally keeps far fewer than this.
_MAX_TRACKED_BUFFERS = 16_384


def segment_path_key(path: str) -> str:
    """Normalize a segment path so splitmuxsink's `location` matches ours."""
    return os.path.normcase(os.path.normpath(str(path)))


@dataclass(slots=True)
class FragmentRecord:
    """One segment file splitmuxsink opened, while it is still being tracked.

    `running_time_to_monotonic_ns` maps the pipeline's running time onto
    `time.monotonic_ns()` (base time plus the pipeline clock's offset to
    the monotonic clock), captured when the file opened. `None` when no
    pipeline clock was available.
    """

    session_id: str
    feed_id: str
    fragment_index: int
    file_path: str
    created_at: str
    start_running_time_ns: int
    start_pts_ns: int
    running_time_to_monotonic_ns: int | None = None
    end_running_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class FinishedFragment:
    """A segment whose file is closed and whose bounds are final."""

    record: FragmentRecord
    end_running_time_ns: int
    frame_count: int

    @property
    def duration_ns(self) -> int:
        return max(0, self.end_running_time_ns - self.record.start_running_time_ns)


class SegmentBookkeeper:
    """Tracks the open segment, segments waiting for their close message,
    and the running times of recent encoded buffers."""

    def __init__(self, *, max_tracked_buffers: int = _MAX_TRACKED_BUFFERS) -> None:
        self._lock = threading.Lock()
        self._current: FragmentRecord | None = None
        # Rotated away from, waiting for `fragment-closed` (keyed by path).
        self._closing: dict[str, FragmentRecord] = {}
        # Close messages for the current file, seen before it was rotated
        # away from (message order across threads is not guaranteed).
        self._closed_early: set[str] = set()
        # Files we stopped tracking on purpose (post-Stop throwaway);
        # their close message is expected and ignored.
        self._ignored: set[str] = set()
        self._buffer_running_times: deque[int] = deque(maxlen=max_tracked_buffers)

    # -- streaming thread -------------------------------------------------

    def note_buffer(self, running_time_ns: int) -> None:
        """Record one encoded buffer heading for splitmuxsink."""
        with self._lock:
            times = self._buffer_running_times
            if times and running_time_ns < times[-1]:
                # Running time went backwards: a new timeline (should only
                # follow a reset). Old timestamps can't be attributed to it.
                times.clear()
            times.append(int(running_time_ns))

    def open_fragment(self, record: FragmentRecord) -> FinishedFragment | None:
        """splitmuxsink opened `record`'s file; the previous one ends here.

        Returns the previous segment as finished when its close message
        already arrived; otherwise it waits for `fragment_closed`.
        """
        with self._lock:
            previous = self._current
            self._current = record
            if previous is None:
                return None
            previous.end_running_time_ns = record.start_running_time_ns
            key = segment_path_key(previous.file_path)
            if key in self._closed_early:
                self._closed_early.discard(key)
                return self._finish_locked(previous, previous.end_running_time_ns)
            self._closing[key] = previous
            return None

    # -- bus thread -------------------------------------------------------

    def fragment_closed(
        self, location: str, running_time_ns: int | None = None
    ) -> FinishedFragment | None:
        """splitmuxsink reported `location` closed. Returns it when finished."""
        key = segment_path_key(location)
        with self._lock:
            if key in self._ignored:
                self._ignored.discard(key)
                return None
            record = self._closing.pop(key, None)
            if record is None:
                current = self._current
                if current is not None and segment_path_key(current.file_path) == key:
                    self._closed_early.add(key)
                # Otherwise a file we never tracked (unrouted, or a
                # duplicate message): nothing to do.
                return None
            end = record.end_running_time_ns
            assert end is not None  # set when it moved to `_closing`
            if (
                running_time_ns is not None
                and abs(int(running_time_ns) - end) > _CLOSE_MISMATCH_WARN_NS
            ):
                LOGGER.warning(
                    "segment %s: fragment-closed running-time %.3f ms differs from "
                    "the next segment's start %.3f ms; keeping the latter",
                    os.path.basename(record.file_path),
                    int(running_time_ns) / 1e6,
                    end / 1e6,
                )
            return self._finish_locked(record, end)

    # -- Stop / teardown (splitmuxsink at NULL: every file is closed) -----

    def finish_closing(self) -> list[FinishedFragment]:
        """Finish every segment still waiting for its close message."""
        with self._lock:
            records = list(self._closing.values())
            self._closing.clear()
            finished = []
            for record in records:
                end = record.end_running_time_ns
                assert end is not None
                finished.append(self._finish_locked(record, end))
            return finished

    def finish_current(self, *, nominal_frame_duration_ns: int) -> FinishedFragment | None:
        """Finish the open segment, which nothing rotated away from.

        Used when Stop's split did not happen, and on teardown. There is no
        next segment to end it, so its end is its last buffer plus one
        frame. (splitmuxsink's own close message for a file closed this way
        reports the *start* of the last frame, docs/REPLAY_SEAM_GLITCH.md
        §7.2 E3, so it can't be used either.) Returns None when no buffer
        was attributed to it.
        """
        with self._lock:
            record = self._current
            self._current = None
            if record is None:
                return None
            self._closed_early.discard(segment_path_key(record.file_path))
            mine = [
                rt for rt in self._buffer_running_times
                if rt >= record.start_running_time_ns
            ]
            if not mine:
                return None
            frame_ns = _median_step_ns(mine) or max(1, int(nominal_frame_duration_ns))
            return self._finish_locked(record, mine[-1] + frame_ns)

    def drop_current(self) -> FragmentRecord | None:
        """Stop tracking the open segment without finishing it."""
        with self._lock:
            record = self._current
            self._current = None
            if record is not None:
                key = segment_path_key(record.file_path)
                self._closed_early.discard(key)
                self._ignored.add(key)
            return record

    def current(self) -> FragmentRecord | None:
        with self._lock:
            return self._current

    def reset(self) -> None:
        """Forget everything (a new pipeline restarts running time at 0)."""
        with self._lock:
            self._current = None
            self._closing.clear()
            self._closed_early.clear()
            self._ignored.clear()
            self._buffer_running_times.clear()

    # -- internals --------------------------------------------------------

    def _finish_locked(self, record: FragmentRecord, end_ns: int) -> FinishedFragment:
        start = record.start_running_time_ns
        record.end_running_time_ns = end_ns
        frame_count = sum(1 for rt in self._buffer_running_times if start <= rt < end_ns)
        self._prune_locked()
        return FinishedFragment(record=record, end_running_time_ns=end_ns, frame_count=frame_count)

    def _prune_locked(self) -> None:
        """Drop buffer timestamps no tracked segment can still claim."""
        starts = [r.start_running_time_ns for r in self._closing.values()]
        if self._current is not None:
            starts.append(self._current.start_running_time_ns)
        times = self._buffer_running_times
        if not starts:
            return  # keep them: the next segment's first buffers may be here
        floor = min(starts)
        while times and times[0] < floor:
            times.popleft()


def _median_step_ns(running_times: list[int]) -> int | None:
    """Median spacing of the last few buffers, or None with fewer than two."""
    tail = running_times[-17:]
    steps = [b - a for a, b in zip(tail, tail[1:]) if b > a]
    if not steps:
        return None
    return int(statistics.median(steps))
