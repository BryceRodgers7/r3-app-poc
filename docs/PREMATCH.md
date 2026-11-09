# Pre-match setup

The operator fills in the match details and the camera layout in a dialog
that appears **before** the Operator and Referee windows exist. No feed is
running while it is open, so there is no live video behind it. Pressing
Begin Match (after an "Are you sure?") saves the answers, starts the
cameras, and opens the two windows.

Code: `app/config/prematch_config.py` (data + files),
`app/ui/prematch_dialog.py` (the dialog), `app/ui/prematch_flow.py` (open,
save), `app/ui/starting_cameras_window.py`, and the wiring in
`main.build_application`.

## Startup order

`main.main()` calls `build_application(prematch_setup=True)`:

1. Load `app_settings.toml`, apply the dark theme.
2. Crash-recovery check (`_run_startup_recovery_flow`), unchanged.
3. **Pre-match dialog** (`run_prematch_flow`). Skipped when the operator
   chose **Resume** in the recovery dialog: that match was entered before
   the crash, and the saved camera layout is loaded without a dialog.
4. Begin Match, then confirm. Both config files are written. Quit, then
   confirm, makes `build_application` raise `StartupCancelled`; `main()`
   returns 0 and no window, feed or session is created.
5. **"Starting cameras…" window** is shown and painted once.
6. Coordinator, both windows, `coordinator.initialize()` (starts the
   feeds), then the Starting window closes and `main()` shows the windows.

`build_application()` defaults to `prematch_setup=False`, so the scripted
NDI harnesses in `docs/ndi_test_harness` still start with no dialog.

`initialize()` runs on the main thread, so the event loop is blocked while
the feeds start. The Starting window is therefore static: its bar is an
unmeasured busy bar and will not animate. Animating it would need the
startup moved off the main thread (or `processEvents()` calls inside
`initialize()`), which is a larger change.

## Match details (tab 1)

| Field | Type | Required | Opens with |
|---|---|---|---|
| Date | date | yes | today |
| Scheduled start time | time, 24-hour | yes | `19:00` |
| Venue | char(50) | no | last saved value |
| Team A | char(30) | no | last saved value |
| Team B | char(30) | no | `Opponent` |
| Referee 1, Referee 2 | char(35) | no | blank |
| Line judge 1, Line judge 2 | char(35) | no | blank |

Every value can be changed. A blank optional field is stored as null (its
key is left out of the file). The limits are enforced by the entry boxes
(`maxLength`) and again when saving.

## Camera setup (tab 2)

One row per **active** (`enabled = true`) feed:

- **Order**: a position from 1 to N, N being the number of active feeds.
  Choosing a position another camera holds **swaps** the two cameras, so
  the positions are always a permutation of 1..N.
- **Configured source** (read-only): the feed's `display_name` from
  `app_settings.toml`, with its `ndi_name` in parentheses (the source kind
  for a synthetic feed). It lets the operator tell cameras apart without
  live video.
- **Short name**: char(20), required.

A small panel previews the viewport positions. Begin Match is disabled
while a short name is blank.

Nothing is saved until Begin Match is confirmed. Quit, Esc and the window's
close button all ask "Quit?" first.

## Config files

Both live in `base_data_dir` (next to `metadata.db`), are written only by
the app, and are not the database. `app_settings.toml` is never rewritten:
it is hand-edited, carries long comments, and the app has no TOML writer.

`match_details.toml`:

```toml
[defaults]                       # edit by hand; kept on every save
scheduled_start_time = "19:00"
team_b = "Opponent"

[match]                          # the most recent match
date = "2026-11-09"
scheduled_start_time = "19:00"
venue = "Riverside Sports Hall"  # venue and team_a seed the next dialog
team_a = "Lakeside Lions"
team_b = "Opponent"
```

`camera_layout.toml`, one table per camera:

```toml
[[cameras]]
feed_id = "ndi_1"
short_name = "Baseline A"
viewport_order = 1
```

The camera layout is **layered over** `[[feeds]]`
(`FeedRegistry.build_default` → `apply_camera_layout`): `short_name`
replaces the feed's `display_name`, and the feeds are ordered by
`viewport_order`. Feeds with no entry keep their `app_settings.toml` name
and follow the listed ones. An entry for a feed that no longer exists is
ignored. A missing file means today's behavior. The layout is also applied
on a Resume start. A feed that is disabled today keeps its saved entry (it
moves after the listed cameras) so its name returns when it is enabled
again.

A missing, damaged or hand-broken file never stops a start: it is logged
and the built-in defaults are used (an invalid `[[cameras]]` row is
skipped on its own). If a file cannot be written, the error is logged and
the match still starts with the values just entered; only the "remember
for next time" part is lost.

## Tests

`test_prematch_config.py`, `test_feed_registry_camera_layout.py`,
`test_prematch_dialog.py`, `test_prematch_flow.py`,
`test_starting_cameras_window.py`, `test_main_prematch_startup.py`. They
run offscreen; no GStreamer is needed.

To see it for real, start the app with `python main.py`.
