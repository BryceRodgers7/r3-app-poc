"""Transport button that visibly acknowledges every press.

A stock `QPushButton` only looks pressed while it is physically held —
about 100 ms for a normal click, easy to miss with your eyes on the
video. `ControlButton` adds two states on top of that:

- **Flash**: a press lights the button for at least `FLASH_MS`, however
  short the click was. The flash starts on `pressed`, which runs before
  `clicked`, so it is already on screen if the click handler blocks the
  UI thread (Begin Game) and it outlives a button that disables itself
  in response (Challenge).
- **Active**: `set_active(True)` latches a highlight on buttons that
  represent a mode (current playback rate, open time-out, marked clip).
  Callers drive it from `UiState`, not from the click, so it shows what
  the controller actually did rather than what the button assumes
  happened.

The state styling is per-button QSS keyed on dynamic properties, so
callers never touch `setStyleSheet` — `MainWindow._render_state` runs
at up to 25 Hz and a stylesheet swap per tick is expensive.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton, QWidget


FLASH_MS = 250

# Press acknowledgement color. Reused by the jog wheel so every control
# in the rail answers a touch the same way.
PRESS_FEEDBACK_COLOR = "#ffd23f"


@dataclass(frozen=True, slots=True)
class ButtonTone:
    """Resting colors. Flash / active / disabled are shared across tones."""

    background: str
    hover: str
    border: str
    text: str = "#f4f6f8"


NEUTRAL_TONE = ButtonTone("#2f3742", "#3c4654", "#566273")
GO_TONE = ButtonTone("#2c8a3a", "#34a045", "#5cc66f")
STOP_TONE = ButtonTone("#c33b3b", "#d24747", "#ec8080")


def control_button_style(tone: ButtonTone, *, font_px: int) -> str:
    """Build the QSS for one button.

    Rule order matters: QSS breaks specificity ties by source order, so
    flash comes last to beat `:disabled` while a button that disabled
    itself in response to the press is still flashing. The
    `[flash]:disabled` selector is needed separately because
    `[active]:disabled` is more specific than a bare `[flash]`.
    """
    return (
        "QPushButton {"
        f" background-color: {tone.background}; color: {tone.text};"
        f" border: 2px solid {tone.border}; border-radius: 6px;"
        f" font-size: {font_px}px; font-weight: 700; padding: 4px 2px; }}"
        f"QPushButton:hover {{ background-color: {tone.hover}; }}"
        'QPushButton[active="true"] {'
        " background-color: #1f6feb; color: #ffffff; border-color: #9cc9ff; }"
        "QPushButton:disabled {"
        " background-color: #22272e; color: #626b78; border-color: #2d333c; }"
        'QPushButton[active="true"]:disabled {'
        " background-color: #1f6feb; color: #ffffff; border-color: #9cc9ff; }"
        'QPushButton:pressed, QPushButton[flash="true"],'
        ' QPushButton[flash="true"]:disabled {'
        f" background-color: {PRESS_FEEDBACK_COLOR}; color: #14171c;"
        " border-color: #fff4c2; }"
    )


class ControlButton(QPushButton):
    """`QPushButton` with a minimum-duration press flash and a latched state."""

    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
        *,
        tone: ButtonTone = NEUTRAL_TONE,
        font_px: int = 18,
    ) -> None:
        super().__init__(text, parent)
        self._font_px = font_px
        self._tone = tone
        self.setProperty("flash", False)
        self.setProperty("active", False)
        self.setStyleSheet(control_button_style(tone, font_px=font_px))

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(FLASH_MS)
        # On Windows a default timer is a WM_TIMER, which waits for the
        # message queue to drain. With feeds pushing frames through the
        # event loop that held the flash on for seconds; a precise timer
        # arrives as a posted event instead.
        self._flash_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._flash_timer.timeout.connect(self._end_flash)
        self.pressed.connect(self.flash)

    def set_tone(self, tone: ButtonTone) -> None:
        """Swap the resting colors. No-op when the tone is unchanged."""
        if tone == self._tone:
            return
        self._tone = tone
        self.setStyleSheet(control_button_style(tone, font_px=self._font_px))

    def set_active(self, active: bool) -> None:
        """Latch or clear the mode highlight. No-op when unchanged."""
        if active == self.is_active():
            return
        self.setProperty("active", active)
        self._repolish()
        self.update()

    def is_active(self) -> bool:
        return bool(self.property("active"))

    def is_flashing(self) -> bool:
        return bool(self.property("flash"))

    def flash(self) -> None:
        """Light the press flash. Every press calls this; call it directly
        when the action starts later than the press (End Game's modal)."""
        # Restarting the timer keeps rapid repeat presses lit.
        self._flash_timer.start()
        if not self.is_flashing():
            self.setProperty("flash", True)
            self._repolish()
        # Synchronous: a tap can deliver press + release back-to-back,
        # and the click handler may then block the UI thread.
        self.repaint()

    def _end_flash(self) -> None:
        self.setProperty("flash", False)
        self._repolish()
        self.update()

    def _repolish(self) -> None:
        # QSS doesn't re-evaluate attribute selectors on setProperty.
        self.style().unpolish(self)
        self.style().polish(self)
