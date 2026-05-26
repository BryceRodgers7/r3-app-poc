"""Scrubber slider for the referee window (Phase 14.C).

Seeks the shared replay clock that drives all tiles. One slider per
window (not per tile) — `PlaybackController.seek_to_session_time(ns)`
takes a session-time and renders every feed at that point via the
existing multi-feed render loop.

Behavior:
  - Live-updates from the controller's `_playback_session_time_ns`
    between user interactions (driven by MainWindow's `_render_state`
    callback).
  - While the user is dragging, live updates are suppressed so the
    handle doesn't fight the operator.
  - On release (or final value change), emits
    `seek_to_session_time_requested(target_ns)`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSlider, QWidget


class ScrubberSlider(QSlider):
    """Horizontal QSlider that emits seek requests in session-time ns."""

    # Resolution: one slider integer step == 10 ms of session-time.
    # Coarse enough to keep the slider's internal int range small for
    # multi-hour games (1 hr ≈ 360k steps); fine enough that single-px
    # drags land on frame-step boundaries at 30 fps.
    _NS_PER_STEP = 10_000_000  # 10 ms

    # Use qint64 — session-time-ns values exceed int32 range past
    # ~2.14 s of recording, and PySide6's `Signal(int)` maps to a
    # C++ 32-bit int which silently truncates large values.
    seek_to_session_time_requested = Signal("qint64")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimum(0)
        self.setMaximum(0)
        self.setSingleStep(1)
        self.setPageStep(50)  # ~500 ms — clicking the track jumps a half-second.
        self.setTracking(False)  # Only emit valueChanged on release.

        self._dragging = False
        self._range_earliest_ns: int | None = None
        self._range_latest_ns: int | None = None

        self.sliderPressed.connect(self._on_drag_started)
        self.sliderReleased.connect(self._on_drag_finished)
        self.valueChanged.connect(self._on_value_changed)

    def update_position(
        self,
        *,
        earliest_ns: int | None,
        latest_ns: int | None,
        current_ns: int | None,
    ) -> None:
        """Refresh range + handle position from PlaybackController state.

        Called from MainWindow's `_render_state` on every UiState push.
        No-ops the value-changed emission while updating so the live
        sync doesn't loop back into a seek request.

        While the operator is dragging, range updates are accepted but
        the handle position is left alone — the operator's drag wins.
        """
        if earliest_ns is None or latest_ns is None or latest_ns <= earliest_ns:
            # No replayable range yet — disable the widget so the
            # operator can see at a glance that scrubbing isn't
            # available (pre-game, no segments).
            self.setEnabled(False)
            self.blockSignals(True)
            try:
                self.setMinimum(0)
                self.setMaximum(0)
                self.setValue(0)
            finally:
                self.blockSignals(False)
            self._range_earliest_ns = None
            self._range_latest_ns = None
            return

        self.setEnabled(True)
        self._range_earliest_ns = int(earliest_ns)
        self._range_latest_ns = int(latest_ns)
        span_ns = self._range_latest_ns - self._range_earliest_ns
        max_step = max(1, span_ns // self._NS_PER_STEP)
        # blockSignals around range + value updates so this method
        # never indirectly emits a seek request.
        self.blockSignals(True)
        try:
            self.setMinimum(0)
            self.setMaximum(int(max_step))
            if not self._dragging and current_ns is not None:
                target = self._ns_to_step(current_ns)
                self.setValue(target)
        finally:
            self.blockSignals(False)

    def _ns_to_step(self, ns: int) -> int:
        if self._range_earliest_ns is None:
            return 0
        offset_ns = max(0, int(ns) - self._range_earliest_ns)
        return min(self.maximum(), offset_ns // self._NS_PER_STEP)

    def _step_to_ns(self, step: int) -> int:
        base = self._range_earliest_ns or 0
        return base + int(step) * self._NS_PER_STEP

    def _on_drag_started(self) -> None:
        self._dragging = True

    def _on_drag_finished(self) -> None:
        self._dragging = False
        # `setTracking(False)` defers valueChanged until release, so
        # the controller-bound seek lands here once per drag.
        self.seek_to_session_time_requested.emit(self._step_to_ns(self.value()))

    def _on_value_changed(self, value: int) -> None:
        # Triggers for keyboard arrows / page-up / track-click too —
        # those don't go through sliderPressed/Released, so emit here.
        # During an in-progress drag, `_dragging` is True and the
        # release handler will emit the final value; ignore the
        # intermediate signal here to keep the contract "one seek
        # request per gesture".
        if self._dragging:
            return
        self.seek_to_session_time_requested.emit(self._step_to_ns(value))
