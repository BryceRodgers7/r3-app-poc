"""Unified GStreamer bus-message logging.

Every bus message from every per-feed pipeline (live ingest, replay buffer,
replay audio, recording, ...) is routed through `log_bus_message` so logs can
be filtered or correlated by `feed_id` and `pipeline_role`.

The function is deliberately stdlib-only — `pipeline_manager` extracts the
Gst-specific bits (parse_error / parse_warning / parse_info) and hands the
plain strings here. That keeps the helper trivially unit-testable without
GStreamer present.
"""

from __future__ import annotations

import logging
from typing import Final

LOGGER: Final = logging.getLogger("app.gst_bus")

_LEVEL_BY_TYPE: Final[dict[str, int]] = {
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "EOS": logging.INFO,
    "STATE_CHANGED": logging.DEBUG,
    "STREAM_START": logging.DEBUG,
    "QOS": logging.DEBUG,
}


def level_for_type(type_name: str) -> int:
    """Return the logging level used for a given GStreamer message type name."""
    return _LEVEL_BY_TYPE.get(type_name.upper(), logging.DEBUG)


def log_bus_message(
    *,
    feed_id: str,
    pipeline_role: str,
    type_name: str,
    summary: str | None = None,
    details: str | None = None,
) -> None:
    """Emit one structured log line for a GStreamer bus message.

    `pipeline_role` is a short identifier of which sub-pipeline the message
    came from (e.g. "live", "replay", "replay-audio", "recording").
    """
    level = level_for_type(type_name)
    LOGGER.log(
        level,
        "gstbus feed_id=%s role=%s type=%s summary=%s details=%s",
        feed_id or "?",
        pipeline_role or "?",
        type_name.upper(),
        summary or "",
        details or "",
    )
