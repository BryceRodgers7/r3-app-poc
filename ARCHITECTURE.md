# Sports Replay Target Architecture

This document describes the intended production-oriented architecture for the sports replay application. It is a target design reference only; it does not imply that all pieces are implemented yet.

## Status Summary

The current codebase is a good vertical slice, not a finished foundation.

What is already heading in the right direction:

- Pluggable ingest via `SourceInterface`
- Separation between ingest, replay storage, recording, UI, and session storage
- A GStreamer-centered `tee`/fan-out direction in `PipelineManager`
- A controller layer that already thinks in terms of playback state instead of raw UI events

What must change before the app grows into a production replay system:

- Replace the single-source app object graph with a feed-oriented architecture
- Replace the single active video surface with independent output channels
- Replace the single recorder with per-feed recording services
- Replace fixed-speed replay with timeline + rate based playback control

## Design Goals

- Support multiple simultaneous camera feeds, including future NDI ingest
- Support two independent windows:
  - Program window: live multiview only, no transport controls
  - Operator window: live multiview plus pause, replay, jump-to-live, and slow motion
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

Each live camera feed should become a first-class object in the system instead of a detail hidden behind one global `source`.

## Core Runtime Components

### `FeedRegistry`

Owns the configured feeds and their metadata.

Responsibilities:

- Register feed identifiers and display names
- Track source type per feed (`ndi`, camera, file, synthetic test source)
- Expose health, format, fps, and availability metadata

### `FeedIngestService`

One instance per feed.

Responsibilities:

- Connect to a single source
- Normalize timestamps and frame metadata
- Drive a per-feed GStreamer ingest pipeline
- Fan out frames to recording, replay buffering, and optional monitoring paths

Notes:

- This is the natural evolution of the current `SourceInterface` plus `PipelineManager`
- For NDI, each feed should have its own receiver/pipeline rather than sharing one global source object

### `RecordingManager`

Owns the full-session recordings.

Responsibilities:

- Start and stop recording for all active feeds
- Create one recorder per feed
- Write one output file set per feed
- Persist recording manifests and session metadata

Expected output layout:

- `session_xxx/recording/feed_cam1/...`
- `session_xxx/recording/feed_cam2/...`
- `session_xxx/recording/feed_cam3/...`

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

### `PlaybackSession`

Represents what one output is currently showing.

There should be one `PlaybackSession` per output window, not one for the whole app.

Responsibilities:

- Hold current mode (`live`, `paused`, `replay`)
- Hold current timeline position
- Hold playback rate (`1.0`, `0.5`, `0.25`, etc.)
- Hold selected feeds/layout for that output
- Compute "seconds behind live"

This is the key step that makes slow motion and independent windows safe to add.

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

Owns the full runtime graph.

Responsibilities:

- Start feeds
- Start per-feed recorders and replay stores
- Create program and operator playback sessions
- Route operator commands to the operator playback session only
- Keep the program output pinned to live unless intentionally changed later

This is the natural replacement for the current single-controller bootstrap in `main.py`.

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

Multi-camera support is still safe to add, but it should be added by lifting the architecture from "one source" to "many feeds" before more features pile onto the current single-source assumptions.

Good news:

- `SourceInterface` is a useful seam
- Session storage concepts are reusable
- Per-feed overlay metadata already exists in spirit through `feed_id`/`source_name`

Current limitations to address:

- `AppState` is global and single-source
- `PipelineManager` owns one source and one active video output
- `Recorder` owns one output file
- `PreviewOutput` binds to one widget

Conclusion:

- Multi-camera is not precluded
- It is not safe to bolt on by duplicating the current singleton-style objects
- It is safe if the next architectural step is feed-oriented orchestration

## Multi-Window Growth Path

Multi-window support is also still safe to add, but only if output ownership is separated first.

Good news:

- Qt can host multiple windows cleanly
- The current controller already separates playback state from raw UI button wiring

Current limitation:

- The current media layer swaps one shared surface between live and replay

Conclusion:

- A second window is absolutely possible
- One window can be live-only while the other has live/replay controls
- That should be implemented by creating independent output renderers/playback sessions, not by teaching one shared output object new tricks

## Multiple Per-Feed Recordings

Multiple file recordings are still safe to add.

Recommended design:

- One recording branch per feed
- One recorder instance per feed
- One manifest per feed plus a session-level manifest

This does not require recorded video to be raw/unprocessed. A "copy of the live feed" with overlays is fine as long as:

- The overlay policy is explicit
- The timestamps are consistent
- Recording does not depend on what either window is currently displaying

Conclusion:

- The current architecture does not preclude this
- The current `Recorder` class is too narrow and should evolve into a manager + per-feed recorder model

## Slow Motion

Slow motion is still safe to add later if replay is promoted from a fixed 1x timer into a timeline/rate model.

Recommended rule:

- Replay state should always be expressed as:
  - target timestamp
  - playback rate
  - mode

Then the same operator actions become straightforward:

- Pause: rate `0.0`
- Replay: rate `1.0`
- Slow motion: rate `0.5` or `0.25`
- Jump to live: mode `live`, timestamp pinned to live edge

Conclusion:

- Slow motion is not blocked
- It should be added after `PlaybackSession` exists, not as a one-off special case inside the current controller

## Recommended Near-Term Refactor Order

1. Introduce feed identifiers and a feed registry
2. Split ingest/record/replay ownership into per-feed services
3. Introduce `PlaybackSession` as an independent per-output state model
4. Replace the single preview binding with per-window output renderers
5. Add the second window
6. Add slow-motion playback rates
7. Add real NDI ingest

## Architectural Verdict

The project is on the right track only if the current code is treated as a vertical slice and not as the final application shape.

The existing design does not preclude:

- Multi-camera support
- A second live-only window
- Independent live/replay operator output
- Multiple feed recordings
- Slow-motion replay

But the next steps matter. If new features are added directly onto the current single-source, single-output object graph, the design will become brittle quickly. If the next step is to introduce feed-oriented ingest and per-output playback sessions, the project can grow into the intended system safely.
