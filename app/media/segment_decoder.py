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


@dataclass(slots=True)
class _OpenSegment:
    """A `cv2.VideoCapture` paired with the segment file path it points at."""

    file_path: str
    capture: Any  # cv2.VideoCapture; typed loosely so cv2 stays an optional import.


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
            self._seek_capture(capture, offset_ms)
            ok, frame_bgr = capture.read()
        except Exception:
            LOGGER.exception("SegmentDecoder.read failed for %s", file_path)
            self._failed_paths.add(file_path)
            self._close_locked()
            return None
        if not ok or frame_bgr is None:
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
        self._open = _OpenSegment(file_path=file_path, capture=capture)
        return capture

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


def _is_capture_open(capture: Any) -> bool:
    """Return True when the capture reports it has opened the file."""
    is_open = getattr(capture, "isOpened", None)
    if callable(is_open):
        try:
            return bool(is_open())
        except Exception:
            return False
    return True
