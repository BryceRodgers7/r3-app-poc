"""Pre-match setup data and its two config files.

The pre-match dialog (`app/ui/prematch_dialog.py`) collects two kinds of
data before the feeds start:

- **Match details** (date, start time, venue, teams, officials), stored in
  `match_details.toml`.
- **Camera layout** (a short name and a viewport position per camera),
  stored in `camera_layout.toml`.

Both files live in `AppSettings.base_data_dir`, are owned by the app, and
are written only when the operator confirms Begin Match. Neither is the
metadata database, and `app_settings.toml` is never rewritten (it is
hand-edited and carries long comments, and the app has no TOML writer).

The camera layout is **layered over** `[[feeds]]` in `app_settings.toml`:
`short_name` replaces a feed's `display_name`, and `viewport_order` sets
its position in the video grid (`FeedRegistry.build_default`). A missing
file means today's behavior.

This module is standard-library only so `app.config.settings` can import
it without a cycle and tests can run it without Qt.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

MATCH_DETAILS_FILENAME = "match_details.toml"
CAMERA_LAYOUT_FILENAME = "camera_layout.toml"

# Field limits from the pre-match requirements (char(N)).
VENUE_MAX_LENGTH = 50
TEAM_MAX_LENGTH = 30
OFFICIAL_MAX_LENGTH = 35
CAMERA_NAME_MAX_LENGTH = 20

DEFAULT_START_TIME = time(19, 0)
DEFAULT_TEAM_B = "Opponent"

# Optional text fields of `MatchDetails`: attribute -> (label, max length).
# Order is the order they appear in the dialog and the config file.
TEXT_FIELDS: dict[str, tuple[str, int]] = {
    "venue": ("Venue", VENUE_MAX_LENGTH),
    "team_a": ("Team A", TEAM_MAX_LENGTH),
    "team_b": ("Team B", TEAM_MAX_LENGTH),
    "referee_1": ("Referee 1", OFFICIAL_MAX_LENGTH),
    "referee_2": ("Referee 2", OFFICIAL_MAX_LENGTH),
    "line_judge_1": ("Line judge 1", OFFICIAL_MAX_LENGTH),
    "line_judge_2": ("Line judge 2", OFFICIAL_MAX_LENGTH),
}


def normalize_text(value: object) -> str | None:
    """Return `value` stripped, or None when it is missing or blank.

    The requirements allow the optional fields to be null; a blank entry
    is stored as null (the key is omitted from the TOML file).
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(slots=True)
class MatchDetails:
    """One match's pre-match data. Date and start time are never null."""

    match_date: date
    scheduled_start: time
    venue: str | None = None
    team_a: str | None = None
    team_b: str | None = None
    referee_1: str | None = None
    referee_2: str | None = None
    line_judge_1: str | None = None
    line_judge_2: str | None = None

    def validate(self) -> list[str]:
        """Return a message per violated limit; empty when the data is valid."""
        errors: list[str] = []
        for attribute, (label, limit) in TEXT_FIELDS.items():
            value = getattr(self, attribute)
            if value is not None and len(value) > limit:
                errors.append(f"{label} is longer than {limit} characters.")
        return errors

    def blank_field_labels(self) -> list[str]:
        """Labels of the optional fields that were left blank, in form order."""
        return [
            label
            for attribute, (label, _limit) in TEXT_FIELDS.items()
            if getattr(self, attribute) is None
        ]


@dataclass(frozen=True, slots=True)
class MatchDefaults:
    """Static values that pre-populate the dialog (`[defaults]` in the file)."""

    scheduled_start: time = DEFAULT_START_TIME
    team_b: str | None = DEFAULT_TEAM_B


@dataclass(frozen=True, slots=True)
class MatchState:
    """What `match_details.toml` held when it was read."""

    defaults: MatchDefaults
    last_match: MatchDetails | None


def format_time(value: time) -> str:
    """Render a time as 24-hour `HH:MM`."""
    return f"{value.hour:02d}:{value.minute:02d}"


def parse_time(value: object) -> time | None:
    """Parse `HH:MM` (or `HH:MM:SS`) into a time, or None when invalid."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def parse_date(value: object) -> date | None:
    """Parse an ISO `YYYY-MM-DD` date, or None when invalid."""
    if isinstance(value, date):  # TOML date literal written by hand
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def initial_match_details(state: MatchState, *, today: date) -> MatchDetails:
    """Build the values the dialog opens with.

    Date is today. Start time and Team B come from `[defaults]` (19:00 and
    "Opponent" unless edited). Venue and Team A retain the last saved
    match. The officials start blank.
    """
    last = state.last_match
    return MatchDetails(
        match_date=today,
        scheduled_start=state.defaults.scheduled_start,
        venue=last.venue if last else None,
        team_a=last.team_a if last else None,
        team_b=state.defaults.team_b,
    )


def load_match_state(path: Path) -> MatchState:
    """Read `match_details.toml`; a missing or unreadable file gives defaults."""
    data = _read_toml(path)
    defaults_table = _as_table(data.get("defaults"))

    scheduled_start = parse_time(defaults_table.get("scheduled_start_time"))
    if scheduled_start is None:
        if "scheduled_start_time" in defaults_table:
            LOGGER.warning(
                "%s: [defaults] scheduled_start_time is not HH:MM; using %s.",
                path,
                format_time(DEFAULT_START_TIME),
            )
        scheduled_start = DEFAULT_START_TIME
    if "team_b" in defaults_table:
        team_b = _limited(defaults_table.get("team_b"), TEAM_MAX_LENGTH)
    else:
        team_b = DEFAULT_TEAM_B

    return MatchState(
        defaults=MatchDefaults(scheduled_start=scheduled_start, team_b=team_b),
        last_match=_match_from_table(_as_table(data.get("match")), path),
    )


def save_match_state(path: Path, defaults: MatchDefaults, details: MatchDetails) -> None:
    """Write `match_details.toml` with `details` as the last match.

    Raises `ValueError` when `details` breaks a length limit, and
    `OSError` when the file cannot be written.
    """
    errors = details.validate()
    if errors:
        raise ValueError(" ".join(errors))

    lines = [
        "# Pre-match setup. Written by the app when Begin Match is confirmed.",
        "#",
        "# [defaults] pre-populates the pre-match dialog. The app keeps it as",
        "# it is on every save; edit it by hand to change the defaults.",
        "# [match] is the most recent match. Its venue and team_a are offered",
        "# again the next time the dialog opens.",
        "",
        "[defaults]",
        f"scheduled_start_time = {_toml_string(format_time(defaults.scheduled_start))}",
        f"team_b = {_toml_string(defaults.team_b or '')}",
        "",
        "[match]",
        f"date = {_toml_string(details.match_date.isoformat())}",
        f"scheduled_start_time = {_toml_string(format_time(details.scheduled_start))}",
    ]
    for attribute in TEXT_FIELDS:
        value = getattr(details, attribute)
        if value is not None:  # null is stored by leaving the key out
            lines.append(f"{attribute} = {_toml_string(value)}")
    _atomic_write(path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------- cameras


@dataclass(frozen=True, slots=True)
class CameraSetting:
    """The operator's choices for one feed: a short name and a grid position."""

    feed_id: str
    short_name: str
    viewport_order: int


def validate_short_name(name: str) -> str | None:
    """Return why `name` is not a valid camera short name, or None if it is."""
    stripped = name.strip()
    if not stripped:
        return "Camera short name is required."
    if len(stripped) > CAMERA_NAME_MAX_LENGTH:
        return f"Camera short name is longer than {CAMERA_NAME_MAX_LENGTH} characters."
    return None


def load_camera_layout(path: Path) -> dict[str, CameraSetting]:
    """Read `camera_layout.toml` into a dict keyed by `feed_id`.

    A missing or unreadable file gives an empty dict. A row that is
    invalid (no feed_id, a bad short name, a non-positive order) is
    skipped with a warning so a hand-edit typo cannot stop the app.
    """
    data = _read_toml(path)
    rows = data.get("cameras")
    if not isinstance(rows, list):
        return {}
    layout: dict[str, CameraSetting] = {}
    for index, row in enumerate(rows):
        table = _as_table(row)
        feed_id = normalize_text(table.get("feed_id"))
        short_name = normalize_text(table.get("short_name"))
        order = table.get("viewport_order")
        problem: str | None = None
        if feed_id is None:
            problem = "feed_id is missing"
        elif short_name is None or validate_short_name(short_name) is not None:
            problem = "short_name is missing or too long"
        elif isinstance(order, bool) or not isinstance(order, int) or order < 1:
            problem = "viewport_order must be a whole number of 1 or more"
        elif feed_id in layout:
            problem = f"feed_id {feed_id!r} appears twice"
        if problem is not None:
            LOGGER.warning("%s: [[cameras]] row %d skipped: %s.", path, index + 1, problem)
            continue
        layout[feed_id] = CameraSetting(feed_id, short_name, order)  # type: ignore[arg-type]
    return layout


def save_camera_layout(path: Path, settings: Iterable[CameraSetting]) -> None:
    """Write `camera_layout.toml`, one `[[cameras]]` table per camera.

    Raises `ValueError` for an invalid short name or order, and `OSError`
    when the file cannot be written.
    """
    lines = [
        "# Camera short names and viewport order. Written by the app when",
        "# Begin Match is confirmed.",
        "#",
        "# Layered over [[feeds]] in app_settings.toml: short_name replaces the",
        "# feed's display_name, and viewport_order (1 = first) sets its position",
        "# in the video grid. Feeds not listed here keep their app_settings.toml",
        "# name and follow the listed ones.",
    ]
    for setting in settings:
        problem = validate_short_name(setting.short_name)
        if problem is not None:
            raise ValueError(f"{setting.feed_id}: {problem}")
        if setting.viewport_order < 1:
            raise ValueError(f"{setting.feed_id}: viewport order must be 1 or more.")
        lines += [
            "",
            "[[cameras]]",
            f"feed_id = {_toml_string(setting.feed_id)}",
            f"short_name = {_toml_string(setting.short_name.strip())}",
            f"viewport_order = {setting.viewport_order}",
        ]
    _atomic_write(path, "\n".join(lines) + "\n")


def initial_viewport_orders(
    feed_ids: Sequence[str], saved: Mapping[str, CameraSetting]
) -> dict[str, int]:
    """Return each feed's position, numbered 1..N without gaps.

    Feeds with a saved setting come first, ordered by their saved
    `viewport_order` (ties keep their `feed_ids` order). Feeds without one
    follow in `feed_ids` order. `FeedRegistry` uses the same ranking, so
    the dialog and the running app always agree.
    """

    def rank(item: tuple[int, str]) -> tuple[int, int, int]:
        index, feed_id = item
        setting = saved.get(feed_id)
        if setting is None:
            return (1, 0, index)
        return (0, setting.viewport_order, index)

    ranked = sorted(enumerate(feed_ids), key=rank)
    return {feed_id: position for position, (_index, feed_id) in enumerate(ranked, start=1)}


def swap_viewport_order(
    orders: Mapping[str, int], feed_id: str, new_order: int
) -> dict[str, int]:
    """Give `feed_id` the position `new_order`, swapping with its holder.

    Choosing a position another camera already holds moves that camera to
    the position `feed_id` just left, so the orders stay a permutation.
    """
    updated = dict(orders)
    previous = updated[feed_id]
    for other_id, value in orders.items():
        if other_id != feed_id and value == new_order:
            updated[other_id] = previous
    updated[feed_id] = new_order
    return updated


# ---------------------------------------------------------------- helpers


def _match_from_table(table: dict[str, Any], path: Path) -> MatchDetails | None:
    if not table:
        return None
    match_date = parse_date(table.get("date"))
    start = parse_time(table.get("scheduled_start_time"))
    if match_date is None or start is None:
        LOGGER.warning(
            "%s: [match] has no valid date and scheduled_start_time; "
            "ignoring the remembered venue and team.",
            path,
        )
        return None
    values = {
        attribute: _limited(table.get(attribute), limit)
        for attribute, (_label, limit) in TEXT_FIELDS.items()
    }
    return MatchDetails(match_date=match_date, scheduled_start=start, **values)


def _limited(value: object, limit: int) -> str | None:
    """Normalize `value` and cut it to `limit` characters (hand-edited files)."""
    text = normalize_text(value)
    if text is None:
        return None
    return text[:limit].rstrip() or None


def _as_table(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError):
        # A damaged config file must not stop a match from starting.
        LOGGER.warning("Could not read %s; using built-in defaults.", path, exc_info=True)
        return {}


def _toml_string(value: str) -> str:
    """Quote `value` as a TOML basic string."""
    escapes = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out = ['"']
    for char in value:
        if char in escapes:
            out.append(escapes[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so a crash cannot leave a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp_path, path)
