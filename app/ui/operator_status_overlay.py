"""Operator-window Play/Clip counter overlay (Phase 14.B).

Free-floating translucent label that sits over the upper-right of
the video panel. Shows the current play number and the 0-indexed
clip number derived from the ClipManager (via UiState). Mirrors the
pattern in `MultiFeedVideoPanel`'s playback-status pill: transparent
to mouse events, translucent background, positioned by the parent
window's `resizeEvent` rather than by a layout.

Outside of recording the overlay is hidden — there's no useful
counter to surface, and the operator's blank-state pane should
stay uncluttered.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget


class OperatorStatusOverlay(QLabel):
    """Top-right Play/Clip counter overlay for the operator window."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("operatorStatusOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.setStyleSheet(
            """
            QLabel#operatorStatusOverlay {
                background-color: rgba(24, 24, 24, 212);
                border: 1px solid #505050;
                border-radius: 10px;
                color: #f3f3f3;
                font-size: 16px;
                font-weight: 700;
                padding: 10px 14px;
            }
            """
        )
        self.hide()

    def set_counters(
        self,
        *,
        is_recording: bool,
        play_number: int | None,
        clip_number: int | None,
    ) -> None:
        """Update the overlay text.

        - Outside recording: hide entirely.
        - During recording: show two lines, "Play NNN" on top and
          "Clip NNN" beneath. "Play N/A" while in pre-game (before
          the first Next Play press). Clip is the 0-indexed counter
          from ClipManager — only None in the transient gap between
          Start press and ClipManager.start_game().
        """
        if not is_recording or clip_number is None:
            self.hide()
            self.setText("")
            return
        play_label = (
            f"Play {play_number}" if play_number is not None else "Play N/A"
        )
        clip_label = f"Clip {clip_number}"
        self.setText(f"{play_label}\n{clip_label}")
        self.adjustSize()
        self.show()
        self.raise_()
