# Sports Replay Target Architecture

This document describes the intended production-oriented architecture for the sports replay application. It is a target design reference only; it does not imply that all pieces are implemented yet.

## Status Summary

The current codebase is a good vertical slice, not a finished foundation. Several items that were once “next refactors” are **already implemented** in tree.

**Implemented today (as of this document’s last pass against the code):**

- Pluggable ingest via `SourceInterface`
- **Feed-oriented** graph: `FeedRegistry`, one `FeedRuntime` per enabled feed, each with its own `PipelineManager`
- **Two output channels:** `ApplicationCoordinator` wires an **operator** and **program** `PlaybackController`, each with its own `MultiFeedOutputRenderer` and `MainWindow` (`main.py`). NATIVE feeds (NDI) render live preview directly via d3d11videosink into each window's child surface; replay flips the QStackedLayout to the QImage layer so segment-decoder frames stay visible (slice 3.A.3 retry). Multi-feed replay is synchronized via the per-app `SessionClock`; tiles for feeds without coverage at the rewound timestamp render a clamped freeze frame and surface a "FROZEN" badge in the corner (Phase 5 + Phase 6)
- **Native segmented recording:** each feed’s `PipelineManager` runs a `tee → valve → videoconvert → jpegenc → splitmuxsink` branch that writes one MKV per segment under `recording/<game_NNN>/<feed_id>/segment_NNNNN.mkv`. Each press of "Start game recording" allocates a fresh `game_NNN` subdir (`find_next_game_index` picks `max+1`); `fragment_index` resets to 0 each game (Phase 7.B-ext) — per-game folder isolation makes cross-game collisions impossible. Segment metadata (PTS span, frame count, file size, session-time bounds) is persisted to a SQLite `segments` table and indexed in-memory by `SegmentIndex` (slices 4.A / 4.B / 5.A). When the source has audio, the audio tee dynamically wires `audioconvert → audioresample → opusenc → splitmuxsink.audio_%u` once an audio buffer has been observed on the source's audio tee (slice 4.F + Phase 9.C); sources without audio produce video-only segments without operator intervention.
- **Reliable Stop/Start cycle:** `disable_file_recording` emits `splitmuxsink.split-now` while the valve is still open, sleeps briefly so the next buffer triggers the rotation (matroskamux writes the EBML trailer for the in-flight segment), then closes the valve and drops the post-split throwaway pending segment so it doesn't end up in the DB. `enable_file_recording` rebuilds the splitmuxsink element from scratch via `_rebuild_splitmuxsink_locked` whenever `_recording_was_disabled` is set — state-cycling alone (NULL→PLAYING) wasn't enough because splitmuxsink retains an internal "current file" pointer across state changes, which used to make post-Start buffers append to the previous game's last file. **Known artifact:** the rotation triggered by `split-now` opens an empty/short segment file that's then dropped from the DB; the file remains on disk and is unwatchable, but the startup recovery scan quarantines or marks it dirty on next launch
- **Transport overlay invariants:** `PlaybackController.seconds_behind_live` advances with wall-clock at sub-segment resolution by reading `SessionClock.now_session_time_ns()` (rather than per-segment PTS spans, which only update at 4s rotation boundaries). Slow-motion buttons (`Slow 1/2x`, `Slow 1/4x`) only change playback rate — they do NOT auto-rewind. Stopping recording clears stale "behind live" / "PAUSED" overlays back to fresh-startup state via `refresh_recording_state` → `_sync_replay_state_with_recording_locked`. Both operator and program controllers' `refresh_recording_state` is called immediately after `enable_file_recording` / `disable_file_recording` so overlays update without operator interaction
- **Crash recovery on startup (slices 4.E + §11.4 + Phase 7.D):** `app/storage/session_recovery.py` marks unfinished sessions `DIRTY` in `session.json`, walks `<session>/recording/` recursively to find every `segment_*.mkv` (both the legacy flat layout and the per-game nested `recording/<game_NNN>/<feed_id>/` layout), validates each via `cv2.VideoCapture`, quarantines corrupt segments to `<session>/quarantine/<feed_id>/`, and inserts `dirty` rows for valid in-progress files that lost their finalize step. `app/ui/recovery_dialog.py` then surfaces each dirty session in a modal **Resume / End and finalize / Discard** prompt before any new session is created. Resume is offered only on the most recent dirty session (older ones get only End-and-finalize / Discard). On Resume, `ApplicationCoordinator._setup_resume_continuation` reuses the crashed `game_NNN/` folder, rebases `SessionClock` past the latest pre-crash segment so post-resume session-time values don't collide, and seeds the per-game-feed fragment counter past existing files. Pre-crash plays whose `end_session_time_ns` is NULL are auto-closed with `auto_closed_on_crash = TRUE`.
- **Queue-depth observability and saturation health (slices 3.B + 3.C):** preview/record queues use explicit leaky / time-bounded policies; `TelemetryHub` samples per-feed queue depths once per tick, drives `RecordingState.RECORDING_ERROR` on sustained record saturation and `FeedState.DEGRADED` on sustained preview saturation, and surfaces transitional `python_push` feeds in a diagnostics-widget banner that escalates when `[app] app_mode = "production"`.
- **Replay query layer:** `RecordingSegmentReplayStore` (slice 4.C) wraps `SegmentIndex` and is the single eligibility / lookup contract used by `PlaybackController`. Per-game scoping (Phase 7.B-ext) — a `_current_game_start_session_time_ns` filter set by `toggle_long_session_recording` keeps replay scoped to the current game across Stop/Start cycles.
- **Disk-budget validation (Phase 7.A):** `app/core/disk_budget.py` estimates aggregate write throughput (`feed_count × frame size × fps × codec coefficient`) against `[recording] disk_budget_mb_s`. Verdict (OK / WARN / OVER_BUDGET) drives a startup log line, optional health event, and a diagnostics-widget readout. Catches misconfigured rigs before splitmuxsink wedges.
- **Latest-replayable surface in operator UI (Phase 7.B):** `UiState.latest_replayable_session_time_ns`, `live_lag_behind_replayable_seconds`, and `replay_available` are populated by `PlaybackController._update_state_timestamps_locked`; the status bar renders "Replay covers 0:00 – 0:42 (latest finalized −4s)" or "Replay not yet available — first segment finalizing" based on them.
- **Play markers + Replay Play (Phase 7.H):** `PlayManager` opens **Play #1** on every "Start game recording" press and persists boundaries to the `plays` SQLite table. The "Next Play" button closes the current play and immediately opens the next; "Stop game recording" closes the open play; the "Replay Play" transport button seeks to the currently-open play's `start_session_time_ns`. The current play number renders as `Play #N` on the playback overlay and operator status bar. Crash recovery auto-closes any NULL-end play with `auto_closed_on_crash = TRUE`.
- **Post-session MP4 processor (Phase 8):** `python -m app.tools.post_session_processor <session_path>` runs against a `FINALIZED` session, builds one long-form MP4 per `(game_subdir, feed_id)` via an ffmpeg subprocess (`-c:v libx264 -c:a aac`), writes a `<game>/plays.json` sidecar per game, and persists per-artifact bookkeeping to the `export_artifacts` SQLite table for idempotent re-runs. The live recording app refuses to encode while a session is active.
- **Segment-file replay rendering (slice 4.C.tail + Phase 5):** `SegmentDecoder` (a `cv2.VideoCapture` wrapper, `app/media/segment_decoder.py`) decodes MJPEG frames out of the recorded segments and hands them to `OutputRenderer.show_frame`. The replay clock advances in **session-time** (Phase 5.C) and `_render_at_session_time_ns` iterates every enabled feed via `nearest_frame_location` (Phase 5.B clamping rule), so feeds without coverage at the rewound timestamp render a clamped freeze frame with a "FROZEN" badge (Phase 6) rather than blanking. The "primary feed" reference still exists in a few `PlaybackController` paths but synchronized multi-feed replay works end-to-end.
- Separation between ingest, recording (segments), replay query, UI, and session storage
- GStreamer-centered `tee`/fan-out direction in each feed’s `PipelineManager`
- Controllers that track playback mode and transport (not raw button events only)

**Still open before the app matches the full “target” story in this document:**

- A first-class **`PlaybackSession`** model (timeline position, rate, per-output state) as a named type — today much of this lives inside `PlaybackController`.
- ProRes / DNxHR codec support and the `[recording] container = "mov"` path. MJPEG-in-MKV ships today; queued for Phase 11 alongside hardware-accelerated encoder selection.
- Phase 10 production hardening: source-loss policy across reconnects, drift correction across many feeds, disk-full / encoder-failure recovery beyond the existing health-event surface.
- The synthetic dev source remains on the Python-push path by design (it is the camera-less dev fallback). Production NDI ingest is fully native (Phase 3.A.2 + 3.A.3 retry); USB / OpenCV ingest was removed in Phase 2.5.

## Design Goals

- Support multiple simultaneous camera feeds, including NDI for configured feeds
- Two independent windows exist in the current app. Operator replay supports **1/2x and 1/4x** rates via `PlaybackController.set_playback_rate`; a richer **timeline + rate** model (as a first-class `PlaybackSession` type) is still future work.
  - Program window: live multiview only, no transport controls
  - Operator window: live multiview plus pause, replay, jump-to-live, and slow replay speeds
- Keep ingest and recording running while the operator pauses or replays
- Record one copy of each live feed for later review
- Keep future clip export and event logging straightforward
- Preserve room for professional concerns like source loss handling, timestamp alignment, and operator-safe failure modes

## Proposed Top-Level Model

The system should be organized around four layers:

1. Feed ingest layer
2. Recording and replay storage layer
3. Output composition and playback layer
4. UI/control layer

Each enabled feed is already a first-class `FeedDefinition` / `FeedRuntime` in the system; legacy single-feed mode without `[[feeds]]` still uses one effective feed from `[source]`.

## Core Runtime Components

### `FeedRegistry`

Owns the configured feeds and their metadata.

Responsibilities:

- Register feed identifiers and display names
- Track source type per feed (`ndi`, camera, file, synthetic test source)
- Expose health, format, fps, and availability metadata

### `FeedIngestService` (conceptual name)

**Current code:** one `FeedRuntime` per feed wraps `SourceInterface` + `PipelineManager` + `PreviewOutput`.

Responsibilities:

- Connect to a single source
- Normalize timestamps and frame metadata (ongoing)
- Drive a per-feed GStreamer ingest pipeline
- Fan out frames to the live preview path and the splitmuxsink-driven segment recorder

Notes:

- This matches the direction of `SourceInterface` + `PipelineManager` per feed
- NDI feeds use a per-feed `NDIReceiver`; they do not share one global source object

### `RecordingManager`

Owns the global `RecordingState` machine (`app/media/recording_manager.py`).

Responsibilities:

- Track whether long-form recording is active across all feeds
- Drive the start/stop transitions exposed to the operator UI

**On disk today:** under each session, `recording/<game_NNN>/<feed_id>/segment_NNNNN.mkv` holds that feed’s segmented MJPEG-in-MKV recording for one game (one Start/Stop cycle = one `game_NNN` subdir, with the index allocated by `find_next_game_index`). Segment metadata is persisted in the session’s SQLite `segments` table (slice 4.B). Slice 4.D removed the previous per-feed `Recorder` writer and `MuxedMediaWriter`/`ReplayBuffer` rolling-frame stores; recording is now driven entirely from inside each feed’s `PipelineManager` via `splitmuxsink`.

### `RecordingSegmentReplayStore` (slice 4.C)

Read-only query layer over `SegmentIndex` (`app/storage/segment_replay_store.py`).

Responsibilities:

- Enforce the §10.4 / §15.2 eligibility rule: replay is only available while `recording_state == RECORDING`
- Resolve a target PTS to `(segment_file, offset_in_segment_ns)` for completed segments only (§6.6)
- Report `(earliest_pts, latest_replayable_pts)` per feed for the operator UI

This is the only contract `PlaybackController` uses for replay eligibility. The actual decode/render of segment files into the operator output is **slice 4.C.tail** (deferred).

### `PlaybackSession` (target; not a separate class yet)

Represents what one output is currently showing. **Today** the closest implementation is **`PlaybackController`** (one per window) plus its private state and `AppState` for the UI.

There should be one logical playback session per output window, not one for the whole app (the app already runs two `PlaybackController` instances for operator vs program).

Responsibilities:

- Hold current mode (`live`, `paused`, `replay`)
- Hold current timeline position
- Hold playback rate (`1.0`, `0.5`, `0.25`, etc.)
- Hold selected feeds/layout for that output
- Compute "seconds behind live"

Extracting a dedicated `PlaybackSession` type from `PlaybackController` would make slow motion and rate changes easier to reason about.

### `OutputRenderer`

Owns a single displayed output surface or window.

Responsibilities:

- Render the selected set of feeds into a layout or multiview
- Bind to one native window handle
- Display either live or replay frames based on its `PlaybackSession`
- Show overlays appropriate for that output

There should be at least two output renderers:

- `program_output`
- `operator_output`

### `ApplicationCoordinator`

Owns the full runtime graph (see `app/core/application_coordinator.py`).

Responsibilities:

- Start feeds (`FeedRuntime.start` per enabled feed)
- Drive `RecordingManager.recording_state` transitions and call each feed’s `PipelineManager.enable_file_recording` / `disable_file_recording`
- Own the shared `SegmentIndex` and `RecordingSegmentReplayStore`, threading the index into each `PipelineManager` for segment-row inserts
- Own program and operator `PlaybackController` instances and route long recording / clip actions
- Keep the program controller in **live-only** mode while the operator controller handles pause/replay

`main.py` constructs the coordinator, two `MultiFeedOutputRenderer` instances, and two `MainWindow` instances (operator vs program).

## Window Model

The intended model is:

- Program window:
  - Live only
  - No transport controls
  - Can show a multiview of all cameras
- Operator window:
  - Can show live multiview
  - Can pause
  - Can replay last 10 seconds
  - Can replay in slow motion
  - Has transport controls

These windows should not fight over one shared preview surface.

Instead, each window should have:

- Its own `PlaybackSession`
- Its own `OutputRenderer`
- Its own widget binding/native window handle

With that structure:

- The operator can switch between live and replay without affecting program output
- The program window can remain continuously live
- A second window can be added safely without breaking the first one

## Multi-Camera Growth Path

Multi-feed ingest is **in place**: `FeedRegistry` + per-feed `FeedRuntime` + multiview-oriented `MultiFeedOutputRenderer`.

**Still rough for “true” multi-camera replay products:**

- Operator transport still leans on the **primary** feed in a few `PlaybackController` paths (`_primary_runtime`, `_primary_feed_id`); the multi-feed render loop (Phase 5.C) is correct, but a few transport edge cases haven't been fully de-primary'd.
- `AppState` is **per `PlaybackController`**, not global, but it still reflects one aggregate view of status for that window.
- Each feed has its own `PipelineManager` + `PreviewOutput` (the `Recorder` and replay-store instances were removed in Phase 4.D — recording is now driven from inside each feed's `PipelineManager` via `splitmuxsink`, and replay queries go through the single shared `RecordingSegmentReplayStore`).

Conclusion:

- Multi-feed is no longer “future only” at the object-graph level
- Deeper work remains for equal-class timeline and layout semantics across every feed

## Multi-Window Growth Path

**Implemented:** two top-level windows (operator + program), two `PlaybackController` instances, two `MultiFeedOutputRenderer` instances (`main.py`).

Remaining refinements:

- Keep program and operator semantics clear as features grow (e.g. program never shows replay)
- Ensure any new global shortcuts or focus behavior do not conflate the two outputs

The earlier “single shared surface swapping live/replay” limitation is **no longer** the current architecture.

## Multiple Per-Feed Recordings

**Implemented:** each feed’s `PipelineManager` writes its own `splitmuxsink` segment chain under `recording/<game_NNN>/<feed_id>/segment_NNNNN.mkv`. Segment metadata is persisted to a shared SQLite `segments` table and indexed in-memory (slice 4.B). Each press of "Start game recording" creates a fresh `game_NNN` subdir so a single game's footage can be copied off the system as one folder.

Multi-feed policy (when to start/stop all feeds together) lives in `ApplicationCoordinator.toggle_long_session_recording`. The coordinator allocates the `game_NNN` subdir once per Start and threads it into every feed's `enable_file_recording`, so all feeds in a single game share the same subdir name.

This does not require recorded video to be raw/unprocessed. A "copy of the live feed" with overlays is fine as long as:

- The overlay policy is explicit
- The timestamps are consistent
- Recording does not depend on what either window is currently displaying

## Slow Motion

**Current app:** the operator can set **1/2x** and **1/4x** replay via `set_playback_rate` (see `main_window` / `controls_widget`); the clock uses `PlaybackController`’s anchor + rate fields.

**Target rule** (for a cleaner long-term design):

- Replay state should always be explicit as: target timestamp, playback rate, and mode, ideally carried by a dedicated `PlaybackSession` (or factored-out state object) instead of ad hoc fields only.

Conclusion:

- Basic slow replay exists; the remaining step is to align it with a **timeline + rate** model everywhere (including multi-feed replay) rather than accreting more one-off cases in the controller

## Recommended Near-Term Refactor Order

1. ~~Introduce feed identifiers and a feed registry~~ — `FeedRegistry` and `[[feeds]]` configuration exist
2. ~~Split ingest ownership into per-feed services~~ — `FeedRuntime` + per-feed `PipelineManager`
3. ~~Replace the rolling JPEG buffer with native segmented muxers~~ — `splitmuxsink` recording branch + `SegmentIndex` + `RecordingSegmentReplayStore` (slices 4.A / 4.B / 4.C)
4. ~~Wire segment-file replay into the operator output~~ — `SegmentDecoder` + PTS-ns replay clock (slice 4.C.tail)
5. Introduce `PlaybackSession` (or factor state out of `PlaybackController`) as an explicit per-output model
6. ~~Per-window output renderers and a second window~~ — operator + program `MainWindow` and renderers
7. ~~Extend replay to multi-feed timeline semantics~~ — Phase 5 `SessionClock` + Phase 5.B / 5.C cross-feed render loop with §8.6.1 clamping. A few transport methods still lean on the primary feed; full removal is a small follow-up.
8. ~~Convert NDI ingest to a native GStreamer source bin~~ — Phase 3.A.2 + 3.A.3 retry
9. ~~Re-introduce embedded audio in segments~~ — slice 4.F + Phase 7.G + Phase 9.C dynamic wiring
10. Add ProRes / DNxHR codec support and the `mov` container path — queued for Phase 11
11. Phase 10 production hardening (source-loss policy, drift correction, disk-full recovery)

## Architectural Verdict

The project remains a **vertical slice** in terms of product maturity, but the **object graph** has already moved to **feeds**, **per-feed media stacks**, and **two outputs** (operator + program). New work should build on that — avoid reintroducing a single global source or a single shared preview as the only output path.

**Already aligned with the long-term story:**

- Multi-feed ingest, per-feed recording, two windows, feed registry

**Where complexity will grow next:**

- Explicit playback session / timeline model, slow motion, and symmetric multi-feed replay behavior — not more one-off special cases in one controller
