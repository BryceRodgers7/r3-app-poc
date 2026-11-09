"""The challenge category / outcome pick-lists and their `[challenge]` config."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config.challenge_options import (
    CATEGORY_MAX_LENGTH,
    DEFAULT_CATEGORIES,
    DEFAULT_OUTCOMES,
    OUTCOME_MAX_LENGTH,
    parse_options,
)
from app.config.settings import AppSettings


def _categories(value: object):
    return parse_options(
        value, name="categories", max_length=CATEGORY_MAX_LENGTH, default=DEFAULT_CATEGORIES
    )


def _outcomes(value: object):
    return parse_options(
        value, name="outcomes", max_length=OUTCOME_MAX_LENGTH, default=DEFAULT_OUTCOMES
    )


class DefaultOptionTests(unittest.TestCase):
    def test_default_categories_match_the_requirements(self) -> None:
        self.assertEqual(
            DEFAULT_CATEGORIES,
            (
                "Ball Ruled In or Out",
                "Player/Ball Contact",
                "Net / Antenna Faults & Interference",
                "Center-line Faults",
            ),
        )

    def test_default_outcomes_match_the_requirements(self) -> None:
        self.assertEqual(DEFAULT_OUTCOMES, ("Confirmed", "Reversed", "Inconclusive"))

    def test_the_limits_are_char_40_and_char_15(self) -> None:
        self.assertEqual(CATEGORY_MAX_LENGTH, 40)
        self.assertEqual(OUTCOME_MAX_LENGTH, 15)

    def test_every_default_fits_its_limit(self) -> None:
        self.assertTrue(all(len(c) <= CATEGORY_MAX_LENGTH for c in DEFAULT_CATEGORIES))
        self.assertTrue(all(len(o) <= OUTCOME_MAX_LENGTH for o in DEFAULT_OUTCOMES))


class ParseOptionsTests(unittest.TestCase):
    def test_a_missing_key_gives_the_default(self) -> None:
        self.assertEqual(_categories(None), DEFAULT_CATEGORIES)
        self.assertEqual(_outcomes(None), DEFAULT_OUTCOMES)

    def test_a_custom_list_is_used_and_trimmed(self) -> None:
        self.assertEqual(_outcomes(["  Upheld ", "Overturned"]), ("Upheld", "Overturned"))

    def test_the_boundary_lengths_are_accepted(self) -> None:
        self.assertEqual(_categories(["c" * 40]), ("c" * 40,))
        self.assertEqual(_outcomes(["o" * 15]), ("o" * 15,))

    def test_an_entry_over_its_limit_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "longer than 40"):
            _categories(["c" * 41])
        with self.assertRaisesRegex(RuntimeError, "longer than 15"):
            _outcomes(["o" * 16])

    def test_blank_entries_are_rejected_because_the_fields_are_not_null(self) -> None:
        for bad in ([""], ["   "], ["ok", ""]):
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "non-blank"):
                _outcomes(bad)

    def test_a_non_string_entry_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            _outcomes([1, 2])

    def test_an_empty_or_non_list_value_is_rejected(self) -> None:
        for bad in ([], "Confirmed", 3, {"a": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "non-empty list"):
                _outcomes(bad)

    def test_duplicates_are_rejected_ignoring_case(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "more than once"):
            _outcomes(["Confirmed", "confirmed"])

    def test_the_error_names_the_key(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"\[challenge\] categories"):
            _categories([])


class SettingsLoadTests(unittest.TestCase):
    def _load(self, toml_text: str) -> AppSettings:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "app_settings.toml"
            path.write_text(toml_text, encoding="utf-8")
            return AppSettings.load(path)

    def test_no_challenge_block_keeps_the_defaults(self) -> None:
        settings = self._load("[app]\napp_name = 'x'\n")
        self.assertEqual(settings.challenge_categories, DEFAULT_CATEGORIES)
        self.assertEqual(settings.challenge_outcomes, DEFAULT_OUTCOMES)

    def test_a_missing_file_keeps_the_defaults(self) -> None:
        settings = AppSettings.load(Path("missing_app_settings.toml"))
        self.assertEqual(settings.challenge_categories, DEFAULT_CATEGORIES)

    def test_the_lists_are_read_from_the_config_file(self) -> None:
        settings = self._load(
            '[challenge]\ncategories = ["Ball In/Out", "Net"]\noutcomes = ["Upheld", "Overturned"]\n'
        )
        self.assertEqual(settings.challenge_categories, ("Ball In/Out", "Net"))
        self.assertEqual(settings.challenge_outcomes, ("Upheld", "Overturned"))

    def test_one_key_can_be_overridden_alone(self) -> None:
        settings = self._load('[challenge]\noutcomes = ["Yes", "No"]\n')
        self.assertEqual(settings.challenge_categories, DEFAULT_CATEGORIES)
        self.assertEqual(settings.challenge_outcomes, ("Yes", "No"))

    def test_an_invalid_list_stops_the_load_with_a_clear_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"\[challenge\] outcomes"):
            self._load('[challenge]\noutcomes = ["a very long outcome name"]\n')

    def test_the_example_config_still_loads_with_the_default_lists(self) -> None:
        example = Path(__file__).resolve().parent.parent / "app_settings.toml.example"
        settings = AppSettings.load(example)
        self.assertEqual(settings.challenge_categories, DEFAULT_CATEGORIES)
        self.assertEqual(settings.challenge_outcomes, DEFAULT_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
