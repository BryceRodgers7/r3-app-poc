"""Read negotiated video dimensions from GStreamer caps before downstream scaling."""

from __future__ import annotations

import logging
import time
from typing import Any

LOGGER = logging.getLogger(__name__)


def parse_video_dimensions_fps_from_caps(caps: Any) -> tuple[int | None, int | None, float | None]:
    """Return width, height, and framerate from the first video structure in caps."""
    if caps is None or caps.get_size() == 0:
        return None, None, None
    structure = caps.get_structure(0)
    name = structure.get_name()
    if not name.startswith("video/"):
        return None, None, None

    w, h = None, None
    if structure.has_field("width"):
        ok, value = structure.get_int("width")
        if ok:
            w = int(value)
    if structure.has_field("height"):
        ok, value = structure.get_int("height")
        if ok:
            h = int(value)

    fps: float | None = None
    if structure.has_field("framerate"):
        try:
            ok, num, denom = structure.get_fraction("framerate")
            if ok and denom:
                fps = float(num) / float(denom)
        except Exception:
            LOGGER.debug("Could not parse framerate from caps structure.", exc_info=True)

    return w, h, fps


def poll_convert_sink_caps(
    convert: Any,
    *,
    timeout_ms: float = 800.0,
    poll_interval_ms: float = 25.0,
) -> tuple[int | None, int | None, float | None]:
    """Wait until videoconvert sink caps show a negotiated video size (decode → scale)."""
    pad = convert.get_static_pad("sink")
    if pad is None:
        return None, None, None

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        caps = pad.get_current_caps()
        if caps is not None and caps.get_size() > 0:
            w, h, fps = parse_video_dimensions_fps_from_caps(caps)
            if w is not None and h is not None:
                return w, h, fps
        time.sleep(poll_interval_ms / 1000.0)

    return None, None, None
