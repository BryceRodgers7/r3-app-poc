"""App-wide dark theme.

The custom widgets (control rail, jog wheel, seek bar, video tiles) set
their own dark colors, but anything left to the platform style —
QMessageBox, the recovery and post-process dialogs, tooltips, window
title bars — followed the Windows app theme and came out light on a
light-mode machine. `apply_dark_theme` makes the whole app dark
regardless of the OS setting.

Fusion is used instead of the native `windows11` style because Fusion
draws everything from the palette, so dialogs pick up the same colors
as the hand-styled controls. The `windows11` style paints buttons and
inputs from its own fixed WinUI color table and would ignore most of
this palette.

The colors are the values the custom widgets already use (see
`control_button.py`, `review_seek_bar.py`, `jog_wheel.py`).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

LOGGER = logging.getLogger(__name__)

WINDOW_BACKGROUND = "#14171c"
SURFACE = "#1c2027"
SURFACE_RAISED = "#22272e"
CONTROL = "#2f3742"
BORDER = "#2d333c"
BORDER_STRONG = "#566273"
TEXT = "#f4f6f8"
TEXT_MUTED = "#aeb6c2"
TEXT_DISABLED = "#626b78"
ACCENT = "#1f6feb"
LINK = "#6daffe"
ERROR = "#ff6e6e"


def build_dark_palette() -> QPalette:
    """Return the app's dark `QPalette`."""
    palette = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    def put(color_role: QPalette.ColorRole, color: str) -> None:
        palette.setColor(color_role, QColor(color))

    put(role.Window, WINDOW_BACKGROUND)
    put(role.WindowText, TEXT)
    put(role.Base, SURFACE)
    put(role.AlternateBase, SURFACE_RAISED)
    put(role.Text, TEXT)
    put(role.PlaceholderText, TEXT_DISABLED)
    put(role.Button, CONTROL)
    put(role.ButtonText, TEXT)
    put(role.BrightText, ERROR)
    put(role.ToolTipBase, SURFACE_RAISED)
    put(role.ToolTipText, TEXT)
    put(role.Highlight, ACCENT)
    put(role.HighlightedText, "#ffffff")
    put(role.Link, LINK)
    put(role.LinkVisited, LINK)
    # Fusion shades bevels, frames and the progress-bar groove from these.
    put(role.Light, BORDER_STRONG)
    put(role.Midlight, "#3c4654")
    put(role.Mid, BORDER)
    put(role.Dark, "#0e1014")
    put(role.Shadow, "#000000")
    accent_role = getattr(role, "Accent", None)  # Qt 6.6+
    if accent_role is not None:
        put(accent_role, ACCENT)

    for disabled_role in (role.WindowText, role.Text, role.ButtonText, role.HighlightedText):
        palette.setColor(group.Disabled, disabled_role, QColor(TEXT_DISABLED))
    palette.setColor(group.Disabled, role.Button, QColor(SURFACE_RAISED))
    palette.setColor(group.Disabled, role.Highlight, QColor(BORDER))
    return palette


def apply_dark_theme(app: QApplication) -> None:
    """Make `app` dark: color scheme, style and palette.

    Call once, right after the `QApplication` is created and before any
    window or dialog is built. The color-scheme request is what turns
    the Windows title bars dark; it needs Qt 6.8, and on older Qt the
    title bars stay with the OS theme while everything inside the
    windows is still dark.
    """
    set_color_scheme = getattr(app.styleHints(), "setColorScheme", None)
    if set_color_scheme is not None:
        set_color_scheme(Qt.ColorScheme.Dark)
    else:
        LOGGER.info("Qt < 6.8: title bars will follow the OS theme")
    # Style and palette go after the scheme request: a scheme change
    # re-resolves the default palette, and setting ours last keeps it.
    app.setStyle("Fusion")
    app.setPalette(build_dark_palette())
