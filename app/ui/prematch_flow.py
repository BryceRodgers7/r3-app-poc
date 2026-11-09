"""Startup flow for the pre-match setup (runs before any feed starts).

`main.build_application(prematch_setup=True)` calls `run_prematch_flow`
between the crash-recovery prompt and the construction of the
coordinator:

1. Read the enabled feeds from the settings (no coordinator needed).
2. Pre-populate the dialog from `match_details.toml` and
   `camera_layout.toml` in `base_data_dir`.
3. Show the dialog. Quit gives `None`; the caller exits the app.
4. On Begin Match, write both files and return what the operator chose.

A failure to write a config file is logged and does not stop the match:
the returned result is still applied for this run, only the "remember for
next time" part is lost.

The flow is skipped on a session Resume (the match was entered before the
crash); the bootstrap then just loads the saved camera layout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

from app.config.prematch_config import (
    CameraSetting,
    MatchDetails,
    initial_match_details,
    load_camera_layout,
    load_match_state,
    save_camera_layout,
    save_match_state,
)
from app.config.settings import AppSettings
from app.core.feed_registry import FeedRegistry
from app.ui.prematch_dialog import PrematchDialog, build_camera_choices

LOGGER = logging.getLogger(__name__)

# QDialog.DialogCode.Accepted; spelled as an int so test doubles need no Qt.
_ACCEPTED = 1


@dataclass(frozen=True, slots=True)
class PrematchResult:
    """What the operator confirmed with Begin Match."""

    details: MatchDetails
    cameras: tuple[CameraSetting, ...]

    def camera_layout(self) -> dict[str, CameraSetting]:
        """The cameras keyed by feed_id, ready for `AppSettings.camera_layout`."""
        return {setting.feed_id: setting for setting in self.cameras}


DialogFactory = Callable[..., object]


def run_prematch_flow(
    settings: AppSettings,
    *,
    today: date | None = None,
    dialog_factory: DialogFactory | None = None,
) -> PrematchResult | None:
    """Show the pre-match dialog and save its result.

    Returns None when the operator quits. `settings.camera_layout` must
    not have been applied yet: the dialog shows each feed's configured
    `display_name` from app_settings.toml.
    """
    feeds = FeedRegistry.build_default(settings).get_enabled_feeds()
    state = load_match_state(settings.match_details_path)
    saved_layout = load_camera_layout(settings.camera_layout_path)

    details = initial_match_details(state, today=today or date.today())
    choices = build_camera_choices(feeds, saved_layout)

    dialog = (dialog_factory or PrematchDialog)(details, choices)
    accepted = dialog.exec() == _ACCEPTED
    if not accepted or dialog.quit_requested():
        LOGGER.info("Pre-match setup: operator quit before beginning the match.")
        return None

    chosen_details = dialog.match_details()
    cameras = merge_with_unlisted(dialog.camera_settings(), saved_layout)
    result = PrematchResult(details=chosen_details, cameras=tuple(cameras))

    _save(
        "match details",
        lambda: save_match_state(settings.match_details_path, state.defaults, chosen_details),
    )
    _save("camera layout", lambda: save_camera_layout(settings.camera_layout_path, cameras))
    return result


def load_saved_camera_layout(settings: AppSettings) -> dict[str, CameraSetting]:
    """Load the saved camera layout without showing the dialog (Resume path)."""
    return load_camera_layout(settings.camera_layout_path)


def load_saved_match_details(settings: AppSettings) -> MatchDetails | None:
    """The last saved match, or None; used when a Resume skips the dialog.

    A resumed game continues the match entered before the crash, so its
    team names are what the challenge screen should offer.
    """
    return load_match_state(settings.match_details_path).last_match


def merge_with_unlisted(
    shown: list[CameraSetting], saved: dict[str, CameraSetting]
) -> list[CameraSetting]:
    """Keep saved settings for feeds the dialog did not list.

    A feed that is disabled today (so absent from the dialog) keeps its
    saved short name for when it is enabled again. Its position moves
    after the listed cameras, keeping the order it had.
    """
    shown_ids = {setting.feed_id for setting in shown}
    leftovers = sorted(
        (s for s in saved.values() if s.feed_id not in shown_ids),
        key=lambda setting: setting.viewport_order,
    )
    merged = list(shown)
    next_order = len(shown) + 1
    for setting in leftovers:
        merged.append(replace(setting, viewport_order=next_order))
        next_order += 1
    return merged


def _save(what: str, write: Callable[[], None]) -> None:
    try:
        write()
    except (OSError, ValueError):
        LOGGER.exception(
            "Could not save the %s; this run uses the values just entered, "
            "but they will not be remembered next time.",
            what,
        )
