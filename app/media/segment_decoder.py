"""Per-feed segment-file decoder for replay playback (slice 4.C.tail).

Wraps `cv2.VideoCapture` with a small amount of state so the
`PlaybackController`'s replay clock can decode `MediaFrame`s out of the
recorded segment files without paying the cost of opening + closing the
file on every tick.

Design choices (per the §4.C.tail trade-offs in
`docs/r3_app_architecture.md`):

- **Python QImage path**, not d3d11videosink. The recorded segments are
  intra-frame MJPEG; `cv2.VideoCapture` decodes a 720p frame in a few
  milliseconds, well within the 40ms replay-tick budget. This avoids
  reopening the 3.A.3 native-binding bug.
- **Persistent `cv2.VideoCapture` per feed.** Closed and reopened only
  when the resolver hands us a different segment file, or when replay
  ends.
- **Frame-accurate seeking via `CAP_PROP_POS_MSEC`** at request time;
  `cv2.VideoCapture` will land on the keyframe at-or-before the
  requested offset (every JPEG is a keyframe, so this is exact).
- **Sequential reads while playing.** OpenCV's FFmpeg seek steps back ~16
  frames and decodes forward to the target, so a seek per tick cost ~49 ms
  on a 720p60 segment against ~4.5 ms for a plain read, and capped replay at
  ~8 fps for two feeds. Normal playback only ever wants the next frame or
  two, so the decoder tracks the capture's position and reads (or skips
  forward) instead, seeking only for a real jump: rewind, scrub, a new
  segment. Repeat requests for the frame already shown decode nothing.
- **Stateless w.r.t. playback rate.** The controller advances the PTS
  clock at `rate * elapsed`; the decoder just renders whatever offset it
  is asked for.
- **Frame index from the store when it has one.** Segments with exact
  bounds (`Segment.end_exclusive`) come with `location.frame_index`, the
  frame the store resolved; the decoder reads that frame rather than
  re-deriving it from the offset and the container's declared rate.
- **Never past the end.** A request beyond the file's real last frame
  returns that last frame (and remembers how many frames the file has)
  instead of `None`, which used to leave the previous frame on screen.
  See docs/REPLAY_SEAM_GLITCH.md §2.3 / §7.4 step 2.
- **Positioned by the file's own timestamps.** On open, the decoder reads
  every frame's timestamp from the mkv block headers (`mkv_timestamps`,
  a few ms, no decoding). OpenCV's seek finds frame k as `k / declared
  fps`, which is a neighbouring frame when frames aren't evenly spaced
  (the synthetic source, older recordings). With the timestamps, a seek
  aims just before frame k's real timestamp, checks where it landed
  (`CAP_PROP_POS_MSEC`) and reads forward to k. Without them (other
  containers, unreadable files, test stubs) the frame-rate arithmetic
  above is used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Any

import bisect

import numpy as np

from app.core.models import MediaFrame
from app.media.mkv_timestamps import read_video_timestamps_ms
from app.storage.segment_replay_store import SegmentReplayLocation

LOGGER = logging.getLogger(__name__)


# Skipping forward decodes every skipped frame (~4.5 ms each at 720p), so past
# this many it is cheaper to seek (~49 ms, i.e. about 11 decodes).
_MAX_SKIP_FRAMES = 8

# A file whose seeks don't work is read from its start instead, up to this
# many frames (a 4 s segment at 60 fps is 240).
_MAX_READ_FROM_START_FRAMES = 300

# When a read lands past the end of a file, how many earlier frames to try
# for its real last frame. Exact bounds are off by at most one frame; older
# rows by two or three (docs/REPLAY_SEAM_GLITCH.md §2.3).
_MAX_END_BACKTRACK_FRAMES = 3


@dataclass(slots=True)
class _OpenSegment:
    """A `cv2.VideoCapture` paired with the segment file path it points at.

    `fps` and the index fields let `decode` read sequentially instead of
    seeking. They stay `None` when the backend can't report a frame rate (as
    with the test stubs), which keeps the original seek-every-time behaviour.
    """

    file_path: str
    capture: Any  # cv2.VideoCapture; typed loosely so cv2 stays an optional import.
    fps: float | None = None
    next_index: int | None = None  # index the next read() returns
    last_index: int | None = None  # index of `last_image`
    last_image: Any = None  # the frame most recently returned
    frame_total: int | None = None  # frames in the file, once a read past the end found it
    # Every frame's timestamp (ms from the first frame), from the file.
    timestamps_ms: list[float] | None = None
    # OpenCV reports a bogus CAP_PROP_POS_MSEC after the first `read()` of
    # a capture's life, even after a seek; one read first makes it reliable.
    primed: bool = False
    # How many frames before the target a seek aims. Grows once a seek in
    # this file overshoots, so later seeks don't pay for a second try.
    seek_frames_back: float = 0.4


class SegmentDecoder:
    """Decode `MediaFrame`s out of recorded segment files for replay.

    Holds at most one open `cv2.VideoCapture` per instance. Callers pass
    a `SegmentReplayLocation` (segment + offset_in_segment_ns) and get
    back a `MediaFrame` whose `image` is the decoded BGR pixels.

    Returns `None` when the underlying capture cannot decode a frame at
    the requested offset (corrupt file, offset past EOF, etc.). Failures
    are logged once per file path so a flaky segment doesn't spam logs.

    The constructor takes an optional `capture_factory` so tests can
    inject a stub `VideoCapture` without depending on the real cv2
    backend.
    """

    def __init__(
        self,
        feed_id: str,
        source_name: str,
        *,
        capture_factory: Callable[[str], Any] | None = None,
        timestamp_reader: Callable[[str], list[float] | None] | None = None,
    ) -> None:
        self._feed_id = feed_id
        self._source_name = source_name
        self._capture_factory = capture_factory or _default_capture_factory
        self._timestamp_reader = timestamp_reader or read_video_timestamps_ms
        self._open: _OpenSegment | None = None
        self._failed_paths: set[str] = set()

    def decode(self, location: SegmentReplayLocation) -> MediaFrame | None:
        """Decode the frame at `location.offset_in_segment_ns` of `location.segment.file_path`.

        Returns `None` when the segment can't be opened or decoded. The
        caller (the replay clock) should fall back to its previous
        behavior — typically "keep the last shown frame".
        """
        file_path = location.segment.file_path
        if file_path in self._failed_paths:
            return None
        capture = self._ensure_open(file_path)
        if capture is None:
            return None
        offset_ms = max(0.0, location.offset_in_segment_ns / 1_000_000.0)
        frame_index = getattr(location, "frame_index", None)
        try:
            frame_bgr = self._read_frame(self._open, offset_ms, frame_index)
        except Exception:
            LOGGER.exception("SegmentDecoder.read failed for %s", file_path)
            self._failed_paths.add(file_path)
            self._close_locked()
            return None
        if frame_bgr is None:
            return None
        # `cv2.VideoCapture.read` returns the next frame at-or-after the
        # seek target; for MJPEG every frame is a keyframe so the
        # delivered frame is the requested one.
        image = np.ascontiguousarray(frame_bgr)
        return MediaFrame(
            frame_id=int(time.monotonic_ns()),  # purely cosmetic; replay frames don't sequence into ingest IDs
            timestamp=time.time(),
            image=image,
            source_name=self._source_name,
            feed_id=self._feed_id,
        )

    def close(self) -> None:
        """Release the underlying capture, if any.

        Idempotent. `decode()` will reopen on the next call.
        """
        self._close_locked()

    def _ensure_open(self, file_path: str) -> Any | None:
        if self._open is not None and self._open.file_path == file_path:
            return self._open.capture
        # Different file (or first call) — close any existing handle and
        # open the new one.
        self._close_locked()
        if not Path(file_path).exists():
            LOGGER.warning("SegmentDecoder: file does not exist: %s", file_path)
            self._failed_paths.add(file_path)
            return None
        try:
            capture = self._capture_factory(file_path)
        except Exception:
            LOGGER.exception("SegmentDecoder: capture_factory raised for %s", file_path)
            self._failed_paths.add(file_path)
            return None
        if capture is None or not _is_capture_open(capture):
            LOGGER.warning("SegmentDecoder: failed to open %s", file_path)
            self._failed_paths.add(file_path)
            return None
        fps = _capture_fps(capture)
        timestamps_ms: list[float] | None = None
        try:
            timestamps_ms = self._timestamp_reader(file_path)
        except Exception:
            LOGGER.debug("SegmentDecoder: timestamp read failed for %s", file_path, exc_info=True)
        self._open = _OpenSegment(
            file_path=file_path,
            capture=capture,
            fps=fps,
            next_index=0 if (fps or timestamps_ms) else None,
            frame_total=len(timestamps_ms) if timestamps_ms else None,
            timestamps_ms=timestamps_ms or None,
        )
        return capture

    def _read_frame(
        self, seg: _OpenSegment, offset_ms: float, frame_index: int | None = None
    ) -> Any | None:
        """Return the BGR frame at `frame_index` (or `offset_ms`), seeking only when it must.

        A target past the file's real end returns the last frame. Returns
        `None` only when the capture has nothing to give (corrupt file, or
        a target far beyond the end).
        """
        if seg.timestamps_ms is not None:
            return self._read_frame_by_timestamps(seg, offset_ms, frame_index)
        if not seg.fps:
            self._seek_capture(seg.capture, offset_ms)
            return self._read_and_track(seg, None)
        if frame_index is not None:
            target = max(0, int(frame_index))
        else:
            # Same frame-number mapping OpenCV's own seek uses.
            target = int(offset_ms * seg.fps / 1000.0 + 0.5)
        if seg.frame_total is not None and target >= seg.frame_total:
            target = seg.frame_total - 1
        if seg.last_image is not None and target == seg.last_index:
            return seg.last_image  # paused, or two ticks inside one frame
        previous_index, previous_image = seg.last_index, seg.last_image
        if self._advance_sequentially(seg, target):
            frame = self._read_and_track(seg, target)
        else:
            self._seek_capture(seg.capture, target * 1000.0 / seg.fps)
            frame = self._read_and_track(seg, target)
        if frame is None and target > 0:
            frame = self._read_last_frame(seg, target, previous_index, previous_image)
        return frame

    def _read_frame_by_timestamps(
        self, seg: _OpenSegment, offset_ms: float, frame_index: int | None
    ) -> Any | None:
        """Frame `frame_index` (or the one nearest `offset_ms`), using the file's timestamps."""
        stamps = seg.timestamps_ms
        assert stamps is not None
        last = len(stamps) - 1
        if frame_index is not None:
            target = min(max(0, int(frame_index)), last)
        else:
            target = _nearest_index(stamps, offset_ms)
        if seg.last_image is not None and target == seg.last_index:
            return seg.last_image
        if seg.next_index is not None and 0 <= target - seg.next_index <= _MAX_SKIP_FRAMES:
            if self._walk_to(seg, seg.next_index, target):
                return seg.last_image
        return self._seek_to_frame(seg, target)

    def _seek_to_frame(self, seg: _OpenSegment, target: int) -> Any | None:
        """Seek so the next read is frame `target`, checking where the seek landed."""
        stamps = seg.timestamps_ms
        assert stamps is not None
        if not seg.primed:
            seg.capture.read()  # a read, not a grab: it's the first read that misreports
            seg.primed = True
        spacing = _local_spacing_ms(stamps, target)
        # Aim just before the frame (lands on it when frames are evenly
        # spaced), then further back if OpenCV overshot.
        attempts = [b for b in (0.4, 3.0, 8.0) if b >= seg.seek_frames_back]
        for frames_back in attempts:
            seg.seek_frames_back = frames_back
            aim_ms = max(0.0, stamps[target] - frames_back * spacing)
            self._seek_capture(seg.capture, aim_ms)
            ok, frame_bgr = seg.capture.read()
            if not ok or frame_bgr is None:
                continue
            landed = _index_at(seg.capture, stamps)
            if landed is None:
                # The capture can't say where it is: trust the seek, as the
                # frame-rate path does, and don't read on from here.
                seg.next_index = None
                seg.last_index, seg.last_image = target, frame_bgr
                return frame_bgr
            if landed > target:
                continue
            seg.last_index, seg.last_image = landed, frame_bgr
            seg.next_index = landed + 1
            if landed == target or self._walk_to(seg, landed + 1, target):
                return seg.last_image
        # Seeking isn't working in this file (one cut short by a crash has
        # no index, and OpenCV then just reads on): read it from the start.
        return self._read_from_start(seg, target)

    def _read_from_start(self, seg: _OpenSegment, target: int) -> Any | None:
        seg.next_index = seg.last_index = seg.last_image = None
        if target > _MAX_READ_FROM_START_FRAMES:
            LOGGER.debug(
                "SegmentDecoder: cannot position on frame %d of %s", target, seg.file_path
            )
            return None
        try:
            capture = self._capture_factory(seg.file_path)
        except Exception:
            return None
        if capture is None or not _is_capture_open(capture):
            return None
        try:
            seg.capture.release()
        except Exception:
            LOGGER.debug("SegmentDecoder: capture.release raised", exc_info=True)
        seg.capture = capture
        seg.primed = False
        if self._walk_to(seg, 0, target):
            return seg.last_image
        return None

    def _walk_to(self, seg: _OpenSegment, next_index: int, target: int) -> bool:
        """Read forward from `next_index` (the next read's frame) to `target`."""
        for _ in range(target - next_index):
            if not seg.capture.grab():
                seg.next_index = seg.last_index = seg.last_image = None
                return False
        ok, frame_bgr = seg.capture.read()
        seg.primed = True
        if not ok or frame_bgr is None:
            seg.next_index = seg.last_index = seg.last_image = None
            return False
        seg.last_index, seg.last_image = target, frame_bgr
        seg.next_index = target + 1
        return True

    def _read_last_frame(
        self,
        seg: _OpenSegment,
        failed_target: int,
        previous_index: int | None,
        previous_image: Any,
    ) -> Any | None:
        """Frame `failed_target` doesn't exist: find and return the file's last frame."""
        for candidate in range(failed_target - 1, failed_target - 1 - _MAX_END_BACKTRACK_FRAMES, -1):
            if candidate < 0:
                break
            if previous_image is not None and candidate == previous_index:
                frame = previous_image  # stepping forward off the end
                seg.last_index, seg.last_image = candidate, frame
            else:
                self._seek_capture(seg.capture, candidate * 1000.0 / seg.fps)
                frame = self._read_and_track(seg, candidate)
            if frame is not None:
                seg.frame_total = candidate + 1
                return frame
        return None

    def _advance_sequentially(self, seg: _OpenSegment, target: int) -> bool:
        """Skip forward so `read()` returns frame `target`; False if a seek is needed."""
        if seg.next_index is None:
            return False
        gap = target - seg.next_index
        if gap < 0 or gap > _MAX_SKIP_FRAMES:
            return False  # backwards, or far enough that seeking is cheaper
        for _ in range(gap):
            if not seg.capture.grab():
                seg.next_index = None  # lost our place; the caller seeks
                return False
            seg.next_index += 1
        return True

    def _read_and_track(self, seg: _OpenSegment, target: int | None) -> Any | None:
        ok, frame_bgr = seg.capture.read()
        if not ok or frame_bgr is None:
            seg.next_index = seg.last_index = seg.last_image = None
            return None
        if target is None:
            # No frame rate: we can't map positions to indices, so always seek.
            seg.next_index = seg.last_index = None
            return frame_bgr
        # Trust the capture's own counter over our arithmetic when it has one.
        position = _capture_position(seg.capture)
        seg.next_index = position if position is not None else target + 1
        seg.last_index = seg.next_index - 1
        seg.last_image = frame_bgr
        return frame_bgr

    def _seek_capture(self, capture: Any, offset_ms: float) -> None:
        """Seek the capture to the requested offset.

        Uses `CAP_PROP_POS_MSEC` which is the most portable seek mode on
        cv2 + FFmpeg + MJPEG-MKV. Some backends require a small re-read
        after seeking; this method only sets the position and lets the
        caller `read()`.
        """
        try:
            import cv2  # noqa: WPS433 — local import keeps cv2 optional for stub-driven tests.
            capture.set(cv2.CAP_PROP_POS_MSEC, float(offset_ms))
        except Exception:
            # Stubs may not expose CAP_PROP_POS_MSEC; fall back to a
            # `set("pos_msec", ...)` style if the stub provides it.
            try:
                capture.set("pos_msec", float(offset_ms))
            except Exception:
                LOGGER.debug(
                    "SegmentDecoder: capture.set failed for offset %.1fms",
                    offset_ms,
                )

    def _close_locked(self) -> None:
        if self._open is None:
            return
        try:
            self._open.capture.release()
        except Exception:
            LOGGER.debug("SegmentDecoder: capture.release raised", exc_info=True)
        self._open = None


def _default_capture_factory(file_path: str) -> Any:
    """Open a cv2.VideoCapture against `file_path` (FFmpeg backend)."""
    import cv2

    return cv2.VideoCapture(str(file_path))


def _capture_fps(capture: Any) -> float | None:
    """The container's frame rate, or None if the backend can't say."""
    try:
        import cv2

        fps = float(capture.get(cv2.CAP_PROP_FPS))
    except Exception:
        return None
    return fps if 1.0 <= fps <= 240.0 else None


def _capture_position(capture: Any) -> int | None:
    """Index of the frame the next `read()` will return, per the capture itself."""
    try:
        import cv2

        position = capture.get(cv2.CAP_PROP_POS_FRAMES)
    except Exception:
        return None
    if position is None or position < 0:
        return None
    return int(position)


def _nearest_index(stamps: list[float], offset_ms: float) -> int:
    """Index of the timestamp nearest `offset_ms`."""
    i = bisect.bisect_left(stamps, offset_ms)
    if i <= 0:
        return 0
    if i >= len(stamps):
        return len(stamps) - 1
    return i if stamps[i] - offset_ms < offset_ms - stamps[i - 1] else i - 1


def _local_spacing_ms(stamps: list[float], index: int) -> float:
    if len(stamps) < 2:
        return 33.4
    if index > 0:
        return max(1.0, stamps[index] - stamps[index - 1])
    return max(1.0, stamps[1] - stamps[0])


def _index_at(capture: Any, stamps: list[float]) -> int | None:
    """Which frame the capture just read, from its reported timestamp."""
    try:
        import cv2

        position_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC))
    except Exception:
        return None
    index = _nearest_index(stamps, position_ms)
    if abs(stamps[index] - position_ms) > 0.5 * _local_spacing_ms(stamps, index):
        return None
    return index


def _is_capture_open(capture: Any) -> bool:
    """Return True when the capture reports it has opened the file."""
    is_open = getattr(capture, "isOpened", None)
    if callable(is_open):
        try:
            return bool(is_open())
        except Exception:
            return False
    return True
