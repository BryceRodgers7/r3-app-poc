"""One worker thread per feed that prepares replay frames, newest request first.

Replay used to decode and scale every feed inside the UI thread's timer tick, so
a slow tick meant a stalled UI and a slower timer. Here the tick only hands the
frame it wants to this worker and returns; the worker does the decode and the
scale-to-tile on its own thread, and the UI thread takes the finished picture
and gives it to Qt.

"Latest wins": the worker holds at most one waiting request. A newer request
replaces an older one that has not started yet (a dropped frame, counted), so
the work never queues up behind a slow decode; it just skips ahead. The same
goes for results: the UI thread takes the newest finished frame, and an older
one it never got to is replaced (also counted). Nothing here knows about Qt;
`on_ready` is how the owner wakes the UI thread.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
import time
from typing import Any

LOGGER = logging.getLogger(__name__)


class ReplayFrameWorker:
    """Runs `work(request)` on a thread for the newest request; keeps the newest result."""

    def __init__(
        self,
        name: str,
        work: Callable[[Any], Any | None],
        on_ready: Callable[[], None],
    ) -> None:
        self._name = name
        self._work = work
        self._on_ready = on_ready
        self._cond = threading.Condition()
        self._pending: tuple[Any, int] | None = None
        self._finished: tuple[int, Any] | None = None
        # True from the moment `on_ready` is called until `take()` runs, so a
        # burst of results wakes the UI thread once, not once per frame.
        self._notified = False
        self._stopped = False
        self._thread: threading.Thread | None = None
        # Counters for the periodic replay stats line.
        self.submitted = 0
        self.decoded = 0  # frames the worker finished
        self.shown = 0  # frames the UI thread took
        self.dropped_before_decode = 0  # replaced while waiting for the worker
        self.dropped_before_show = 0  # finished, replaced before the UI took it
        self.failed = 0  # work() returned None or raised
        self.work_seconds = 0.0

    # --- called from the UI thread -------------------------------------

    def submit(self, request: Any, epoch: int) -> None:
        """Ask for `request`, replacing any request still waiting. Returns at once."""
        with self._cond:
            if self._stopped:
                return
            if self._pending is not None:
                self.dropped_before_decode += 1
            self._pending = (request, epoch)
            self.submitted += 1
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=self._name, daemon=True
                )
                self._thread.start()
            self._cond.notify()

    def take(self) -> tuple[int, Any] | None:
        """The newest finished `(epoch, result)`, or None. Clears the wake-up flag."""
        with self._cond:
            result, self._finished = self._finished, None
            self._notified = False
            if result is not None:
                self.shown += 1
            return result

    def cancel(self) -> None:
        """Forget the waiting request and any finished-but-untaken result.

        A request already being worked on still finishes; its result carries
        the epoch it was submitted under, so the owner can recognise and
        discard it.
        """
        with self._cond:
            self._pending = None
            self._finished = None
            self._notified = False

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the thread and wait for it to end (it may be mid-decode)."""
        with self._cond:
            self._stopped = True
            self._pending = None
            self._finished = None
            thread = self._thread
            self._cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def stats(self) -> dict[str, float]:
        with self._cond:
            return {
                "submitted": self.submitted,
                "decoded": self.decoded,
                "shown": self.shown,
                "dropped_before_decode": self.dropped_before_decode,
                "dropped_before_show": self.dropped_before_show,
                "failed": self.failed,
                "work_seconds": self.work_seconds,
            }

    # --- the worker thread ---------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                request, epoch = self._pending
                self._pending = None
            started = time.perf_counter()
            try:
                result = self._work(request)
            except Exception:
                LOGGER.exception("%s: preparing a replay frame failed", self._name)
                result = None
            elapsed = time.perf_counter() - started
            wake = False
            with self._cond:
                self.work_seconds += elapsed
                if self._stopped:
                    return
                if result is None:
                    self.failed += 1
                else:
                    self.decoded += 1
                    if self._finished is not None:
                        self.dropped_before_show += 1
                    self._finished = (epoch, result)
                    if not self._notified:
                        self._notified = True
                        wake = True
            if wake:
                try:
                    self._on_ready()
                except Exception:
                    LOGGER.exception("%s: waking the UI thread failed", self._name)
