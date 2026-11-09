"""Tests for the pre-match startup flow (app/ui/prematch_flow.py)."""

from __future__ import annotations

import unittest
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config.prematch_config import (
    CameraSetting,
    MatchDefaults,
    MatchDetails,
    load_camera_layout,
    load_match_state,
    save_camera_layout,
)
from app.config.settings import AppSettings
from app.ui.prematch_flow import load_saved_camera_layout, merge_with_unlisted, run_prematch_flow

TODAY = date(2026, 11, 9)


def _row(feed_id: str, display_name: str, *, enabled: bool = True) -> dict:
    return {
        "feed_id": feed_id,
        "display_name": display_name,
        "source_kind": "ndi",
        "ndi_name": f"HOST ({display_name})",
        "enabled": enabled,
    }


class FakeDialog:
    """A stand-in for `PrematchDialog` that the test scripts."""

    instances: list["FakeDialog"] = []
    script = {}

    def __init__(self, details, choices) -> None:
        self.details = details
        self.choices = choices
        self.instances.append(self)

    def exec(self) -> int:
        return 0 if self.script.get("quit") else 1

    def quit_requested(self) -> bool:
        return bool(self.script.get("quit"))

    def match_details(self) -> MatchDetails:
        return self.script.get("chosen_details") or self.details

    def camera_settings(self) -> list[CameraSetting]:
        if "chosen_cameras" in self.script:
            return self.script["chosen_cameras"]
        return [
            CameraSetting(c.feed_id, c.short_name, c.viewport_order) for c in self.choices
        ]


class PrematchFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.settings = AppSettings()
        self.settings.base_data_dir = Path(self._temp.name)
        self.settings.feeds_table_rows = [
            _row("ndi_1", "NDI One"),
            _row("ndi_2", "NDI Two"),
            _row("ndi_3", "NDI Three", enabled=False),
        ]
        FakeDialog.instances = []
        FakeDialog.script = {}

    def run_flow(self):
        return run_prematch_flow(self.settings, today=TODAY, dialog_factory=FakeDialog)

    def test_first_run_opens_with_the_requirement_defaults(self) -> None:
        self.run_flow()
        dialog = FakeDialog.instances[0]
        self.assertEqual(dialog.details.match_date, TODAY)
        self.assertEqual(dialog.details.scheduled_start, time(19, 0))
        self.assertEqual(dialog.details.team_b, "Opponent")
        self.assertIsNone(dialog.details.venue)
        self.assertIsNone(dialog.details.team_a)

    def test_only_active_feeds_are_offered(self) -> None:
        self.run_flow()
        choices = FakeDialog.instances[0].choices
        self.assertEqual([c.feed_id for c in choices], ["ndi_1", "ndi_2"])
        self.assertEqual([c.viewport_order for c in choices], [1, 2])
        self.assertEqual([c.short_name for c in choices], ["NDI One", "NDI Two"])

    def test_begin_match_saves_both_config_files(self) -> None:
        FakeDialog.script["chosen_details"] = MatchDetails(
            match_date=TODAY,
            scheduled_start=time(18, 15),
            venue="Riverside Sports Hall",
            team_a="Lakeside Lions",
            team_b="Rivals",
            referee_1="Ana Perez",
        )
        FakeDialog.script["chosen_cameras"] = [
            CameraSetting("ndi_2", "Baseline B", 1),
            CameraSetting("ndi_1", "Baseline A", 2),
        ]
        result = self.run_flow()
        assert result is not None

        saved = load_match_state(self.settings.match_details_path).last_match
        self.assertEqual(saved, FakeDialog.script["chosen_details"])
        layout = load_camera_layout(self.settings.camera_layout_path)
        self.assertEqual(layout["ndi_2"], CameraSetting("ndi_2", "Baseline B", 1))
        self.assertEqual(layout["ndi_1"], CameraSetting("ndi_1", "Baseline A", 2))

        self.assertEqual(result.details.referee_1, "Ana Perez")
        self.assertEqual(result.camera_layout()["ndi_2"].viewport_order, 1)

    def test_next_run_remembers_venue_team_a_and_the_camera_layout(self) -> None:
        FakeDialog.script["chosen_details"] = MatchDetails(
            match_date=date(2026, 11, 1),
            scheduled_start=time(20, 0),
            venue="Riverside Sports Hall",
            team_a="Lakeside Lions",
            team_b="Rivals",
            referee_1="Ana Perez",
        )
        FakeDialog.script["chosen_cameras"] = [
            CameraSetting("ndi_2", "Baseline B", 1),
            CameraSetting("ndi_1", "Baseline A", 2),
        ]
        self.run_flow()

        FakeDialog.script = {}
        self.run_flow()
        second = FakeDialog.instances[-1]
        self.assertEqual(second.details.venue, "Riverside Sports Hall")
        self.assertEqual(second.details.team_a, "Lakeside Lions")
        self.assertEqual(second.details.team_b, "Opponent")
        self.assertEqual(second.details.scheduled_start, time(19, 0))
        self.assertIsNone(second.details.referee_1)
        self.assertEqual(second.details.match_date, TODAY)
        by_id = {c.feed_id: c for c in second.choices}
        self.assertEqual((by_id["ndi_2"].short_name, by_id["ndi_2"].viewport_order), ("Baseline B", 1))
        self.assertEqual((by_id["ndi_1"].short_name, by_id["ndi_1"].viewport_order), ("Baseline A", 2))
        # The dialog still shows the app_settings.toml name for reference.
        self.assertEqual(by_id["ndi_1"].configured_name, "NDI One")

    def test_quit_returns_none_and_writes_nothing(self) -> None:
        FakeDialog.script["quit"] = True
        self.assertIsNone(self.run_flow())
        self.assertFalse(self.settings.match_details_path.exists())
        self.assertFalse(self.settings.camera_layout_path.exists())

    def test_a_save_failure_does_not_stop_the_match(self) -> None:
        blocker = Path(self._temp.name) / "a_file"
        blocker.write_text("not a directory", encoding="utf-8")
        self.settings.base_data_dir = blocker / "data"  # cannot be created
        with self.assertLogs("app.ui.prematch_flow", level="ERROR") as logs:
            result = self.run_flow()
        self.assertIsNotNone(result)
        self.assertEqual(len(logs.records), 2)  # match details and camera layout

    def test_the_flow_does_not_apply_the_layout_to_the_settings(self) -> None:
        # The caller does that, after the dialog; the dialog must see the
        # app_settings.toml display names.
        self.run_flow()
        self.assertEqual(self.settings.camera_layout, {})

    def test_the_defaults_section_is_kept_when_the_match_is_saved(self) -> None:
        self.settings.match_details_path.write_text(
            '[defaults]\nscheduled_start_time = "18:30"\nteam_b = "Away"\n', encoding="utf-8"
        )
        self.run_flow()
        self.assertEqual(FakeDialog.instances[0].details.scheduled_start, time(18, 30))
        state = load_match_state(self.settings.match_details_path)
        self.assertEqual(state.defaults, MatchDefaults(time(18, 30), "Away"))
        self.assertIsNotNone(state.last_match)


class UnlistedCameraTests(unittest.TestCase):
    def test_a_disabled_feeds_saved_name_is_kept_after_the_listed_cameras(self) -> None:
        shown = [CameraSetting("a", "A", 1), CameraSetting("b", "B", 2)]
        saved = {
            "a": CameraSetting("a", "Old A", 9),
            "spare": CameraSetting("spare", "Spare", 3),
            "other": CameraSetting("other", "Other", 1),
        }
        merged = merge_with_unlisted(shown, saved)
        self.assertEqual(
            merged,
            [
                CameraSetting("a", "A", 1),
                CameraSetting("b", "B", 2),
                CameraSetting("other", "Other", 3),
                CameraSetting("spare", "Spare", 4),
            ],
        )

    def test_nothing_is_added_when_every_saved_feed_is_shown(self) -> None:
        shown = [CameraSetting("a", "A", 1)]
        self.assertEqual(merge_with_unlisted(shown, {"a": CameraSetting("a", "x", 5)}), shown)


class ResumePathTests(unittest.TestCase):
    def test_the_saved_layout_loads_without_a_dialog(self) -> None:
        with TemporaryDirectory() as temp:
            settings = AppSettings()
            settings.base_data_dir = Path(temp)
            save_camera_layout(settings.camera_layout_path, [CameraSetting("ndi_1", "Baseline A", 1)])
            layout = load_saved_camera_layout(settings)
        self.assertEqual(layout, {"ndi_1": CameraSetting("ndi_1", "Baseline A", 1)})

    def test_a_missing_layout_file_loads_as_empty(self) -> None:
        with TemporaryDirectory() as temp:
            settings = AppSettings()
            settings.base_data_dir = Path(temp)
            self.assertEqual(load_saved_camera_layout(settings), {})


if __name__ == "__main__":
    unittest.main()
