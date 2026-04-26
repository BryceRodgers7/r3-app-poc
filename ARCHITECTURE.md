# Sports Replay Target Architecture

This document describes the intended production-oriented architecture for the sports replay application. It is a target design reference only; it does not imply that all pieces are implemented yet.

## Status Summary

The current codebase is a good vertical slice, not a finished foundation. Several items that were once “next refactors” are **already implemented** in tree.

**Implemented today (as of this document’s last pass against the code):**

- Pluggable ingest via `SourceInterface`
- **Feed-oriented** graph: `FeedRegistry`, one `FeedRuntime` per enabled feed, each with its own `PipelineManager`, `Recorder`, and `ReplayBuffer` / `ReplayStore`
- **Two output channels:** `ApplicationCoordinator` wires an **operator** and **program** `PlaybackController`, each with its own `MultiFeedOutputRenderer` and `MainWindow` (`main.py`)
- **Per-feed recording:** `RecordingManager` holds one `Recorder` per `feed_id`; session paths use `recording/{feed_id}/` under each session
- Separation between ingest, replay storage, recording, UI, and session storage
- GStreamer-centered `tee`/fan-out direction in each feed’s `PipelineManager`
- Controllers that track playback mode and transport (not raw button events only)

**Still open before the app matches the full “target” story in this document:**

- A first-class **`PlaybackSession`** model (timeline position, rate, per-output state) as a named type — today much of this lives inside `PlaybackController`
- **Full** timeline- and rate-based replay (slow motion, independent timelines) without leaning on the primary feed for some replay paths
- Hardening for production: deployment, ops, source-loss policy, timestamp alignment across many feeds, etc.
- Replace or narrow **transitional** Python frame push + OpenCV fallbacks where native GStreamer sources should own the full graph

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

**Current code:** one `FeedRuntime` per feed wraps `SourceInterface` + `PipelineManager` + `PreviewOutput` + shared `Recorder` / `ReplayStore` for that feed.

Responsibilities:

- Connect to a single source
- Normalize timestamps and frame metadata (ongoing)
- Drive a per-feed GStreamer ingest pipeline
- Fan out frames to recording, replay buffering, and optional monitoring paths

Notes:

- This matches the direction of `SourceInterface` + `PipelineManager` per feed
- NDI feeds use a per-feed `NDIReceiver`; they do not share one global source object

### `RecordingManager`

Coordinates per-feed `Recorder` instances (`app/media/recording_manager.py`).

Responsibilities:

- Register one `Recorder` per `feed_id`
- Expose whether any feed is recording for UI state
- Stop all recorders on shutdown

**On disk today:** under each session, `recording/{feed_id}/` holds that feed’s muxed outputs and manifest when long recording is enabled (see `SessionPaths.get_feed_paths`).

The recorded files may contain burned-in timestamp/source overlays if desired. That does not conflict with this architecture.

### `ReplayStoreManager`

Owns rolling replay storage per feed.

Responsibilities:

- Maintain a replay buffer for each feed
- Resolve frames or timeline positions by timestamp
- Report oldest/latest available timestamps per feed
- Support replay window alignment across all feeds

Important:

- Replay should be modeled as timeline-based, not as a special UI trick
- Every feed should expose a timestamp-addressable replay timeline

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
- Register per-feed recorders and replay stores with `RecordingManager` / `ReplayStoreManager`
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

- Operator transport and replay **buffer selection** still center the **primary** feed in places (e.g. replay store lookup by primary `feed_id` in `PlaybackController`); independent per-feed replay timelines are not fully modeled
- `AppState` is **per `PlaybackController`**, not global, but it still reflects one aggregate view of status for that window
- Each feed already has its own `PipelineManager`, `Recorder`, `PreviewOutput`, and replay store instance

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

**Implemented:** one `Recorder` per feed, registered in `RecordingManager`, with per-feed directories under `recording/{feed_id}/`.

Session-level manifests and multi-feed policy (when to start/stop all feeds together) live in `ApplicationCoordinator.toggle_long_session_recording` and related UI.

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
2. ~~Split ingest/record/replay ownership into per-feed services~~ — `FeedRuntime` + per-feed `PipelineManager` / `Recorder` / `ReplayStore`
3. Introduce `PlaybackSession` (or factor state out of `PlaybackController`) as an explicit per-output model
4. ~~Per-window output renderers and a second window~~ — operator + program `MainWindow` and renderers
5. Extend replay to **true** per-feed (or cross-feed) timeline semantics, not only primary-feed replay where applicable
6. Add slow-motion playback rates consistently with a timeline + rate model
7. Harden NDI and non-test sources; reduce reliance on `TestSource` / Python-pushed frames where the graph should be native end-to-end

## Architectural Verdict

The project remains a **vertical slice** in terms of product maturity, but the **object graph** has already moved to **feeds**, **per-feed media stacks**, and **two outputs** (operator + program). New work should build on that — avoid reintroducing a single global source or a single shared preview as the only output path.

**Already aligned with the long-term story:**

- Multi-feed ingest, per-feed recording, two windows, feed registry

**Where complexity will grow next:**

- Explicit playback session / timeline model, slow motion, and symmetric multi-feed replay behavior — not more one-off special cases in one controller
