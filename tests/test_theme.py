"""Tests for the app-wide dark theme (app/ui/theme.py)."""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from app.ui import theme

_ROLE = QPalette.ColorRole
_GROUP = QPalette.ColorGroup


class DarkPaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.palette = theme.build_dark_palette()

    def _color(self, role: QPalette.ColorRole, group=_GROUP.Active) -> str:
        return self.palette.color(group, role).name()

    def test_window_matches_main_window_background(self) -> None:
        self.assertEqual(self._color(_ROLE.Window), theme.WINDOW_BACKGROUND)

    def test_buttons_match_control_rail_neutral_tone(self) -> None:
        self.assertEqual(self._color(_ROLE.Button), theme.CONTROL)
        self.assertEqual(self._color(_ROLE.ButtonText), theme.TEXT)

    def test_text_is_light_on_dark_surfaces(self) -> None:
        for background, foreground in (
            (_ROLE.Window, _ROLE.WindowText),
            (_ROLE.Base, _ROLE.Text),
            (_ROLE.Button, _ROLE.ButtonText),
            (_ROLE.ToolTipBase, _ROLE.ToolTipText),
        ):
            with self.subTest(background=background.name):
                bg = self.palette.color(background).lightnessF()
                fg = self.palette.color(foreground).lightnessF()
                self.assertLess(bg, 0.25)
                self.assertGreater(fg - bg, 0.6)

    def test_disabled_text_is_dimmed(self) -> None:
        for role in (_ROLE.WindowText, _ROLE.Text, _ROLE.ButtonText):
            with self.subTest(role=role.name):
                self.assertEqual(self._color(role, _GROUP.Disabled), theme.TEXT_DISABLED)

    def test_links_use_the_rail_link_blue(self) -> None:
        self.assertEqual(self._color(_ROLE.Link), theme.LINK)


class _FakeStyleHints:
    def __init__(self) -> None:
        self.requested: list[Qt.ColorScheme] = []

    def setColorScheme(self, scheme: Qt.ColorScheme) -> None:
        self.requested.append(scheme)


class _FakeApp:
    """Records what `apply_dark_theme` does, without touching the real
    `QApplication` — other test modules measure layouts under its style."""

    def __init__(self, hints: object) -> None:
        self._hints = hints
        self.calls: list[tuple[str, object]] = []

    def styleHints(self) -> object:
        return self._hints

    def setStyle(self, name: str) -> None:
        self.calls.append(("style", name))

    def setPalette(self, palette: QPalette) -> None:
        self.calls.append(("palette", palette))


class ApplyDarkThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_requests_dark_color_scheme(self) -> None:
        hints = _FakeStyleHints()
        theme.apply_dark_theme(_FakeApp(hints))
        self.assertEqual(hints.requested, [Qt.ColorScheme.Dark])

    def test_sets_fusion_then_palette(self) -> None:
        app = _FakeApp(_FakeStyleHints())
        theme.apply_dark_theme(app)
        self.assertEqual([kind for kind, _ in app.calls], ["style", "palette"])
        self.assertEqual(app.calls[0][1], "Fusion")
        palette = app.calls[1][1]
        self.assertEqual(
            palette.color(_ROLE.Window), QColor(theme.WINDOW_BACKGROUND)
        )

    def test_old_qt_without_color_scheme_still_gets_palette(self) -> None:
        app = _FakeApp(object())  # style hints with no setColorScheme (Qt < 6.8)
        theme.apply_dark_theme(app)
        self.assertEqual([kind for kind, _ in app.calls], ["style", "palette"])

    def test_fusion_style_is_available(self) -> None:
        self.assertIn("fusion", [key.lower() for key in QStyleFactory.keys()])


if __name__ == "__main__":
    unittest.main()
