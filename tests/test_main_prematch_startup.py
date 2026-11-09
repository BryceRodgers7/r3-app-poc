"""The pre-match steps wired into main.py's startup."""

from __future__ import annotations

import inspect
import unittest
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import main
from app.config.prematch_config import CameraSetting, MatchDetails, save_camera_layout
from app.config.settings import AppSettings
from app.ui.prematch_flow import PrematchResult


def _settings(directory: str) -> AppSettings:
    settings = AppSettings()
    settings.base_data_dir = Path(directory)
    return settings


class ApplyPrematchSetupTests(unittest.TestCase):
    def test_a_normal_start_runs_the_dialog_and_applies_its_layout(self) -> None:
        with TemporaryDirectory() as temp:
            settings = _settings(temp)
            result = PrematchResult(
                details=MatchDetails(date(2026, 11, 9), time(19, 0)),
                cameras=(CameraSetting("ndi_1", "Baseline A", 1),),
            )
            with mock.patch.object(main, "run_prematch_flow", return_value=result) as flow:
                main._apply_prematch_setup(settings, resume_session_id=None)
        flow.assert_called_once_with(settings)
        self.assertEqual(settings.camera_layout, {"ndi_1": CameraSetting("ndi_1", "Baseline A", 1)})

    def test_quitting_the_dialog_cancels_the_startup(self) -> None:
        settings = _settings("unused")
        with mock.patch.object(main, "run_prematch_flow", return_value=None):
            with self.assertRaises(main.StartupCancelled):
                main._apply_prematch_setup(settings, resume_session_id=None)
        self.assertEqual(settings.camera_layout, {})

    def test_a_resumed_session_skips_the_dialog_but_keeps_the_saved_layout(self) -> None:
        with TemporaryDirectory() as temp:
            settings = _settings(temp)
            save_camera_layout(
                settings.camera_layout_path, [CameraSetting("ndi_1", "Baseline A", 2)]
            )
            with mock.patch.object(main, "run_prematch_flow") as flow:
                main._apply_prematch_setup(settings, resume_session_id="session_007")
        flow.assert_not_called()
        self.assertEqual(settings.camera_layout, {"ndi_1": CameraSetting("ndi_1", "Baseline A", 2)})

    def test_a_resumed_session_with_no_saved_layout_changes_nothing(self) -> None:
        with TemporaryDirectory() as temp:
            settings = _settings(temp)
            with mock.patch.object(main, "run_prematch_flow") as flow:
                main._apply_prematch_setup(settings, resume_session_id="session_007")
        flow.assert_not_called()
        self.assertEqual(settings.camera_layout, {})


class EntryPointTests(unittest.TestCase):
    def test_scripted_callers_get_no_dialog_by_default(self) -> None:
        # docs/ndi_test_harness drives build_application() with no operator.
        parameter = inspect.signature(main.build_application).parameters["prematch_setup"]
        self.assertIs(parameter.default, False)

    def test_main_enables_the_pre_match_steps(self) -> None:
        with mock.patch.object(main, "build_application", side_effect=main.StartupCancelled) as build:
            self.assertEqual(main.main(), 0)
        build.assert_called_once_with(prematch_setup=True)

    def test_quitting_the_dialog_exits_without_showing_windows_or_running_qt(self) -> None:
        with mock.patch.object(main, "build_application", side_effect=main.StartupCancelled):
            with mock.patch.object(main, "QApplication") as qt_app:
                self.assertEqual(main.main(), 0)
        qt_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
