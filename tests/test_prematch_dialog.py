"""Tests for the pre-match setup dialog (app/ui/prematch_dialog.py)."""

from __future__ import annotations

import unittest
from datetime import date, time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.config.prematch_config import CameraSetting, MatchDetails
from app.core.models import FeedDefinition
from app.ui.prematch_dialog import CameraChoice, PrematchDialog, build_camera_choices


class _Confirm:
    """Records the prompts the dialog raises and answers them."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _parent, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return self.answer


def _details(**overrides) -> MatchDetails:
    values = dict(
        match_date=date(2026, 11, 9),
        scheduled_start=time(19, 0),
        venue="Riverside Sports Hall",
        team_a="Lakeside Lions",
        team_b="Opponent",
    )
    values.update(overrides)
    return MatchDetails(**values)


def _choices(count: int = 4) -> list[CameraChoice]:
    rows = [
        ("ndi_1", "NDI-47800", "POVZOOM-47800 (POVZoom-47800)", "Baseline A"),
        ("ndi_2", "NDI-55014", "POVZOOM-55014 (POVZoom-55014)", "Baseline B"),
        ("ndi_3", "NDI3", "DAVIDHP (VLC)", "Sideline A"),
        ("ndi_4", "vlc1", "DAVIDI7 (VLC)", "Sideline B"),
    ]
    return [
        CameraChoice(feed_id, configured, source, short, order)
        for order, (feed_id, configured, source, short) in enumerate(rows[:count], start=1)
    ]


class _DialogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def make(
        self,
        *,
        details: MatchDetails | None = None,
        cameras: list[CameraChoice] | None = None,
        answer: bool = True,
    ) -> tuple[PrematchDialog, _Confirm]:
        confirm = _Confirm(answer)
        dialog = PrematchDialog(
            details or _details(),
            cameras if cameras is not None else _choices(),
            confirm_fn=confirm,
        )
        self.addCleanup(dialog.deleteLater)
        return dialog, confirm


class MatchDetailsTabTests(_DialogTestCase):
    def test_opens_with_the_supplied_values(self) -> None:
        dialog, _ = self.make()
        self.assertEqual(dialog.date_edit.date().toString("yyyy-MM-dd"), "2026-11-09")
        self.assertEqual(dialog.time_edit.time().toString("HH:mm"), "19:00")
        self.assertEqual(dialog.venue_field.text(), "Riverside Sports Hall")
        self.assertEqual(dialog.team_a_field.text(), "Lakeside Lions")
        self.assertEqual(dialog.team_b_field.text(), "Opponent")
        for field in (
            dialog.referee_1_field,
            dialog.referee_2_field,
            dialog.line_judge_1_field,
            dialog.line_judge_2_field,
        ):
            self.assertEqual(field.text(), "")

    def test_time_is_24_hour(self) -> None:
        dialog, _ = self.make(details=_details(scheduled_start=time(7, 5)))
        self.assertEqual(dialog.time_edit.displayFormat(), "HH:mm")
        self.assertEqual(dialog.time_edit.time().toString("HH:mm"), "07:05")

    def test_text_fields_stop_at_their_limits(self) -> None:
        dialog, _ = self.make()
        limits = {
            dialog.venue_field: 50,
            dialog.team_a_field: 30,
            dialog.team_b_field: 30,
            dialog.referee_1_field: 35,
            dialog.referee_2_field: 35,
            dialog.line_judge_1_field: 35,
            dialog.line_judge_2_field: 35,
        }
        for field, limit in limits.items():
            with self.subTest(label=field.label.text()):
                self.assertEqual(field.edit.maxLength(), limit)
                field.edit.setText("x" * (limit + 10))
                self.assertEqual(len(field.text()), limit)

    def test_counters_track_the_text_length(self) -> None:
        dialog, _ = self.make()
        self.assertEqual(dialog.venue_field.counter.text(), "21 / 50")
        dialog.referee_1_field.edit.setText("Ana")
        self.assertEqual(dialog.referee_1_field.counter.text(), "3 / 35")

    def test_blank_optional_fields_come_back_as_none_and_text_is_trimmed(self) -> None:
        dialog, _ = self.make()
        dialog.venue_field.edit.setText("   ")
        dialog.referee_1_field.edit.setText("  Ana Perez  ")
        details = dialog.match_details()
        self.assertIsNone(details.venue)
        self.assertEqual(details.referee_1, "Ana Perez")
        self.assertIsNone(details.referee_2)

    def test_edited_values_are_returned(self) -> None:
        dialog, _ = self.make()
        dialog.date_edit.setDate(dialog.date_edit.date().addDays(1))
        dialog.time_edit.setTime(dialog.time_edit.time().addSecs(90 * 60))
        details = dialog.match_details()
        self.assertEqual(details.match_date, date(2026, 11, 10))
        self.assertEqual(details.scheduled_start, time(20, 30))

    def test_date_and_time_are_never_empty(self) -> None:
        dialog, _ = self.make()
        self.assertTrue(dialog.date_edit.date().isValid())
        self.assertTrue(dialog.time_edit.time().isValid())


class CamerasTabTests(_DialogTestCase):
    def test_lists_exactly_the_cameras_it_was_given(self) -> None:
        for count in (1, 2, 4):
            with self.subTest(count=count):
                dialog, _ = self.make(cameras=_choices(count))
                self.assertEqual(len(dialog._camera_rows), count)
                self.assertEqual(len(dialog.camera_settings()), count)

    def test_order_choices_run_from_one_to_the_number_of_active_feeds(self) -> None:
        for count in (2, 4):
            with self.subTest(count=count):
                dialog, _ = self.make(cameras=_choices(count))
                for row in dialog._camera_rows:
                    items = [row.order_combo.itemText(i) for i in range(row.order_combo.count())]
                    self.assertEqual(items, [str(n) for n in range(1, count + 1)])

    def test_source_shows_the_display_name_with_the_ndi_name_in_parentheses(self) -> None:
        dialog, _ = self.make()
        text = dialog._camera_rows[0].source_label.text()
        self.assertIn("NDI-47800", text)
        self.assertIn("(POVZOOM-47800 (POVZoom-47800))", text)

    def test_source_text_is_html_escaped(self) -> None:
        choice = CameraChoice("x", "A<b>&", "HOST <1>", "Cam", 1)
        dialog, _ = self.make(cameras=[choice])
        text = dialog._camera_rows[0].source_label.text()
        self.assertNotIn("<b>&", text)
        self.assertIn("A&lt;b&gt;&amp;", text)

    def test_short_names_are_prefilled_and_limited_to_twenty(self) -> None:
        dialog, _ = self.make()
        row = dialog._camera_rows[0]
        self.assertEqual(row.name(), "Baseline A")
        self.assertEqual(row.name_edit.maxLength(), 20)
        self.assertEqual(row.name_counter.text(), "10 / 20")

    def test_choosing_a_taken_position_swaps_the_two_cameras(self) -> None:
        dialog, _ = self.make()
        first = dialog._camera_rows[0]  # ndi_1 holds position 1
        first.order_combo.setCurrentIndex(2)  # ask for position 3
        orders = {s.feed_id: s.viewport_order for s in dialog.camera_settings()}
        self.assertEqual(orders, {"ndi_1": 3, "ndi_2": 2, "ndi_3": 1, "ndi_4": 4})
        # The other combo boxes show the new positions too.
        shown = {row.feed_id: int(row.order_combo.currentData()) for row in dialog._camera_rows}
        self.assertEqual(shown, orders)

    def test_positions_always_stay_a_permutation(self) -> None:
        dialog, _ = self.make()
        for row_index, target_index in ((0, 3), (3, 1), (1, 0), (2, 2), (0, 1)):
            dialog._camera_rows[row_index].order_combo.setCurrentIndex(target_index)
            positions = sorted(s.viewport_order for s in dialog.camera_settings())
            self.assertEqual(positions, [1, 2, 3, 4])

    def test_camera_settings_are_returned_in_viewport_order(self) -> None:
        dialog, _ = self.make()
        dialog._camera_rows[0].order_combo.setCurrentIndex(3)
        self.assertEqual(
            [s.feed_id for s in dialog.camera_settings()], ["ndi_4", "ndi_2", "ndi_3", "ndi_1"]
        )

    def test_the_layout_preview_follows_order_and_names(self) -> None:
        dialog, _ = self.make()
        dialog._camera_rows[0].order_combo.setCurrentIndex(1)  # ndi_1 -> 2, ndi_2 -> 1
        dialog._camera_rows[1].name_edit.setText("Left Wing")
        self.assertEqual(dialog._tiles[0].text(), "1\nLeft Wing")
        self.assertEqual(dialog._tiles[1].text(), "2\nBaseline A")
        self.assertEqual(len(dialog._tiles), 4)

    def test_a_blank_short_name_blocks_begin_match(self) -> None:
        dialog, _ = self.make()
        self.assertTrue(dialog.begin_button.isEnabled())
        dialog._camera_rows[2].name_edit.setText("   ")
        self.assertFalse(dialog.begin_button.isEnabled())
        self.assertIn("short name is required", dialog.status_label.text())
        dialog._camera_rows[2].name_edit.setText("Sideline A")
        self.assertTrue(dialog.begin_button.isEnabled())

    def test_names_are_trimmed_in_the_result(self) -> None:
        dialog, _ = self.make()
        dialog._camera_rows[0].name_edit.setText("  Court 1  ")
        self.assertEqual(dialog.camera_settings()[0].short_name, "Court 1")


class BuildCameraChoicesTests(unittest.TestCase):
    def _feeds(self) -> list[FeedDefinition]:
        return [
            FeedDefinition("ndi_1", "NDI One", "ndi", "HOST (One)"),
            FeedDefinition("ndi_2", "NDI Two", "ndi", "HOST (Two)"),
            FeedDefinition("dev", "Synthetic Pattern", "synthetic"),
        ]

    def test_defaults_to_the_configured_name_and_file_order(self) -> None:
        choices = build_camera_choices(self._feeds(), {})
        self.assertEqual([c.short_name for c in choices], ["NDI One", "NDI Two", "Synthetic Pattern"])
        self.assertEqual([c.viewport_order for c in choices], [1, 2, 3])

    def test_source_detail_is_the_ndi_name_or_the_source_kind(self) -> None:
        choices = build_camera_choices(self._feeds(), {})
        self.assertEqual([c.source_detail for c in choices], ["HOST (One)", "HOST (Two)", "synthetic"])
        self.assertEqual(choices[0].configured_name, "NDI One")

    def test_saved_settings_prefill_name_and_order(self) -> None:
        saved = {
            "ndi_1": CameraSetting("ndi_1", "Baseline A", 2),
            "ndi_2": CameraSetting("ndi_2", "Baseline B", 1),
        }
        choices = {c.feed_id: c for c in build_camera_choices(self._feeds(), saved)}
        self.assertEqual((choices["ndi_1"].short_name, choices["ndi_1"].viewport_order), ("Baseline A", 2))
        self.assertEqual((choices["ndi_2"].short_name, choices["ndi_2"].viewport_order), ("Baseline B", 1))
        self.assertEqual(choices["dev"].viewport_order, 3)

    def test_a_long_configured_name_is_cut_to_the_short_name_limit(self) -> None:
        feeds = [FeedDefinition("x", "A very long configured display name", "ndi", "HOST (X)")]
        self.assertEqual(len(build_camera_choices(feeds, {})[0].short_name), 20)


class BeginMatchTests(_DialogTestCase):
    def test_begin_asks_for_confirmation_and_accepts_on_yes(self) -> None:
        dialog, confirm = self.make(answer=True)
        dialog.begin_button.click()
        self.assertEqual(len(confirm.calls), 1)
        self.assertEqual(confirm.calls[0][0], "Begin the match?")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.quit_requested())

    def test_begin_stays_open_on_no(self) -> None:
        dialog, confirm = self.make(answer=False)
        dialog.begin_button.click()
        self.assertEqual(len(confirm.calls), 1)
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_begin_does_nothing_while_a_camera_name_is_blank(self) -> None:
        dialog, confirm = self.make()
        dialog._camera_rows[0].name_edit.setText("")
        dialog._on_begin_clicked()
        self.assertEqual(confirm.calls, [])
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_the_confirmation_summarizes_what_will_be_saved(self) -> None:
        dialog, _ = self.make()
        text = dialog.begin_confirmation_text()
        self.assertIn("Lakeside Lions vs Opponent", text)
        self.assertIn("2026-11-09 19:00 · Riverside Sports Hall", text)
        self.assertIn("4 cameras: Baseline A, Baseline B, Sideline A, Sideline B", text)
        self.assertIn("Left blank: Referee 1, Referee 2, Line judge 1, Line judge 2", text)
        self.assertIn("cameras will start", text)

    def test_the_confirmation_lists_cameras_in_viewport_order(self) -> None:
        dialog, _ = self.make(cameras=_choices(2))
        dialog._camera_rows[0].order_combo.setCurrentIndex(1)
        self.assertIn("2 cameras: Baseline B, Baseline A", dialog.begin_confirmation_text())

    def test_no_blank_line_when_every_field_is_filled(self) -> None:
        dialog, _ = self.make(
            details=_details(referee_1="a", referee_2="b", line_judge_1="c", line_judge_2="d")
        )
        self.assertNotIn("Left blank", dialog.begin_confirmation_text())

    def test_begin_is_not_the_default_button(self) -> None:
        # Enter inside a text field must not start the match.
        dialog, _ = self.make()
        for button in (dialog.begin_button, dialog.quit_button):
            self.assertFalse(button.isDefault())
            self.assertFalse(button.autoDefault())


class QuitTests(_DialogTestCase):
    def test_quit_asks_for_confirmation_and_quits_on_yes(self) -> None:
        dialog, confirm = self.make(answer=True)
        dialog.quit_button.click()
        self.assertEqual(len(confirm.calls), 1)
        self.assertIn("Quit", confirm.calls[0][0])
        self.assertTrue(dialog.quit_requested())
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_quit_stays_open_on_no(self) -> None:
        dialog, confirm = self.make(answer=False)
        dialog.quit_button.click()
        self.assertEqual(len(confirm.calls), 1)
        self.assertFalse(dialog.quit_requested())

    def test_escape_is_treated_as_quit_with_confirmation(self) -> None:
        dialog, confirm = self.make(answer=False)
        dialog.show()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertEqual(len(confirm.calls), 1)
        self.assertFalse(dialog.quit_requested())
        self.assertTrue(dialog.isVisible())

    def test_the_window_close_button_cannot_dismiss_without_confirmation(self) -> None:
        dialog, confirm = self.make(answer=False)
        dialog.show()
        dialog.close()
        self.assertEqual(len(confirm.calls), 1)
        self.assertTrue(dialog.isVisible())

    def test_closing_the_window_quits_when_confirmed(self) -> None:
        dialog, confirm = self.make(answer=True)
        dialog.show()
        dialog.close()
        self.assertTrue(dialog.quit_requested())
        self.assertFalse(dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
