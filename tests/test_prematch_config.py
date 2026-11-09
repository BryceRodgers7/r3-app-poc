"""Tests for the pre-match config files (app/config/prematch_config.py)."""

from __future__ import annotations

import unittest
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config.prematch_config import (
    CAMERA_NAME_MAX_LENGTH,
    CameraSetting,
    MatchDefaults,
    MatchDetails,
    MatchState,
    initial_match_details,
    initial_viewport_orders,
    load_camera_layout,
    load_match_state,
    normalize_text,
    parse_date,
    parse_time,
    save_camera_layout,
    save_match_state,
    swap_viewport_order,
    validate_short_name,
)
from app.config.settings import AppSettings

TODAY = date(2026, 11, 9)


def _details(**overrides) -> MatchDetails:
    values = dict(match_date=date(2026, 11, 1), scheduled_start=time(20, 30))
    values.update(overrides)
    return MatchDetails(**values)


class _TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)


class InitialMatchDetailsTests(_TempDirTestCase):
    def test_missing_file_gives_the_built_in_defaults(self) -> None:
        state = load_match_state(self.dir / "match_details.toml")
        self.assertEqual(state.defaults.scheduled_start, time(19, 0))
        self.assertEqual(state.defaults.team_b, "Opponent")
        self.assertIsNone(state.last_match)

    def test_first_run_prepopulates_per_the_requirements(self) -> None:
        state = load_match_state(self.dir / "match_details.toml")
        initial = initial_match_details(state, today=TODAY)
        self.assertEqual(initial.match_date, TODAY)
        self.assertEqual(initial.scheduled_start, time(19, 0))
        self.assertEqual(initial.team_b, "Opponent")
        for blank in ("venue", "team_a", "referee_1", "referee_2", "line_judge_1", "line_judge_2"):
            self.assertIsNone(getattr(initial, blank), blank)

    def test_venue_and_team_a_retain_the_last_saved_value(self) -> None:
        path = self.dir / "match_details.toml"
        save_match_state(
            path,
            MatchDefaults(),
            _details(
                venue="Riverside Sports Hall",
                team_a="Lakeside Lions",
                team_b="Rivals",
                referee_1="Ana Perez",
                line_judge_2="Lee Wong",
            ),
        )
        initial = initial_match_details(load_match_state(path), today=TODAY)
        self.assertEqual(initial.venue, "Riverside Sports Hall")
        self.assertEqual(initial.team_a, "Lakeside Lions")
        # Everything else goes back to its default, not to the last match.
        self.assertEqual(initial.match_date, TODAY)
        self.assertEqual(initial.scheduled_start, time(19, 0))
        self.assertEqual(initial.team_b, "Opponent")
        self.assertIsNone(initial.referee_1)
        self.assertIsNone(initial.line_judge_2)

    def test_a_blank_venue_saved_last_stays_blank(self) -> None:
        path = self.dir / "match_details.toml"
        save_match_state(path, MatchDefaults(), _details(venue="Old Hall"))
        save_match_state(path, MatchDefaults(), _details(venue=None))
        initial = initial_match_details(load_match_state(path), today=TODAY)
        self.assertIsNone(initial.venue)

    def test_defaults_section_is_editable_and_survives_a_save(self) -> None:
        path = self.dir / "match_details.toml"
        path.write_text(
            '[defaults]\nscheduled_start_time = "18:30"\nteam_b = "Away Team"\n',
            encoding="utf-8",
        )
        state = load_match_state(path)
        initial = initial_match_details(state, today=TODAY)
        self.assertEqual(initial.scheduled_start, time(18, 30))
        self.assertEqual(initial.team_b, "Away Team")

        save_match_state(path, state.defaults, _details())
        reloaded = load_match_state(path)
        self.assertEqual(reloaded.defaults, state.defaults)

    def test_blank_team_b_default_is_honored(self) -> None:
        path = self.dir / "match_details.toml"
        path.write_text('[defaults]\nteam_b = ""\n', encoding="utf-8")
        state = load_match_state(path)
        self.assertIsNone(state.defaults.team_b)

    def test_unparseable_default_time_falls_back_with_a_warning(self) -> None:
        path = self.dir / "match_details.toml"
        path.write_text('[defaults]\nscheduled_start_time = "7pm"\n', encoding="utf-8")
        with self.assertLogs("app.config.prematch_config", level="WARNING"):
            state = load_match_state(path)
        self.assertEqual(state.defaults.scheduled_start, time(19, 0))


class MatchFileRoundTripTests(_TempDirTestCase):
    def test_all_fields_round_trip(self) -> None:
        path = self.dir / "match_details.toml"
        saved = _details(
            venue="Hall",
            team_a="A",
            team_b="B",
            referee_1="R1",
            referee_2="R2",
            line_judge_1="L1",
            line_judge_2="L2",
        )
        save_match_state(path, MatchDefaults(), saved)
        self.assertEqual(load_match_state(path).last_match, saved)

    def test_null_fields_are_omitted_from_the_file_and_load_as_none(self) -> None:
        path = self.dir / "match_details.toml"
        save_match_state(path, MatchDefaults(), _details(venue="Hall"))
        text = path.read_text(encoding="utf-8")
        self.assertIn('venue = "Hall"', text)
        self.assertNotIn("referee_1", text)
        last = load_match_state(path).last_match
        assert last is not None
        self.assertIsNone(last.referee_1)
        self.assertEqual(last.venue, "Hall")

    def test_quotes_backslashes_tabs_and_unicode_round_trip(self) -> None:
        path = self.dir / "match_details.toml"
        tricky = 'The "Big" Hall \\ north\twing – Zürich 北京'
        save_match_state(path, MatchDefaults(), _details(venue=tricky[:50]))
        last = load_match_state(path).last_match
        assert last is not None
        self.assertEqual(last.venue, tricky[:50])

    def test_save_creates_missing_parent_directories(self) -> None:
        path = self.dir / "not" / "yet" / "match_details.toml"
        save_match_state(path, MatchDefaults(), _details())
        self.assertTrue(path.exists())

    def test_save_leaves_no_temp_file_behind(self) -> None:
        path = self.dir / "match_details.toml"
        save_match_state(path, MatchDefaults(), _details())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["match_details.toml"])

    def test_save_rejects_a_value_over_its_limit(self) -> None:
        path = self.dir / "match_details.toml"
        with self.assertRaises(ValueError):
            save_match_state(path, MatchDefaults(), _details(venue="x" * 51))
        with self.assertRaises(ValueError):
            save_match_state(path, MatchDefaults(), _details(team_a="x" * 31))
        with self.assertRaises(ValueError):
            save_match_state(path, MatchDefaults(), _details(referee_2="x" * 36))
        self.assertFalse(path.exists())

    def test_limits_match_the_requirements(self) -> None:
        # Venue 50, teams 30, officials 35: the boundary values are valid.
        ok = _details(
            venue="v" * 50,
            team_a="a" * 30,
            team_b="b" * 30,
            referee_1="r" * 35,
            referee_2="r" * 35,
            line_judge_1="l" * 35,
            line_judge_2="l" * 35,
        )
        self.assertEqual(ok.validate(), [])

    def test_over_long_hand_edited_values_are_cut_on_load(self) -> None:
        path = self.dir / "match_details.toml"
        path.write_text(
            '[match]\ndate = "2026-11-01"\nscheduled_start_time = "19:00"\n'
            f'venue = "{"v" * 80}"\n',
            encoding="utf-8",
        )
        last = load_match_state(path).last_match
        assert last is not None
        self.assertEqual(len(last.venue or ""), 50)

    def test_damaged_file_falls_back_to_defaults_without_raising(self) -> None:
        path = self.dir / "match_details.toml"
        path.write_text("this is = [not valid toml", encoding="utf-8")
        with self.assertLogs("app.config.prematch_config", level="WARNING"):
            state = load_match_state(path)
        self.assertEqual(state.defaults, MatchDefaults())
        self.assertIsNone(state.last_match)

    def test_match_without_a_valid_date_is_ignored(self) -> None:
        path = self.dir / "match_details.toml"
        path.write_text('[match]\nvenue = "Hall"\n', encoding="utf-8")
        with self.assertLogs("app.config.prematch_config", level="WARNING"):
            state = load_match_state(path)
        self.assertIsNone(state.last_match)

    def test_blank_field_labels_lists_only_blank_optional_fields(self) -> None:
        details = _details(venue="Hall", team_a="A", team_b="B", referee_1="R")
        self.assertEqual(
            details.blank_field_labels(), ["Referee 2", "Line judge 1", "Line judge 2"]
        )


class ParsingTests(unittest.TestCase):
    def test_normalize_text(self) -> None:
        self.assertIsNone(normalize_text(None))
        self.assertIsNone(normalize_text("   "))
        self.assertEqual(normalize_text("  Hall "), "Hall")

    def test_parse_time_is_24_hour(self) -> None:
        self.assertEqual(parse_time("19:00"), time(19, 0))
        self.assertEqual(parse_time("07:05"), time(7, 5))
        self.assertEqual(parse_time("23:59:30"), time(23, 59))
        for bad in ("24:00", "7pm", "19", "19:60", "", None, 1900):
            self.assertIsNone(parse_time(bad), repr(bad))

    def test_parse_date_is_iso(self) -> None:
        self.assertEqual(parse_date("2026-11-09"), date(2026, 11, 9))
        self.assertEqual(parse_date(date(2026, 11, 9)), date(2026, 11, 9))
        for bad in ("09/11/2026", "2026-13-01", "", None):
            self.assertIsNone(parse_date(bad), repr(bad))


class CameraLayoutFileTests(_TempDirTestCase):
    def test_round_trip(self) -> None:
        path = self.dir / "camera_layout.toml"
        settings = [
            CameraSetting("ndi_1", "Baseline A", 2),
            CameraSetting("ndi_2", 'Side "B"', 1),
        ]
        save_camera_layout(path, settings)
        loaded = load_camera_layout(path)
        self.assertEqual(loaded, {s.feed_id: s for s in settings})

    def test_missing_file_is_an_empty_layout(self) -> None:
        self.assertEqual(load_camera_layout(self.dir / "camera_layout.toml"), {})

    def test_invalid_rows_are_skipped_with_a_warning(self) -> None:
        path = self.dir / "camera_layout.toml"
        path.write_text(
            """
[[cameras]]
feed_id = "good"
short_name = "Good"
viewport_order = 1

[[cameras]]
short_name = "No feed id"
viewport_order = 2

[[cameras]]
feed_id = "no_name"
short_name = "   "
viewport_order = 3

[[cameras]]
feed_id = "too_long"
short_name = "this name is far too long"
viewport_order = 4

[[cameras]]
feed_id = "zero_order"
short_name = "Zero"
viewport_order = 0

[[cameras]]
feed_id = "text_order"
short_name = "Text"
viewport_order = "first"

[[cameras]]
feed_id = "good"
short_name = "Duplicate"
viewport_order = 5
""".strip(),
            encoding="utf-8",
        )
        with self.assertLogs("app.config.prematch_config", level="WARNING") as logs:
            loaded = load_camera_layout(path)
        self.assertEqual(list(loaded), ["good"])
        self.assertEqual(loaded["good"].short_name, "Good")
        self.assertEqual(len(logs.records), 6)

    def test_save_rejects_a_blank_or_too_long_name(self) -> None:
        path = self.dir / "camera_layout.toml"
        with self.assertRaises(ValueError):
            save_camera_layout(path, [CameraSetting("a", "  ", 1)])
        with self.assertRaises(ValueError):
            save_camera_layout(path, [CameraSetting("a", "x" * 21, 1)])
        with self.assertRaises(ValueError):
            save_camera_layout(path, [CameraSetting("a", "ok", 0)])
        self.assertFalse(path.exists())

    def test_short_name_limit_is_twenty_characters(self) -> None:
        self.assertEqual(CAMERA_NAME_MAX_LENGTH, 20)
        self.assertIsNone(validate_short_name("x" * 20))
        self.assertIsNotNone(validate_short_name("x" * 21))
        self.assertIsNotNone(validate_short_name(""))
        self.assertIsNotNone(validate_short_name("   "))


class ViewportOrderTests(unittest.TestCase):
    def test_without_saved_settings_orders_follow_the_feed_list(self) -> None:
        self.assertEqual(
            initial_viewport_orders(["a", "b", "c"], {}), {"a": 1, "b": 2, "c": 3}
        )

    def test_saved_order_is_respected_and_renumbered_without_gaps(self) -> None:
        saved = {
            "a": CameraSetting("a", "A", 5),
            "b": CameraSetting("b", "B", 2),
            "c": CameraSetting("c", "C", 9),
        }
        self.assertEqual(
            initial_viewport_orders(["a", "b", "c"], saved), {"b": 1, "a": 2, "c": 3}
        )

    def test_feeds_without_a_saved_setting_follow_the_saved_ones(self) -> None:
        saved = {"c": CameraSetting("c", "C", 1)}
        self.assertEqual(
            initial_viewport_orders(["a", "b", "c"], saved), {"c": 1, "a": 2, "b": 3}
        )

    def test_choosing_a_taken_position_swaps_the_two_cameras(self) -> None:
        orders = {"a": 1, "b": 2, "c": 3, "d": 4}
        self.assertEqual(
            swap_viewport_order(orders, "a", 3), {"a": 3, "b": 2, "c": 1, "d": 4}
        )
        # The input is not mutated.
        self.assertEqual(orders, {"a": 1, "b": 2, "c": 3, "d": 4})

    def test_swapping_always_leaves_a_permutation(self) -> None:
        orders = {"a": 1, "b": 2, "c": 3, "d": 4}
        for feed_id in orders:
            for target in range(1, 5):
                result = swap_viewport_order(orders, feed_id, target)
                self.assertEqual(sorted(result.values()), [1, 2, 3, 4])
                self.assertEqual(result[feed_id], target)

    def test_choosing_the_current_position_changes_nothing(self) -> None:
        orders = {"a": 1, "b": 2}
        self.assertEqual(swap_viewport_order(orders, "a", 1), orders)


class SettingsPathTests(unittest.TestCase):
    def test_config_files_live_in_the_data_directory(self) -> None:
        settings = AppSettings()
        settings.base_data_dir = Path("D:/ReplayData")
        self.assertEqual(settings.match_details_path, Path("D:/ReplayData/match_details.toml"))
        self.assertEqual(settings.camera_layout_path, Path("D:/ReplayData/camera_layout.toml"))

    def test_camera_layout_starts_empty(self) -> None:
        self.assertEqual(AppSettings().camera_layout, {})


class MatchStateTypeTests(unittest.TestCase):
    def test_match_state_is_a_plain_value(self) -> None:
        state = MatchState(defaults=MatchDefaults(), last_match=None)
        self.assertEqual(state.defaults.team_b, "Opponent")


if __name__ == "__main__":
    unittest.main()
