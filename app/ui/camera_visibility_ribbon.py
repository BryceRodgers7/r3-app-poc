"""Camera show/hide ribbon (Phase 14.B).

One checkable button per feed, labeled with the 1-based feed index
to match `docs/window-layouts.pdf`. Toggles emit
`feed_visibility_toggled(feed_id, visible)` so the caller
(`MainWindow`) can drive `MultiFeedVideoPanel.set_tile_visible`.

State is per-window: the operator hiding camera 3 does NOT hide it
on the referee window, and recording / ingest continue normally for
hidden feeds. The ribbon is purely a UI toggle.

The PDF mock shows a "Clip selector widget" label beside the camera
buttons. Phase 14.B renders that as a non-interactive QLabel showing
the current clip type/number; the future enhancement (clicking it to
browse past clips) is out of scope. It sits above the buttons now,
at the top of the window's control rail — the readout is the on-screen
result of Next Play, so it has to be easy to see.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.models import CLIP_TYPE_PLAY, FeedDefinition
from app.ui.control_button import ControlButton


_BUTTONS_PER_ROW = 4
_BUTTON_HEIGHT = 44
_SELECTOR_LABEL_STYLE = (
    "QLabel { color: #f4f6f8; font-size: 18px; font-weight: 700; }"
)


class CameraVisibilityRibbon(QWidget):
    """Clip readout above a grid of per-feed visibility toggle buttons."""

    feed_visibility_toggled = Signal(str, bool)

    def __init__(
        self,
        feeds: Sequence[FeedDefinition],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._buttons_by_feed: dict[str, ControlButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.selector_label = QLabel("Clip selector widget", self)
        self.selector_label.setStyleSheet(_SELECTOR_LABEL_STYLE)
        self.selector_label.setWordWrap(True)
        layout.addWidget(self.selector_label)

        buttons = QGridLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        # Stretch every column, used or not, so buttons are the same
        # width however many feeds are enabled.
        for column in range(_BUTTONS_PER_ROW):
            buttons.setColumnStretch(column, 1)
        layout.addLayout(buttons)

        for index, feed in enumerate(feeds):
            button = ControlButton(str(index + 1), self, font_px=16)
            button.setCheckable(True)
            button.setChecked(True)
            button.set_active(True)
            button.setToolTip(f"Show/hide {feed.display_name}")
            button.setMinimumHeight(_BUTTON_HEIGHT)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            # Capture `feed.feed_id` at definition time.
            button.toggled.connect(
                lambda checked, fid=feed.feed_id: self._on_toggled(fid, checked)
            )
            self._buttons_by_feed[feed.feed_id] = button
            row, column = divmod(index, _BUTTONS_PER_ROW)
            buttons.addWidget(button, row, column)

    def set_selector_label(
        self,
        *,
        clip_type: str | None,
        clip_number: int | None,
        play_number: int | None,
    ) -> None:
        """Replace the placeholder text with the current clip context.

        Phase 14.B: the "Clip selector widget" slot is a non-interactive
        readout. A future slice may turn it into a clickable browser
        for past clips, at which point this method goes away.
        """
        if clip_type is None or clip_number is None:
            self.selector_label.setText("No clip open")
            return
        if clip_type == CLIP_TYPE_PLAY and play_number is not None:
            self.selector_label.setText(
                f"Clip {clip_number} — play {play_number}"
            )
        else:
            self.selector_label.setText(f"Clip {clip_number} — {clip_type}")

    def is_feed_visible(self, feed_id: str) -> bool:
        button = self._buttons_by_feed.get(feed_id)
        return button.isChecked() if button is not None else True

    def _on_toggled(self, feed_id: str, visible: bool) -> None:
        button = self._buttons_by_feed.get(feed_id)
        if button is not None:
            button.set_active(visible)
        self.feed_visibility_toggled.emit(feed_id, visible)
