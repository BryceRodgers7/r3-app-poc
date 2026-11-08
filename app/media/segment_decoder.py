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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Any

import numpy as np

from app.core.models import MediaFrame
from app.storage.segment_replay_store import SegmentReplayLocation

LOGGER = logging.getLogger(__name__)


# Skipping forward decodes every skipped frame (~4.5 ms each at 720p), so past
# this many it is cheaper to seek (~49 ms, i.e. about 11 decodes).
_MAX_SKIP_FRAMES = 8


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
    ) -> None:
        self._feed_id = feed_id
        self._source_name = source_name
        self._capture_factory = capture_factory or _default_capture_factory
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
        try:
            frame_bgr = self._read_frame(self._open, offset_ms)
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
        self._open = _OpenSegment(
            file_path=file_path,
            capture=capture,
            fps=fps,
            next_index=0 if fps else None,
        )
        return capture

    def _read_frame(self, seg: _OpenSegment, offset_ms: float) -> Any | None:
        """Return the BGR frame at `offset_ms`, seeking only when it must.

        Returns `None` when the capture has nothing to give (past EOF,
        corrupt frame), as `decode` always has.
        """
        target = None
        if seg.fps:
            # Same frame-number mapping OpenCV's own seek uses.
            target = int(offset_ms * seg.fps / 1000.0 + 0.5)
            if seg.last_image is not None and target == seg.last_index:
                return seg.last_image  # paused, or two ticks inside one frame
            if self._advance_sequentially(seg, target):
                return self._read_and_track(seg, target)
        self._seek_capture(seg.capture, offset_ms)
        return self._read_and_track(seg, target)

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


def _is_capture_open(capture: Any) -> bool:
    """Return True when the capture reports it has opened the file."""
    is_open = getattr(capture, "isOpened", None)
    if callable(is_open):
        try:
            return bool(is_open())
        except Exception:
            return False
    return True
