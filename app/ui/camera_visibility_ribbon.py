"""Camera show/hide ribbon (Phase 14.B).

One checkable button per feed, labeled with the 1-based feed index
to match `docs/window-layouts.pdf`. Toggles emit
`feed_visibility_toggled(feed_id, visible)` so the caller
(`MainWindow`) can drive `MultiFeedVideoPanel.set_tile_visible`.

State is per-window: the operator hiding camera 3 does NOT hide it
on the referee window, and recording / ingest continue normally for
hidden feeds. The ribbon is purely a UI toggle.

The PDF mock shows a "Clip selector widget" label to the left of
the camera buttons. Phase 14.B renders that as a non-interactive
QLabel showing the current clip type/number; the future enhancement
(clicking it to browse past clips) is out of scope.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from app.core.models import CLIP_TYPE_PLAY, FeedDefinition


_RIBBON_BUTTON_STYLE_VISIBLE = (
    "QPushButton { background-color: #2c5a8a; color: white; "
    "font-size: 16px; font-weight: 700; padding: 8px 14px; "
    "min-width: 44px; border-radius: 4px; }"
    "QPushButton:hover { background-color: #36699e; }"
)
_RIBBON_BUTTON_STYLE_HIDDEN = (
    "QPushButton { background-color: #3a3a3a; color: #b0b0b0; "
    "font-size: 16px; font-weight: 700; padding: 8px 14px; "
    "min-width: 44px; border-radius: 4px; }"
    "QPushButton:hover { background-color: #4a4a4a; }"
)
_SELECTOR_LABEL_STYLE = (
    "QLabel { color: #c8c8c8; font-size: 13px; font-weight: 600; }"
)


class CameraVisibilityRibbon(QWidget):
    """Horizontal ribbon of per-feed visibility toggle buttons."""

    feed_visibility_toggled = Signal(str, bool)

    def __init__(
        self,
        feeds: Sequence[FeedDefinition],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._buttons_by_feed: dict[str, QPushButton] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.selector_label = QLabel("Clip selector widget", self)
        self.selector_label.setStyleSheet(_SELECTOR_LABEL_STYLE)
        self.selector_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.selector_label)
        layout.addSpacing(8)

        for index, feed in enumerate(feeds, start=1):
            button = QPushButton(str(index), self)
            button.setCheckable(True)
            button.setChecked(True)
            button.setToolTip(f"Show/hide {feed.display_name}")
            button.setStyleSheet(_RIBBON_BUTTON_STYLE_VISIBLE)
            button.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
            # Capture `feed.feed_id` at definition time.
            button.toggled.connect(
                lambda checked, fid=feed.feed_id: self._on_toggled(fid, checked)
            )
            self._buttons_by_feed[feed.feed_id] = button
            layout.addWidget(button)

        layout.addStretch(1)

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
            button.setStyleSheet(
                _RIBBON_BUTTON_STYLE_VISIBLE
                if visible
                else _RIBBON_BUTTON_STYLE_HIDDEN
            )
        self.feed_visibility_toggled.emit(feed_id, visible)
