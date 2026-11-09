"""The pick-lists for the challenge details screen.

A challenge records a **category** (why it was called) and an **outcome**
(how the review ended). Both lists come from `[challenge]` in
`app_settings.toml`, because they rarely change and are edited by hand:

    [challenge]
    categories = ["Ball Ruled In or Out", "Player/Ball Contact", ...]
    outcomes = ["Confirmed", "Reversed", "Inconclusive"]

Either key may be left out to keep its built-in default. The limits are
the requirement's `char(40)` (category) and `char(15)` (outcome), and both
fields are NOT NULL, so every entry must be non-blank.

Standard library only, so `app.config.settings` can import it.
"""

from __future__ import annotations

from collections.abc import Sequence

CATEGORY_MAX_LENGTH = 40
OUTCOME_MAX_LENGTH = 15

DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Ball Ruled In or Out",
    "Player/Ball Contact",
    "Net / Antenna Faults & Interference",
    "Center-line Faults",
)
DEFAULT_OUTCOMES: tuple[str, ...] = (
    "Confirmed",
    "Reversed",
    "Inconclusive",
)


def parse_options(
    value: object, *, name: str, max_length: int, default: Sequence[str]
) -> tuple[str, ...]:
    """Validate one `[challenge]` list and return it as a tuple.

    A missing key (`value is None`) gives `default`. Anything else must be
    a non-empty list of non-blank strings no longer than `max_length`,
    with no repeats (compared ignoring case). Violations raise
    `RuntimeError` with a message that names the key, matching how the
    other `app_settings.toml` sections report a bad value.
    """
    if value is None:
        return tuple(default)
    where = f"[challenge] {name}"
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{where} must be a non-empty list of strings.")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(f"{where} entries must be non-blank strings; got {item!r}.")
        text = item.strip()
        if len(text) > max_length:
            raise RuntimeError(
                f"{where} entry {text!r} is longer than {max_length} characters."
            )
        key = text.casefold()
        if key in seen:
            raise RuntimeError(f"{where} lists {text!r} more than once.")
        seen.add(key)
        cleaned.append(text)
    return tuple(cleaned)
