"""The referee window's seek bar over the clip under review."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.config.settings import AppSettings
from app.core.app_state import UiState
from app.core.models import FeedDefinition
from app.ui.control_button import PRESS_FEEDBACK_COLOR
from app.ui.main_window import _LAYOUT_GAP, MainWindow
from app.ui.review_seek_bar import ReviewSeekBar, format_clip_time


_LEFT = Qt.MouseButton.LeftButton
_START_NS = 50_000_000_000  # past 2**32, as session-time is by mid-game
_END_NS = 60_000_000_000


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])


def _bar(*, enabled: bool = True) -> ReviewSeekBar:
    bar = ReviewSeekBar()
    bar.resize(800, 36)
    bar.update_review((_START_NS, _END_NS), _START_NS)
    bar.set_bar_enabled(enabled)
    return bar


def _x_for(bar: ReviewSeekBar, fraction: float) -> int:
    left, right = bar._track_span()
    return round(left + (right - left) * fraction)


def _recorder(bar: ReviewSeekBar) -> list[object]:
    events: list[object] = []
    bar.seek_requested.connect(events.append)
    bar.seek_started.connect(lambda: events.append("started"))
    bar.seek_finished.connect(lambda: events.append("finished"))
    return events


class FormatClipTimeTests(unittest.TestCase):
    def test_tenths(self) -> None:
        self.assertEqual(format_clip_time(3_400_000_000), "0:03.4")

    def test_minutes(self) -> None:
        self.assertEqual(format_clip_time(75_000_000_000), "1:15.0")

    def test_never_rolls_up_to_sixty_seconds(self) -> None:
        self.assertEqual(format_clip_time(59_990_000_000), "0:59.9")

    def test_clamps_below_zero(self) -> None:
        self.assertEqual(format_clip_time(-1), "0:00.0")


class SeekGestureTests(_QtTestCase):
    def test_click_seeks_to_that_point_in_the_clip(self) -> None:
        bar = _bar()
        events = _recorder(bar)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(_x_for(bar, 0.5), 18))
        self.assertEqual(events[0], "started")
        self.assertEqual(events[-1], "finished")
        self.assertAlmostEqual(events[1], 55_000_000_000, delta=20_000_000)

    def test_session_time_survives_the_signal(self) -> None:
        # Signal(int) would truncate this to 32 bits.
        bar = _bar()
        events = _recorder(bar)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(_x_for(bar, 0.0), 18))
        self.assertEqual(events[1], _START_NS)

    def test_ends_of_the_track_are_the_ends_of_the_clip(self) -> None:
        bar = _bar()
        events = _recorder(bar)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(bar.width() - 1, 18))
        self.assertEqual(events[1], _END_NS)

    def test_drag_collapses_moves_into_one_seek(self) -> None:
        bar = _bar()
        events = _recorder(bar)
        QTest.mousePress(bar, _LEFT, pos=QPoint(_x_for(bar, 0.2), 18))
        for fraction in (0.3, 0.4, 0.5, 0.6):
            QTest.mouseMove(bar, QPoint(_x_for(bar, fraction), 18))
        seeks = [e for e in events if isinstance(e, int)]
        self.assertEqual(seeks, [])  # all still behind the timer
        QTest.mouseRelease(bar, _LEFT, pos=QPoint(_x_for(bar, 0.6), 18))
        seeks = [e for e in events if isinstance(e, int)]
        self.assertEqual(len(seeks), 1)
        self.assertAlmostEqual(seeks[0], 56_000_000_000, delta=20_000_000)

    def test_drag_seeks_while_still_held(self) -> None:
        bar = _bar()
        events = _recorder(bar)
        QTest.mousePress(bar, _LEFT, pos=QPoint(_x_for(bar, 0.2), 18))
        QTest.qWait(80)
        self.assertEqual(sum(isinstance(e, int) for e in events), 1)
        QTest.mouseRelease(bar, _LEFT, pos=QPoint(_x_for(bar, 0.2), 18))

    def test_release_lands_on_the_final_position(self) -> None:
        bar = _bar()
        events = _recorder(bar)
        QTest.mousePress(bar, _LEFT, pos=QPoint(_x_for(bar, 0.2), 18))
        QTest.mouseRelease(bar, _LEFT, pos=QPoint(_x_for(bar, 0.9), 18))
        self.assertEqual(events[-1], "finished")
        self.assertAlmostEqual(events[-2], 59_000_000_000, delta=20_000_000)


class GateTests(_QtTestCase):
    def test_starts_disabled(self) -> None:
        self.assertFalse(ReviewSeekBar().is_bar_enabled())

    def test_disabled_bar_ignores_clicks(self) -> None:
        bar = _bar(enabled=False)
        events = _recorder(bar)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(100, 18))
        self.assertEqual(events, [])

    def test_no_clip_ignores_clicks(self) -> None:
        bar = _bar()
        bar.update_review(None, None)
        events = _recorder(bar)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(100, 18))
        self.assertEqual(events, [])

    def test_zero_length_clip_ignores_clicks(self) -> None:
        bar = _bar()
        bar.update_review((_START_NS, _START_NS), _START_NS)
        events = _recorder(bar)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(100, 18))
        self.assertEqual(events, [])

    def test_challenge_ending_mid_drag_drops_it_without_finishing(self) -> None:
        # seek_finished would resume playback as the window returns to live.
        bar = _bar()
        events = _recorder(bar)
        QTest.mousePress(bar, _LEFT, pos=QPoint(_x_for(bar, 0.2), 18))
        bar.set_bar_enabled(False)
        QTest.qWait(60)
        QTest.mouseRelease(bar, _LEFT, pos=QPoint(_x_for(bar, 0.8), 18))
        self.assertEqual(events, ["started"])


class DisplayTests(_QtTestCase):
    def _handle_color(self, bar: ReviewSeekBar) -> str:
        left, right = bar._track_span()
        x = round(left + (right - left) * bar._played_fraction())
        return bar.grab().toImage().pixelColor(x, 18).name()

    def test_handle_follows_playback(self) -> None:
        bar = _bar()
        bar.update_review((_START_NS, _END_NS), 57_500_000_000)
        self.assertAlmostEqual(bar._played_fraction(), 0.75)

    def test_drag_keeps_the_handle_under_the_finger(self) -> None:
        bar = _bar()
        QTest.mousePress(bar, _LEFT, pos=QPoint(_x_for(bar, 0.8), 18))
        bar.update_review((_START_NS, _END_NS), _START_NS)
        self.assertAlmostEqual(bar._played_fraction(), 0.8, places=2)
        QTest.mouseRelease(bar, _LEFT, pos=QPoint(_x_for(bar, 0.8), 18))

    def test_controller_position_wins_after_release(self) -> None:
        # The store clamps to what's replayable, so the seek can land short.
        bar = _bar()
        QTest.mouseClick(bar, _LEFT, pos=QPoint(_x_for(bar, 0.8), 18))
        bar.update_review((_START_NS, _END_NS), 55_000_000_000)
        self.assertAlmostEqual(bar._played_fraction(), 0.5)

    def test_handle_goes_amber_while_dragging(self) -> None:
        # Same press feedback as every button.
        bar = _bar()
        QTest.mousePress(bar, _LEFT, pos=QPoint(_x_for(bar, 0.5), 18))
        self.assertEqual(self._handle_color(bar), PRESS_FEEDBACK_COLOR)
        QTest.mouseRelease(bar, _LEFT, pos=QPoint(_x_for(bar, 0.5), 18))
        self.assertNotEqual(self._handle_color(bar), PRESS_FEEDBACK_COLOR)

    def test_position_outside_the_clip_pins_to_the_ends(self) -> None:
        bar = _bar()
        bar.update_review((_START_NS, _END_NS), _END_NS + 5_000_000_000)
        self.assertEqual(bar._played_fraction(), 1.0)

    def test_unchanged_state_skips_repaint(self) -> None:
        # update_review runs on every UiState tick.
        bar = _bar()
        with mock.patch.object(bar, "update") as update:
            bar.update_review((_START_NS, _END_NS), _START_NS)
        update.assert_not_called()


class MainWindowWiringTests(_QtTestCase):
    def _window(self, controls_role: str = "referee") -> MainWindow:
        controller = mock.Mock()
        controller.get_state.return_value = UiState()
        renderer = mock.Mock()
        window = MainWindow(
            settings=AppSettings(),
            controller=controller,
            output_renderer=renderer,
            feeds=[
                FeedDefinition(feed_id="feed_a", display_name="A", source_kind="synthetic")
            ],
            controls_role=controls_role,
            live_only_window=(controls_role == "operator"),
            application_coordinator=None,
            show_diagnostics=False,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        QApplication.processEvents()
        return window

    def test_only_the_referee_window_has_a_seek_bar(self) -> None:
        self.assertIsNotNone(self._window("referee").review_seek_bar)
        self.assertIsNone(self._window("operator").review_seek_bar)

    def test_bar_sits_under_the_video_not_under_the_rail(self) -> None:
        window = self._window()
        panel, bar = window.video_panel, window.review_seek_bar
        self.assertEqual(bar.x(), panel.x())
        self.assertEqual(bar.width(), panel.width())
        self.assertEqual(bar.y(), panel.geometry().bottom() + 1 + _LAYOUT_GAP)

    def test_challenge_gate_includes_the_bar(self) -> None:
        window = self._window()
        window._set_referee_transport_enabled(True)
        self.assertTrue(window.review_seek_bar.is_bar_enabled())
        window._set_referee_transport_enabled(False)
        self.assertFalse(window.review_seek_bar.is_bar_enabled())

    def test_state_drives_the_bar(self) -> None:
        window = self._window()
        window._render_state(
            UiState(review_range_ns=(_START_NS, _END_NS), playback_position_ns=55_000_000_000)
        )
        self.assertAlmostEqual(window.review_seek_bar._played_fraction(), 0.5)

    def test_bar_drives_the_controller_as_a_gesture(self) -> None:
        window = self._window()
        bar = window.review_seek_bar
        bar.update_review((_START_NS, _END_NS), _START_NS)
        bar.set_bar_enabled(True)
        QTest.mouseClick(bar, _LEFT, pos=QPoint(_x_for(bar, 0.0), 18))
        controller = window._controller
        names = [call[0] for call in controller.method_calls]
        self.assertEqual(
            [n for n in names if n in {"begin_jog", "seek_to_session_time", "end_jog"}],
            ["begin_jog", "seek_to_session_time", "end_jog"],
        )
        controller.seek_to_session_time.assert_called_once_with(_START_NS)


if __name__ == "__main__":
    unittest.main()
