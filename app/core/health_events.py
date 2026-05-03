"""Append-only health-event log.

`HealthEvent` matches the schema in `docs/r3_app_architecture.md` §14.7. Events
are persisted as one JSON object per line under the active session's
`logs/health_events.jsonl` so an operator (or post-session tool) can review
what happened during a game without parsing free-form log output.

The log is intentionally JSONL rather than SQLite for Phase 1 — it is a pure
diagnostics surface and avoids touching the existing storage layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import itertools
import json
import logging
from pathlib import Path
import threading
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)


class HealthSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class HealthEvent:
    """One operator-relevant health event.

    Field names mirror §14.7. `id` is assigned at record time, `created_at` is
    UTC ISO-8601, `metadata` is an arbitrary JSON-serializable dict.
    """

    id: int
    session_id: str | None
    feed_id: str | None
    severity: str
    category: str
    message: str
    created_at: str
    resolved_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class HealthEventLog:
    """Thread-safe append-only health-event log with optional file persistence.

    The log can be constructed without a path (records are still emitted to
    the standard logger) and later attached to a session via `open()`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._session_id: str | None = None
        self._path: Path | None = None
        self._file: Any = None
        self._last_categories: dict[tuple[str | None, str], int] = {}
        # Parallel to `_last_categories`. The Phase 10.A operator banner
        # needs the full event payload (severity + message) to render,
        # not just the id, so the open marker is mirrored here.
        self._open_events: dict[tuple[str | None, str], HealthEvent] = {}
        self._category_counts: dict[str, int] = {}

    def open(self, path: Path, session_id: str) -> None:
        """Begin appending to `path`. Creates parent dirs if needed."""
        path = Path(path)
        with self._lock:
            self._close_file_locked()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")
            self._path = path
            self._session_id = session_id
            self._last_categories.clear()
            self._open_events.clear()

    def close(self) -> None:
        with self._lock:
            self._close_file_locked()
            self._session_id = None
            self._path = None

    def record(
        self,
        *,
        severity: HealthSeverity | str,
        category: str,
        message: str,
        feed_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HealthEvent:
        """Record one event. Returns the persisted `HealthEvent`."""
        sev = severity.value if isinstance(severity, HealthSeverity) else str(severity)
        with self._lock:
            event = HealthEvent(
                id=next(self._counter),
                session_id=self._session_id,
                feed_id=feed_id,
                severity=sev,
                category=category,
                message=message,
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata=dict(metadata or {}),
            )
            if self._file is not None:
                try:
                    self._file.write(event.to_json_line() + "\n")
                    self._file.flush()
                except OSError:
                    LOGGER.exception("health event log write failed")
            self._last_categories[(feed_id, category)] = event.id
            self._open_events[(feed_id, category)] = event
            self._category_counts[category] = self._category_counts.get(category, 0) + 1
        log_level = {
            HealthSeverity.INFO.value: logging.INFO,
            HealthSeverity.WARNING.value: logging.WARNING,
            HealthSeverity.ERROR.value: logging.ERROR,
        }.get(sev, logging.INFO)
        LOGGER.log(
            log_level,
            "health_event id=%d feed_id=%s severity=%s category=%s message=%s",
            event.id,
            feed_id or "-",
            sev,
            category,
            message,
        )
        return event

    def category_count(self, category: str) -> int:
        """Return the lifetime count of events recorded under `category`."""
        with self._lock:
            return self._category_counts.get(category, 0)

    def has_open_event(self, *, category: str, feed_id: str | None) -> bool:
        """Return whether `(feed_id, category)` already has an event recorded.

        Producers use this to suppress repeat emissions while a condition
        persists. Phase 1 does not auto-resolve events; that is for later
        slices that build a richer state machine.
        """
        with self._lock:
            return (feed_id, category) in self._last_categories

    def clear_open_event(self, *, category: str, feed_id: str | None) -> None:
        """Forget the open marker for `(feed_id, category)` so a re-emission is
        allowed if the condition recurs."""
        with self._lock:
            self._last_categories.pop((feed_id, category), None)
            self._open_events.pop((feed_id, category), None)

    def open_events(self) -> list[HealthEvent]:
        """Return a snapshot of currently-open events, in id order.

        Phase 10.A consumes this to render the operator alert banner —
        each `(feed_id, category)` pair has at most one entry (the most
        recent record for that pair), and the entry is removed when
        `clear_open_event` fires."""
        with self._lock:
            return sorted(self._open_events.values(), key=lambda e: e.id)

    def _close_file_locked(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                LOGGER.debug("health event log close raised", exc_info=True)
            self._file = None


_DEFAULT_LOG = HealthEventLog()


def default_log() -> HealthEventLog:
    """Return the process-wide default `HealthEventLog`."""
    return _DEFAULT_LOG


def record_health_event(
    *,
    severity: HealthSeverity | str,
    category: str,
    message: str,
    feed_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> HealthEvent:
    """Record one event in the process-wide default log."""
    return _DEFAULT_LOG.record(
        severity=severity,
        category=category,
        message=message,
        feed_id=feed_id,
        metadata=metadata,
    )
