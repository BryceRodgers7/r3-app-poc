"""Bottom-left play-number badge on the referee window (Phase 14.C).

Shows `Play NNN` when a play clip is open, `Play NN` (last play
number) during timeout/challenge, or `N/A` during pre-game.

Phase 14.D will flip the text color to red while a challenge lockout
is active; the public `set_challenge_active(active: bool)` hook is
here so 14.D can light it up with a one-line cross-window signal
wire.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget


_NORMAL_STYLE = (
    "QLabel { color: #e8e8e8; font-size: 22px; font-weight: 700; "
    "padding: 6px 14px; background-color: rgba(0, 0, 0, 140); "
    "border-radius: 6px; }"
)
_CHALLENGE_STYLE = (
    "QLabel { color: #d24747; font-size: 22px; font-weight: 700; "
    "padding: 6px 14px; background-color: rgba(0, 0, 0, 140); "
    "border-radius: 6px; }"
)


class RefereePlayBadge(QLabel):
    """Free-floating play-number readout, parented to the video panel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._challenge_active = False
        self.setText("Play N/A")
        self.setStyleSheet(_NORMAL_STYLE)
        self.adjustSize()

    def set_play_state(
        self,
        *,
        play_number: int | None,
    ) -> None:
        """Update the readout from the current ClipManager state.

        - Pre-game (no play has started yet) → "Play N/A".
        - Active play / timeout / challenge → "Play NN" (current play
          when a play is open; last play number otherwise — the
          coordinator already collapses this into `current_play_number`).
        """
        if play_number is None:
            self.setText("Play N/A")
        else:
            self.setText(f"Play {play_number}")
        self.adjustSize()

    def set_challenge_active(self, active: bool) -> None:
        """Phase 14.D hook: flip text color to red during a challenge.

        Wired to `ApplicationCoordinator.challenge_state_changed` in
        14.D. No-op until then.
        """
        if active == self._challenge_active:
            return
        self._challenge_active = active
        self.setStyleSheet(_CHALLENGE_STYLE if active else _NORMAL_STYLE)
