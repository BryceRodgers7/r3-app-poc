"""Qt signal objects used to decouple the UI from controllers."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class AppSignals(QObject):
    """Shared signals emitted when placeholder application state changes."""

    state_changed = Signal(object)
    status_message = Signal(str)
