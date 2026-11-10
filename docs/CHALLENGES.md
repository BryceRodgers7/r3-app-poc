# Challenge details and the game clock

Two operator-window features sit on top of the existing clip / challenge
lockout machinery (`IMPLEMENTATION.md` Phase 14): the **challenge details
screen**, and the **Elapsed: h:mm** readout.

Code: `app/core/challenge_manager.py` (timer, validation, file store),
`app/config/challenge_options.py` (the pick-lists),
`app/ui/challenge_dialog.py` (the screen), `app/ui/play_selector_widget.py`
(the referee's play selector), and the wiring in `ApplicationCoordinator`
and `MainWindow`.

## Challenge details

Pressing **Challenge** still does what it did: it closes the current clip,
opens a `challenge` clip and locks the referee window to the challenged
play for review. It now also **starts a timer and opens the challenge
details screen**, a non-modal window over the operator window.

The operator picks:

| Detail | Choices | Stored as |
|---|---|---|
| Team who challenged | The Team A and Team B names stored by the pre-match dialog (a blank name shows as "Team A" / "Team B") | `team_side` (`A` / `B`) and `team` (the name) |
| Challenge category | `[challenge] categories`; char(40), not null | `category` |
| Challenge outcome | `[challenge] outcomes`; char(15), not null | `outcome` |

**Challenge Done** is enabled only when all three are chosen. Pressing it
stops the timer, stores one record, closes the screen and ends the
referee's challenge lockout (the referee returns to live, as Next Play
does). The stored `elapsed_seconds` is the time from the Challenge press
to the first Challenge Done press.

**Discard** (after a confirmation) drops a mis-pressed challenge with no
record. Without it, an accidental press would force the operator to invent
values for the not-null fields.

### Referee: choosing which play to review

When Challenge is pressed the referee window is pinned to the play that
just ended. The operator may have pressed Next Play *before* the challenge
was called, in which case that play is tiny (the new one) and the call is
in the play before it. So the referee window has a **play selector** above
the transport: `◀ Earlier` / `Later ▶` around a `Play N of M` readout. It is
deliberately quiet, since it is a secondary control: short (30 px), thin-bordered,
muted buttons, with a hairline separator under it setting it apart from the
transport buttons.

- It lists every finished play of the current game (`ClipManager.closed_plays()`),
  so the referee can go back one play or several, and forward again.
- Each step re-pins the referee on that play: paused at its start, with the
  replay fence on its bounds, exactly as the Challenge press does for the
  first one. The review seek bar follows the new range.
- The selector is greyed out outside a challenge, like the rest of the
  referee transport, and ends with the lockout (Done, Discard, Next Play,
  Time-out or End Game).
- The stored record's `play_number` is **the play under review when Done is
  pressed**, not the play the challenge opened on, so an early Next Play
  does not leave the record naming the wrong play.
- The coordinator is the single source of truth: a press only asks
  `ApplicationCoordinator.review_challenge_play(n)`, which re-pins the replay
  and answers with `challenge_review_changed(selected, plays)`; the widget
  redraws from that, so the selector always shows the play actually under
  review.

### What the screen does *not* change

- The `challenge` clip row is untouched: it still ends on Next Play or
  Time-out, and the no-back-to-back rule still holds. Done does not open
  the next play.
- While the screen is open the Challenge button is off, so two details
  screens can never overlap. Next Play, Time-out and End Game still work
  with it open; the screen and its timer carry on until Done or Discard,
  because the timer is defined as Challenge press to Done press.
- It is **not modal**. An application-modal dialog would also block the
  referee window, which is being used for the review at that moment.
- It cannot be closed by Esc or the window's close button, so an open
  challenge is never lost by accident. If the app exits with one open,
  nothing is saved for it and a warning is logged.

### Where the records go (not the database)

`<session>/challenges.json`, one file per session, one entry per
challenge, each naming its game:

```json
{
  "version": 1,
  "session_id": "session_001",
  "challenges": [
    {
      "challenge_number": 1,
      "game_subdir": "game_001",
      "play_number": 1,
      "clip_number": 2,
      "team_side": "B",
      "team": "Opponent",
      "category": "Net / Antenna Faults & Interference",
      "outcome": "Reversed",
      "elapsed_seconds": 2.712,
      "started_session_time_ns": 5457144000,
      "ended_session_time_ns": 8169591100,
      "started_at": "2026-11-09T22:20:19.485103+00:00",
      "completed_at": "2026-11-09T22:20:22.143329+00:00"
    }
  ]
}
```

- `challenge_number` counts from 1 within each game, and continues after a
  Resume because it is read from the file.
- `play_number` is the play that was reviewed (the referee can step back
  from the one the challenge opened on); `clip_number` is the
  `challenge` clip, so a record can be matched to the `clips` rows (and
  to the video, through the session-time fields) without anything being
  written to the database.
- The file is rewritten atomically on every Done. A damaged file is kept
  aside as `challenges.json.corrupt` rather than overwritten.
- If the write fails the screen stays open with a message; pressing Done
  again retries, and the elapsed time stays at the first press.
- `ChallengeStore.load()` reads the records back for later use.

### Configuring the pick-lists

```toml
[challenge]
categories = ["Ball Ruled In or Out", "Player/Ball Contact",
              "Net / Antenna Faults & Interference", "Center-line Faults"]
outcomes = ["Confirmed", "Reversed", "Inconclusive"]
```

These are the built-in defaults, so the block is optional and either key
may be left out. An entry must be non-blank, unique (ignoring case) and
within its limit (40 / 15 characters), or the app refuses to start with a
message naming the key.

## Elapsed: h:mm

The operator rail shows `Elapsed: h:mm` above the Begin Game / End Game
button: the time since the operator pressed **Begin Game**, as whole
minutes (`0:07`, `1:05`).

- Counting starts at the Begin Game press and runs on the session clock.
- It is bright while the game is recording. After End Game it holds its
  final value, muted, until the next Begin Game restarts it from zero.
- After a Resume it counts from the crashed game's original start, so it
  includes the time lost to the crash.
- It refreshes once a second from a timer, so it keeps counting even if no
  frames are arriving.

## Tests

`test_challenge_options.py`, `test_challenge_manager.py`,
`test_challenge_coordinator.py`, `test_challenge_dialog.py`,
`test_operator_challenge_elapsed.py`, `test_challenge_play_review.py`. They
run offscreen; no GStreamer is needed.
