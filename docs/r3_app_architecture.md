# r3-app Production Architecture Design Document
## Multi-Feed Sports Recording, Instant Replay, and Clip Export System

**Document purpose:**  
This document defines the target production architecture for a Windows desktop multi-feed sports recording and replay application.

**Important:**  
This is the authoritative architecture document. Do not treat any unstated media, storage, timing, or replay behavior as an implementation detail. The decisions below are part of the design.

**Status:**  
This document describes the **target** production architecture. The current codebase does not yet conform to it — notably, today's replay storage uses JPEG thumbs plus short muxed segments, frames are pushed through Python on the hot path, and the active recording container is MP4. Those are explicitly forbidden by this document and are tracked as gaps to close in the phased plan in §18. For a description of the current code's object graph, see `ARCHITECTURE.md`.

**Source surface:**  
Production deployments use **NDI cameras only** (`kind = "ndi"`). A synthetic test source (`kind = "synthetic"`) exists for development on machines without NDI hardware and is intentionally never productionized — it is the dev-time fallback the §17.3 guardrail protects, not a parallel production path. USB / OpenCV / GStreamer-camera ingest paths from earlier proof-of-concept work were removed in Phase 2.5; they were a prototype convenience and never an architectural target.

---

# 0. Executive Summary

The application records a live sports game from multiple camera angles, shows live feeds to an operator, allows time-shifted playback on an operator display while recording is active, keeps a separate program display live-only, records long game files, and creates short clips for individual plays. It is currently between Proof-of-Concept and Production-Ready stages.

The production architecture must be:

- **GStreamer-centered** for all real-time media processing.
- **Python-controlled**, but not Python-frame-driven.
- **Timestamp-first**, not frame-count-first.
- **Segment-based**, not raw-frame-based.
- **Multi-feed synchronized** from the start.
- **Failure-tolerant**, because real games cannot be repeated.
- **Observable**, because media systems fail in subtle ways.
- **Phased**, so the existing proof-of-concept can evolve safely.

The most important production shift is:


From:
    Python-managed frames + JPEG/frame dumps + partial replay semantics

To:
    GStreamer-managed media pipelines + timestamped encoded segments + shared session timeline


# 1. Product Requirements

## 1.1 Core user-facing capabilities

The system must support:

1. Multiple simultaneous camera/video feeds.
2. Live multiview operator display.
3. Manual game recording start/stop.
4. Instant replay shortcuts for the last N seconds while recording is active, plus operator replay/scrubbing back to the beginning of the current recording using completed recording segments.
5. Pause, rewind, slow-motion, and jump-to-live for the operator view.
6. Clip marking for individual plays as metadata.
7. Long-form game recording.
8. Session metadata, segment metadata, and play metadata stored durably.
9. Manual post-session MP4 processing after the recording app is shut down.
10. Graceful degradation if a feed disconnects or falls behind.

## 1.2 Non-goals for the first production refactor

Do not prioritize these until the core media architecture is stable:

- Cloud upload.
- Remote streaming.
- Automated highlight detection.
- Multi-user collaboration.
- Advanced telestration.
- Broadcast-grade graphics overlay system.
- Distributed multi-machine ingest.
- Cloud transcoding.
- Web/mobile viewer.

These can be added later only if the timeline, segmented recording store, post-session processing model, and media pipeline architecture are correct.

---

# 2. Architectural Principles

## 2.1 Python is the control plane

Python should:

- Load configuration.
- Create and control GStreamer pipelines.
- Manage UI state.
- Start and stop recording.
- Request replay seeks.
- Maintain session metadata.
- Maintain segment indexes.
- Coordinate feed state.
- Surface metrics and errors.

Python should not:

- Push every video frame through Python in production.
- Pull every frame into Python just to display it.
- Encode video frames directly in Python.
- Store raw JPEG/PNG frames for replay.
- Use frame counts as the main replay timeline.
- Act as the primary media scheduler.

## 2.2 GStreamer is the data plane

GStreamer should handle:

- Source ingestion.
- Decode/convert where necessary.
- Fan-out using `tee`.
- Thread isolation using `queue` or `multiqueue`.
- Encoding.
- Muxing.
- Segment writing.
- Live preview sinks.
- Replay playback pipelines.
- Audio/video synchronization.
- Hardware acceleration where supported.

## 2.3 All media is timestamp-driven

The system must use timestamps as the source of truth.

Required:

- Use monotonic time for session timeline logic.
- Use GStreamer PTS/DTS for media placement.
- Map feed-local PTS to session time.
- Store segment start/end timestamps.
- Use timestamps for replay seek.
- Use timestamps for clip boundaries.

Forbidden:

- Using frame number as the primary timeline.
- Assuming every feed has identical FPS.
- Assuming all feeds start at the same instant.
- Assuming no frame drops.
- Assuming wall-clock time is suitable for media sync.

## 2.4 Segment-based recording and replay

All recording media used for replay must be stored as encoded media segments.

Forbidden in production replay:

- JPEG-per-frame replay buffers.
- PNG-per-frame replay buffers.
- Raw frame folders.
- One giant unbounded recording file for the active recording path.
- MP4 files as the active recording/replay container.

## 2.5 Branches must be isolated

A slow branch must not block a live branch.

Every `tee` output branch must have a queue.

Different branches have different policies:

- Preview: low latency, may drop frames.
- Game recording: reliability-first, should not silently drop.
- Replay read path: reads from completed recording segments from the current active recording while recording is active.

---

# 3. Target High-Level Architecture

## 3.1 Conceptual architecture

```text
                       ┌────────────────────────┐
                       │     Application UI      │
                       │ Operator UI   │
                       └───────────┬────────────┘
                                   │
                                   ▼
                       ┌────────────────────────┐
                       │ ApplicationCoordinator │
                       └───────────┬────────────┘
                                   │
     ┌─────────────────────────────┼─────────────────────────────┐
     ▼                             ▼                             ▼
┌──────────────┐           ┌────────────────┐            ┌────────────────┐
│ FeedRuntime A│           │ FeedRuntime B  │            │ FeedRuntime C  │
└──────┬───────┘           └───────┬────────┘            └───────┬────────┘
       │                           │                             │
       ▼                           ▼                             ▼
┌──────────────┐           ┌────────────────┐            ┌────────────────┐
│ GStreamer    │           │ GStreamer      │            │ GStreamer      │
│ Feed Pipeline│           │ Feed Pipeline  │            │ Feed Pipeline  │
└──────┬───────┘           └───────┬────────┘            └───────┬────────┘
       │                           │                             │
       └──────────────┬────────────┴─────────────┬──────────────┘
                      ▼                          ▼
            ┌────────────────────────────────────────────┐
            │ RecordingStore + Active Replay Segment Index│
            └───────────────────────────┬────────────────┘
                                        ▼
            ┌────────────────────────────────────────────┐
            │ SQLite Metadata + Session Folder Structure │
            └────────────────────────────────────────────┘

                    ┌────────────────────────┐
                    │ Shared SessionTimeline │
                    │ SessionClock + PTS Map │
                    └────────────────────────┘

                    ┌────────────────────────┐
                    │ PlaybackControllers    │
                    │ Operator + Program     │
                    └────────────────────────┘
```

## 3.2 Runtime responsibilities

### ApplicationCoordinator

Owns the long-lived application graph.

Responsibilities:

- Load config.
- Create feeds.
- Create session.
- Own shared `SessionClock`.
- Own `RecordingManager`.
- Own replay index/query services for active recording segments.
- Own operator and program playback controllers (program is live-only; operator has full transport).
- Coordinate startup/shutdown.
- Surface health state to UI.

Must not:

- Process raw video frames.
- Implement codec-specific logic directly.
- Contain feed-specific pipeline details.

### FeedRuntime

One instance per feed.

Responsibilities:

- Own feed identity.
- Own feed-specific GStreamer pipeline.
- Own feed state.
- Publish feed metrics.
- Register segment metadata with stores.
- Handle feed reconnect/recovery.
- Expose preview/program endpoints as needed. Replay playback reads from the recording store rather than from a feed pipeline branch.

Must not:

- Assume it is the primary feed.
- Use global state for timestamps.
- Block other feeds on failure.

### PipelineManager

Builds and controls the GStreamer pipeline for a feed.

Responsibilities:

- Construct source pipeline.
- Add `tee`.
- Add branch queues.
- Configure encoders/muxers/sinks.
- Monitor bus messages.
- Expose state changes.
- Support branch enable/disable.

Must not:

- Write replay metadata directly without going through the store/index API.
- Push frames through Python in production mode.

### RecordingManager

Manages long-form game recording.

Responsibilities:

- Start/stop recording.
- Track per-feed recording segments.
- Mark incomplete segments.
- Finalize recording sessions.
- Expose active-recording segment lookup for replay.
- Provide session metadata needed by the post-session processor after application shutdown.
- Refuse post-session processing while the app is recording or the session is not finalized.

### PostSessionProcessor

A separate manually run program in the same repository.

Responsibilities:

- Accept a finalized session folder as input.
- Refuse to run while the recording application is active.
- Read `session.sqlite`, recording segment metadata, and play markers.
- Create one long-form MP4 per feed/game recording.
- Write one `plays.json` sidecar per game, listing the play boundaries that downstream tooling can use to slice the long-form MP4.
- Record output artifacts and failures in metadata.
- Preserve source recording segments unchanged.

Must not:

- Run as part of the live recording hot path.
- Modify or delete source recording segments.
- Depend on replay being available after recording stops.

### PlaybackController

Controls output playback.

There should be separate playback controllers for:

- Operator output.
- Program output.

Operator playback supports:

- live
- pause
- rewind
- slow motion
- replay navigation from recent instant-replay shortcuts back to recording start
- jump to live
- feed selection / multiview replay


---

# 4. Media Pipeline Design

## 4.1 Required per-feed pipeline shape

Each feed must have one source pipeline split into two branches:

SOURCE
  → decode / convert if needed
  → timestamp + caps normalization
  → tee
      ├─ live preview branch
      └─ game recording branch

Example conceptual branch layout:

                     single video source
                              ↓
                   decode / convert if needed
                              ↓
                timestamp + caps normalization
                              ↓
             tee
              ┌──────────────┴──────────────┐
              ↓                             ↓
        live preview                   game recording
        low latency                    full session
        may drop                       reliability-first


## 4.2 Queue rules

Every branch after a `tee` must have a queue.

Mandatory branch policies:

| Branch | Queue behavior | Reason |
|---|---|---|
| Preview | bounded, leaky downstream | Keep UI live; drop old frames |
| Game recording | non-leaky unless failure mode is declared | Do not silently corrupt recording |

The implementation must explicitly configure queue sizes. Do not rely on defaults for production.

## 4.3 Live preview latency

Preview and program branches should prioritize latency over completeness.

Required behavior under load:

- Drop stale frames rather than growing delay.
- Never allow preview delay to grow unbounded.
- Surface a warning if sustained drops occur.

Target:

preview latency: ideally < 200 ms
operator UI responsiveness: no visible stalls during recording
program output: live-only, low-latency


## 4.4 Recording branch reliability

The recording branch prioritizes completeness.

Required behavior:

- Do not silently drop recording frames.
- If disk or encoder cannot keep up, mark feed/session degraded.
- Surface visible warning to operator.
- Continue other feeds if one feed fails.
- Never allow recording failure to freeze program output.

## 4.5 Replay behavior from active recording files

Replay reads from completed recording segments in the current active recording while recording is running.

Required:

- Fixed-duration recording segments.
- Timestamped segment metadata.
- Fast lookup by timestamp against completed active recording segments from recording start through the latest replayable finalized segment.
- Replay disabled when recording is stopped.
- Currently-writing segments are not required to be replayable until finalized.
- The UI must report replay coverage as available-through time, because the latest few seconds may still be in a writing segment.
- Keyframe-aware segment boundaries where applicable.

---

# 5. Codec and Container Strategy

## 5.1 Core design principle

Replay systems should optimize for:

1. Fast seeking.
2. Smooth scrubbing.
3. Deterministic playback.
4. Crash tolerance.
5. Low CPU decode.
6. Acceptable disk usage.

They should not optimize first for smallest file size.

## 5.2 Active recording format for replay

The active recording format is the critical path for instant replay.

Preferred codecs:

1. **ProRes**
2. **DNxHR**

Acceptable fallback:

3. **MJPEG**

Optional/testing only:

4. FFV1 / UTVideo, when disk budget and playback support are validated.

> **Currently shipped:** MJPEG-in-MKV only. `recording_codec` and `recording_container` accept other values in TOML but the pipeline only wires the MJPEG/MKV path. ProRes / DNxHR encoders live in `gst-plugins-bad`, which isn't always present on UCRT64; the disk-budget estimator's codec-coefficient table (`app/core/disk_budget.py`) also has only an MJPEG entry today. Codec selection is queued for Phase 11 because hardware acceleration ties into encoder choice — see Phase 11 task list.

Avoid for replay:

- H.264
- H.265
- Any long-GOP inter-frame codec

Reason:

- H.264/H.265 require decoding from previous keyframes.
- Scrubbing and reverse/slow-motion replay become more complex.
- Seek latency becomes less predictable.
- AI coding assistants will otherwise default to H.264/MP4, which is wrong for the replay path.

## 5.3 Active recording container

Acceptable containers for active recording segments:

- `.mov`
- `.mxf`
- `.mkv`
- `.ts` when appropriate

Avoid for active recording:

- `.mp4`

Reason:

- MP4 often requires finalization metadata.
- Active rolling buffers and crash recovery are harder with MP4.
- MP4 is suitable for post-session exports, not the active recording path.

> **Currently shipped:** MKV only. The splitmuxsink branch and the on-disk segment naming in `app/media/pipeline_manager.py` are wired specifically for matroskamux. Other values for `[recording] container` are accepted by the TOML loader but would fail at pipeline-build time. `.mov` (the natural ProRes / DNxHR container) is queued alongside Phase 11's codec work — see §5.2 and Phase 11's TODO sub-item (d).

## 5.4 Long game recording format

The system uses one segmented format for long recording and active replay reads.

Required:

- Long game recording is the replay source while recording is active.
- Preferred codecs: ProRes or DNxHR.
- Acceptable fallback: MJPEG.
- Segment duration default: 4 seconds.
- Active long recording must not use MP4.
- H.264 MP4 is not used by the live recording application. H.264/AAC MP4 is used only by the post-session processor for deliverables.

Rationale:

Using the same format for replay and long recording keeps the system simpler and more reliable:

- one segment format
- one timestamp/indexing model
- one recovery model
- fast seeking
- simpler replay and clip export
- fewer codec/container edge cases

Storage usage will be higher, but this is acceptable for the initial production architecture. Disk capacity and throughput must be validated separately.

Post-session requirement:

A separate post-session processing program runs after application shutdown and transcodes a completed session into MP4 deliverables.

## 5.5 Segment duration

Recommended:

2–5 seconds per segment

Tradeoff:

- Shorter segments = faster replay lookup and recovery, more files.
- Longer segments = fewer files, slower seek/recovery.

Default:

segment_duration_seconds = 4

## 5.6 Keyframe strategy

For intra-frame codecs:

- Every frame is effectively seekable.

For inter-frame codecs if used:

- Force keyframes at segment boundaries.
- Use short GOP.
- Store keyframe index.
- Do not assume arbitrary frame-accurate seek is cheap.

---

# 6. Storage and File Layout

## 6.1 Base directory

Default:

```text
C:\SportsReplay
```

Configurable via:

```toml
[storage]
base_data_dir = "C:\\SportsReplay"
```

## 6.2 Session directory structure

```text
C:\SportsReplay\
  metadata.db                    -- shared across every session
  sessions\
    session_001\
      session.json               -- session-state manifest (§14.2)
      logs\
        health_events.jsonl      -- §14.6
      recording\
        game_001\                -- one subdir per Start/Stop cycle (§6.2.1)
          feed_001\
            segment_00000.mkv
            segment_00001.mkv
          feed_002\
            segment_00000.mkv
        game_002\
          feed_001\
            segment_00000.mkv
      processed\                 -- created by the post-session processor
        game_001\
          feed_001.mp4
          feed_002.mp4
          plays.json             -- §6.7 / §8.D
      quarantine\                -- created on demand by the recovery scan
        feed_001\
          segment_00007.mkv
```

### 6.2.1 Per-game recording subdirectory

Each press of "Start game recording" allocates a fresh `game_NNN/` subdirectory under `recording/`. Inside it, every enabled feed gets its own `<feed_id>/segment_NNNNN.mkv` file series. `fragment_index` resets to 0 at the start of each new game; cross-game collisions are impossible because the game folder is part of the path.

The Phase 7.D resume-continuation flow (§11.4 Resume) reuses the crashed game's existing `game_NNN/` instead of allocating `game_(N+1)/`. In that case `find_next_fragment_index` walks the existing folder and picks `max+1` past the pre-crash files, so segment filenames continue monotonically within the game.

Sessions are named `session_NNN` (monotonic per `base_data_dir`); `metadata.db` lives at the base directory level, not inside each session, so the post-session processor can address every session through a single SQLite file.

## 6.3 Segment metadata

Each segment must have metadata:

```text
segment_id
session_id
feed_id
fragment_index
file_path
codec
container
start_pts_ns
end_pts_ns
start_session_time_ns
end_session_time_ns
duration_ns
start_wall_clock_utc
end_wall_clock_utc
frame_count_estimate
size_bytes
state: writing | complete | dirty | corrupt | quarantined
pts_to_session_offset_ns
created_at
finalized_at
```

Each segment carries its own `pts_to_session_offset_ns`. The feed clock is allowed to jump across a reconnect — a new segment is started with a fresh offset. The feed timeline is therefore not stored as a separate table; it is a runtime view computed from segments (see §14.1).

`first_keyframe_pts_ns` / `last_keyframe_pts_ns` are required only when an inter-frame codec is in use (§5.6). For the intra-frame codecs §5.2 mandates (ProRes, DNxHR, MJPEG) every frame is a keyframe and the keyframe PTS is identical to the segment's `start_pts_ns` / last frame, so the columns add no information; the current MJPEG-only implementation omits them.

## 6.4 Metadata storage

SQLite is appropriate for durable metadata.

Hot path lookups should use in-memory indexes.

Pattern:

```text
SQLite:
    durable source of truth

In-memory index:
    fast lookup for replay operations

Startup:
    scan session folders + SQLite
    rebuild in-memory indexes
```

## 6.5 Atomic segment finalization

Segment write process must distinguish:

```text
writing → complete
writing → dirty
dirty → recovered
dirty → quarantined
```

Recommended approach:

- Write segment as temporary/in-progress file.
- Finalize/mux properly.
- Rename atomically or mark complete in metadata only after success.
- On startup, scan for incomplete files.
- Attempt recovery when possible.
- Quarantine corrupt segments.
- Never allow one corrupt segment to break the entire session.

## 6.6 Replay eligibility and retention policy

Replay eligibility is a query policy over completed recording segments in the current active recording, not a separate media-retention system.

Config:

```toml
[replay]
enabled = true
requires_recording = true
default_instant_replay_seconds = 60
quick_replay_seconds = [10, 30, 60, 120]
max_replay_scope = "current_recording"
completed_segments_only = true
```

> **Currently shipped:** the `[replay]` block is not read by the loader (see §13.2). The behaviors it would gate are already correct as hard-coded constants: `requires_recording = true` is enforced unconditionally, `completed_segments_only = true` is enforced by `RecordingSegmentReplayStore` (Phase 7.C lock-in), and the operator UI ships only the **Rewind 10s** shortcut today (the 30 / 60 / 120 buttons listed above are queued — see §15.3).

Rules:

- Replay is unavailable unless recording is active.
- Replay may use completed recording segments from the current active recording, from recording start through the latest replayable finalized segment.
- Currently-writing segments are excluded from replay until finalized.
- Never delete recording segments as part of replay navigation or instant-replay shortcut management.
- If disk pressure is severe, enter degraded mode and warn operator rather than evicting recording media.

## 6.7 Play markers

Every moment of a recording belongs to a play. A play is a contiguous span of session time bounded by operator-driven markers; it carries no media of its own — its truth is the time range, which the replay path and the post-session processor consult against the existing segment store.

Lifecycle:

- The first press of "Start game recording" implicitly opens **Play #1** at the same session time as the recording's first segment.
- The "Next Play" button (replacing the slice 4.D `advance_short_segments` stub) closes the currently-open play and immediately opens the next one. There is no "between plays" state.
- "Stop game recording" closes the currently-open play. The next "Start game recording" creates a new game and opens its own Play #1; the play counter resets per game.
- A `plays` SQLite table is the durable source of truth. Each row stores `play_number` (1-based, per game), `start_session_time_ns`, and `end_session_time_ns` (NULL while a play is currently open).

A play row stores:

```text
play_id
session_id
game_subdir
play_number              -- 1-based, unique within a game
start_session_time_ns
end_session_time_ns      -- NULL while open
created_at
auto_closed_on_crash     -- TRUE when filled in by the resume path
```

`UNIQUE(session_id, game_subdir, play_number)` enforces the per-game numbering.

The play list is **operator-scoped, not feed-scoped** — there is one play sequence per game that applies to every feed. A future per-feed override is unnecessary and would complicate the boundary semantics.

Crash and resume:

- If the app crashes mid-play, the on-disk row for the open play has `end_session_time_ns = NULL`. The §11.4 resume path closes it: `end_session_time_ns` is set to the latest finalized segment's `end_session_time_ns` (the last frame the operator could possibly have seen) and `auto_closed_on_crash = TRUE` is set as a forensic flag.
- The first "Next Play" press after resume opens the next play in sequence, just like a normal boundary tap.

Replay use case:

- A new transport button **"Replay Play"** seeks playback to the currently-open play's `start_session_time_ns` and resumes at 1.0x. To go further back, the operator stacks Rewind 10s clicks (the existing per-10-second offset path).
- The current play number is rendered on the playback overlay (`Play #N`) so the operator always knows what they're inside of.

Post-session export:

- Alongside each `<game_NNN>/<feed>.mp4`, the post-session processor writes a single `<game_NNN>/plays.json` sidecar describing the play boundaries for that game. The JSON is per-game (not per-feed) because plays are operator-scoped. Editors / scrubbers / scoring tools consume the JSON to navigate the matching MP4.
- The processor does not produce per-play sub-clips. The clip model is metadata-first; downstream tooling slices the long-form MP4 using `start_seconds` + `length_seconds` from the JSON when sub-clips are needed.

## 6.8 Retention and cleanup

The live recording app **never** deletes anything on disk. Cleanup is a separate, manually invoked operation (a flag on the post-session processor or a dedicated CLI), never an automatic action during a session.

> **Currently shipped:** the "never delete" half is satisfied — no code path in the live app calls `Path.unlink` against recording media; `tests/test_replay_safety_invariants.py` locks this in for the operator transport methods. The manual cleanup tool prescribed below is **not yet implemented**: there is no `[retention]` reader, no `--cleanup-older-than` flag on the post-session processor, and no standalone cleanup CLI. Tracked in §13.2 ("Future config sections") with the deferral rationale; the right tool shape (subcommand of the post-session processor vs. separate CLI) will be picked the first time a deployment generates enough on-disk volume to need it.

Config:

```toml
[retention]
keep_source_segments_days = 14
keep_processed_exports_days = 90
keep_quarantine_days = 7
```

Rules:

- The cleanup operation is idempotent.
- Cleanup may only delete from sessions other than the one currently being processed.
- Cleanup never deletes from a session in state `RECORDING`, `STOPPED`, or `DIRTY` (see §10.6).
- Disk-pressure warnings (§11.3) remain visible to the operator, but degraded mode never deletes recording media.
- Quarantined segments expire on the shortest clock; they exist for postmortem only.

This is intentional for the volunteer-operator use case: nothing the operator does during a game can lose footage, and the league/tournament organizer has one explicit knob to manage disk space between events.

---

# 7. Disk, CPU, GPU, and Memory Budgeting

## 7.1 Disk throughput is a first-class requirement

The system must calculate or validate expected write bandwidth.

Approximate formula:

```text
total_write_mbps =
    number_of_feeds
    × bitrate_per_feed_mbps
```

Convert to MB/s:

```text
MB/s = Mbps / 8
```

Example:

```text
4 feeds × 150 Mbps = 600 Mbps = 75 MB/s
```




## 7.2 Recommended hardware targets

Define explicit targets before optimization.

Minimum target:

```text
2 feeds
1080p30
in-session replay back to recording start while recording, with 10/30/60/120-second instant replay shortcuts
single NVMe SSD preferred
```

Primary target:

```text
4 feeds
1080p30 or 1080p60
in-session replay back to recording start while recording, with 10/30/60/120-second instant replay shortcuts
long game recording
NVMe SSD strongly recommended
GPU encode/decode preferred
```

Stretch target:

```text
6–8 feeds
1080p60
requires careful hardware validation
likely requires hardware encoders and high sustained disk throughput
```

## 7.3 Memory budget

The system must not store full replay video in RAM.

RAM should be used for:

- indexes
- small queues
- UI state
- metrics
- limited preview buffers

RAM should not be used for:

- full 60-120 second multi-feed replay coverage decoded into memory

## 7.4 Hardware acceleration

The system should support configurable hardware acceleration.

Potential modes:

```toml
[media]
hardware_acceleration = "auto" # auto | none | nvidia | intel | amd
```

Guidelines:

- Hardware acceleration should be optional.
- Software fallback must exist.
- Do not require a specific GPU for development mode.
- Log selected encoder/decoder at startup.

---

# 8. Timebase, Clocking, and Synchronization

## 8.1 SessionClock

The session clock is required.

It must be based on monotonic time, not wall-clock time.

Responsibilities:

- Define `session_start_monotonic_ns`.
- Convert current monotonic time to session time.
- Map feed PTS to session time.
- Provide a stable timeline for replay and clip marking.

## 8.2 Wall-clock time

Wall-clock time may be stored for:

- human-readable logs
- session names
- metadata display
- audit/history

Wall-clock time must not be used for media synchronization.

Reason:

- Wall-clock can jump due to NTP/timezone/manual adjustment.
- Monotonic time does not jump.

## 8.3 Feed PTS mapping

Each feed must maintain:

```text
feed_id
first_pts_ns
first_session_time_ns
pts_to_session_offset_ns
drift_estimate_ns
last_seen_pts_ns
last_seen_session_time_ns
```

## 8.4 Feed startup offsets

Feeds may start at different times.

Required behavior:

- Feed A starting earlier than Feed B must not break sync.
- Replay at a time before Feed B exists should show Feed B as unavailable/blank.
- Feed joins/leaves should be represented in the timeline.

## 8.5 Drift handling

Different feeds may drift.

Initial production implementation may assume stable local feeds, but the model must allow future drift correction.

Minimum:

- Track observed offset.
- Log drift estimates.
- Do not hard-code identical feed clocks.

Future:

- Periodic drift correction.
- Audio/video clock correlation.
- External timecode support.

## 8.6 Multi-feed replay sync

When replaying a timestamp range:

```text
start_session_time_ns → end_session_time_ns
```

The system must:

1. Query each feed timeline for matching segments.
2. Seek each feed to the requested session time.
3. Handle missing segments independently (see §15.5 — never blank a tile).
4. Start playback in sync as closely as possible.
5. Keep feeds aligned during slow motion/pause/resume.
6. Surface degraded status if one feed cannot participate.

### 8.6.1 Per-feed frame clamping rule (catch-up sync)

The replay clock advances in **session time**, not per-feed time. Every operator-window tile renders something on every tick — there is no "blank tile" state during replay. For each feed, on each tick:

- If the current `playback_session_time_ns` falls inside one of the feed's completed segments → decode and render the frame at that offset (normal sync replay).
- If the current `playback_session_time_ns` is **before** the feed's earliest segment → render the feed's **first** recorded frame as a freeze. The tile holds that freeze frame until the playback clock catches up to where the feed actually has coverage, at which point the same query naturally starts returning exact locations and the tile begins moving.
- If the current `playback_session_time_ns` is **after** the feed's latest segment, **or** falls in a mid-session gap (disconnect/reconnect) → render the feed's **last** frame from before that point as a freeze. The tile resumes when the playback clock reaches the next segment.

**Worked example.** Feed A starts at session_time=0; Feed B joins at session_time=5. Operator at session_time=10 clicks Rewind 10s:

- session_time=0..5: Feed A plays normally; Feed B's tile shows its first frame (the t=5 frame) frozen.
- session_time=5: Feed B starts moving from its first frame; both tiles are now in sync.
- session_time=5..10: Both feeds play in sync.

The rule is symmetric across all three "no-coverage-here" cases (feed-not-yet-started, feed-disconnected-mid-session, feed-ended-early). It collapses to a single primitive at the read layer:

```
nearest_frame_location(feed_id, session_time_ns) → (segment, offset_in_segment_ns)
```

returning the exact match when in coverage, the feed's earliest frame when before any coverage, and the latest frame ending at-or-before `session_time_ns` otherwise. Returns `None` only when the feed has zero replayable segments (in which case the tile shows its placeholder until any segment finalizes).

**Rationale.** Operators reason about plays in session time, not per-feed time. A tile that goes blank for 5 seconds during a rewind because that camera joined late is more disorienting than a frozen first-frame held for the same duration. The freeze frame visually communicates "this camera wasn't recording yet" while keeping the multi-feed view legible.

## 8.7 Late join and reconnect

A feed may join after recording has already started, or drop and reconnect mid-game (a camera plugged in late, a flaky NDI source). The model must handle both without special cases at the replay layer.

Rules:

- Each segment carries its own `pts_to_session_offset_ns` (§6.3). A reconnect starts a new segment with a freshly computed offset; the feed clock is allowed to jump.
- A late-joining feed's first segment defines its `first_seen_session_time_ns` on the `Feed` row (§14.3).
- A disconnect ends the currently-writing segment as `dirty` if it cannot be cleanly finalized; recovery rules (§10.6, §11) apply.
- The replay layer never asks "is this feed connected right now?" — it asks "which completed segments cover `[t1, t2]` for this feed?" Gaps are a query result, not a special state.
- Replay over a range where a feed had not yet joined or was disconnected returns no media for that feed in that range; §15.5 (missing media behavior) handles the rest.

---

# 9. Audio Strategy

## 9.1 Initial recommendation

Start with one master audio source.

Do not initially record independent audio from every camera unless required.

Reason:

- Multi-feed audio sync adds complexity.
- Most sports replay workflows need one primary audio bed.
- Operator replay decisions usually prioritize video angles.

## 9.2 Master audio source

Config:

```toml
[audio]
mode = "master" # none | master | per_feed
master_feed_id = "feed_001"
include_audio_in_replay = true
include_audio_in_recording = true
include_audio_in_exports = false
```

## 9.3 Replay audio behavior

Recommended:

- During multi-angle replay, use master audio only.
- If replaying a feed without audio, keep master audio aligned to session time.
- If audio segment is missing, replay video without audio and warn.

## 9.4 Audio sync

Audio must be timestamped and mapped to session time just like video.

Do not sync audio using frame counts.

## 9.5 Future per-feed audio

If per-feed audio is added:

- Store audio stream metadata per feed.
- Allow selecting export audio source.
- Prevent multiple unsynchronized audio streams from playing simultaneously unless explicitly mixed.

---

# 10. Application State Machine

## 10.1 Required state model

The application should expose explicit state rather than scattered booleans.

Recommended states:

```text
STARTING
IDLE
PREVIEWING
RECORDING
REPLAYING
PAUSED
SLOW_MOTION

DEGRADED
ERROR
SHUTTING_DOWN
```

Replay is not an independently armed application mode. Replay is a playback mode that is permitted only while `recording_state == RECORDING`.

## 10.2 Feed state model

Each feed should have explicit state:

```text
DISABLED
CONNECTING
LIVE
DEGRADED
DISCONNECTED
RECONNECTING
FAILED
```

## 10.3 Recording state model

Recording states:

```text
NOT_RECORDING
STARTING_RECORDING
RECORDING
STOPPING_RECORDING
FINALIZING
RECORDING_ERROR
```

## 10.4 Replay state model

Replay states:

```text
REPLAY_UNAVAILABLE_NOT_RECORDING
LIVE_WHILE_RECORDING
SEEKING
REPLAYING
PAUSED
SLOW_MOTION
JUMPING_TO_LIVE
REPLAY_DEGRADED
```

Rules:

- `REPLAY_UNAVAILABLE_NOT_RECORDING` is the replay state whenever recording is not active.
- Replay requests must be rejected when `recording_state != RECORDING`.
- Replay availability is based on completed segments only, so the UI should expose the latest replayable session time.

> **Storage readiness vs operator state.** Earlier drafts of this doc listed `REPLAY_AVAILABLE` as a separate replay state. Phase 2.C folded it into `LIVE_WHILE_RECORDING` because it described **storage** readiness (does the segment store contain at least one completed segment?), not the operator's view. Storage readiness now lives on `RecordingSegmentReplayStore.is_replay_available` and is surfaced as `UiState.replay_available` for the status bar; the operator is in `LIVE_WHILE_RECORDING` regardless of whether a finalized segment exists yet, and the UI shows "Replay not yet available — first segment finalizing" until one does.

## 10.5 State transition rules

Examples:

```text
PREVIEWING → RECORDING
RECORDING → REPLAYING
REPLAYING → JUMPING_TO_LIVE → RECORDING
RECORDING → STOPPING_RECORDING → FINALIZING → PREVIEWING
PREVIEWING → REPLAY_UNAVAILABLE_NOT_RECORDING
RECORDING → DEGRADED
DEGRADED → RECORDING
```

Rules:

- Recording must continue during operator replay unless explicitly stopped.
- Replay must be unavailable whenever recording is not active.
- Feed failure must not crash entire session.
- Post-session processing must not run while the recording application is active.

## 10.6 Session state model

The session has its own state machine, separate from the per-recording state machine in §10.3. The session state is what the post-session processor and the recovery flow on next launch read.

A session is one-per-app-run. The `session_NNN` directory is allocated at launch (`FileManager.get_next_session_id`) and holds **every** game the operator records during that run — multiple Start/Stop cycles all live in the same session. This is intentional: the per-game subdir layout (`recording/<game_NNN>/`, see §6.2.1) makes per-game export possible without splitting the SQLite metadata across many small DBs.

```text
CREATED         # session folder + manifest exist, no recording yet
RECORDING       # at least one feed actively recording
STOPPED         # operator pressed Stop game recording; another game may follow within the same session
FINALIZED       # session manager has closed the session (typically at app shutdown)
DIRTY           # crashed or hard-killed mid-session; not yet recovered or discarded
ARCHIVED        # post-session processor has produced deliverables
```

Transitions:

```text
CREATED   → RECORDING           (operator starts a game recording)
RECORDING → STOPPED             (operator stops the current game)
STOPPED   → RECORDING           (operator starts another game in the same session — fresh game_NNN/ folder)
STOPPED   → FINALIZED           (SessionManager.close() runs — typically at app shutdown)
RECORDING → FINALIZED           (SessionManager.close() runs while a game is still active — graceful-shutdown path)
RECORDING/STOPPED → DIRTY       (crash or hard-kill; detected on next launch)
DIRTY     → FINALIZED           (operator chose Resume → finalize completes)
DIRTY     → CREATED             (operator chose Discard, leaving an empty shell session)
FINALIZED → ARCHIVED            (post-session processor success)
```

`STOPPED → FINALIZED` is **not** a per-Stop-press automatic transition — `STOPPED` is the legitimate resting state between games within one session. The transition fires only when the session itself closes (today, only via `SessionManager.close()` at app shutdown). A future explicit "End Session" UI button would drive it via the same code path; until that button exists, sessions finalize at process exit.

Rules:

- The post-session processor refuses any session whose state is not `FINALIZED` or `ARCHIVED`.
- The operator UI must surface session state plainly. "Game ready for export" maps to `FINALIZED`/`ARCHIVED`; "Game in progress" maps to `RECORDING`/`STOPPED`. The volunteer operator must be able to tell at a glance whether it is safe to walk away.
- The transition `RECORDING/STOPPED → DIRTY` is detected by the absence of a `finalized_at` timestamp on the session manifest at next launch — there is no separate heartbeat needed.
- Cleanup (§6.8) refuses any session in `RECORDING`, `STOPPED`, or `DIRTY`.

---

# 11. Failure Handling and Degraded Operation

## 11.1 Required failure scenarios

The system must handle:

- Camera disconnect.
- Camera reconnect.
- NDI source unavailable.
- Disk write slowdown.
- Disk full.
- Encoder failure.
- Segment finalization failure.
- Corrupt segment on startup.
- UI window close/reopen.
- Application crash/restart.
- One feed missing during replay.
- Replay seek into missing time range.

## 11.2 Failure behavior table

| Failure | Required behavior |
|---|---|
| One feed disconnects | Mark feed degraded/disconnected; other feeds continue |
| Disk slow | Warn operator; recording branch degraded; preview remains live |
| Disk full | Stop nonessential writes; warn loudly; preserve metadata |
| Replay segment missing | Skip/blank that feed for that range |
| Corrupt segment | Quarantine; do not crash session |
| Encoder failure | Mark feed degraded; attempt restart if safe |
| UI error | Do not stop media pipelines unless app is shutting down |
| Post-session processing failure | Mark export artifact failed; do not modify source recording segments |

## 11.3 Operator-visible warnings

The operator UI must surface:

- feed disconnected
- recording degraded
- replay unavailable
- disk nearly full
- disk too slow
- dropped frames high
- encoder failure
- session not safely recording

Do not bury these only in logs.

## 11.4 Session-level recovery flow

On application startup, if any session is in state `DIRTY` (see §10.6), the operator UI **must** present a recovery prompt as the only initial screen and block all media UI until the operator chooses.

Recovery prompt options:

- **Resume** (default highlighted, **most recent dirty session only**): scan `recording/<game_NNN>/<feed_id>/` for incomplete segments, mark partial files `quarantined`, rebuild the in-memory index from surviving completed/dirty segments, transition the session back into `RECORDING` via `DIRTY → CREATED → RECORDING`. The Phase 7.D continuation logic then reuses the crashed `game_NNN/` folder (rather than allocating `game_(N+1)/`) and rebases the new `SessionClock` past the last pre-crash segment so post-resume `start_session_time_ns` values do not collide with pre-crash ones. Replay coverage during the resumed session includes the surviving pre-crash segments; the gap caused by the crash shows up as missing media (§15.5).
- **End and finalize**: do not resume recording; close the session into `FINALIZED` so the post-session processor can run against whatever was successfully captured.
- **Discard**: transition the session to `CREATED` (empty shell), leaving its source segments on disk for retention rules (§6.8) to handle later.

Rules:

- Auto-resume without operator confirmation is forbidden — the cause of the crash (disk full, broken camera, OS update) often persists, and a silent retry loop is worse than a visible prompt.
- **Resume is offered only on the most recent dirty session.** Older dirty sessions get only End-and-finalize / Discard. Resuming two crashes at once isn't meaningful — only one game folder can be the "currently in progress" target for the next Start press, the per-game replay scope can only filter to one crashed game, and the `SessionClock.rebase` anchor is a single point in time. The dialog iterates dirty sessions in directory-sorted order; only `dirty[-1]` is offered the Resume button. Once Resume is accepted (or all sessions have been resolved), the dialog dismisses.
- Partial segments are never repaired in-place. Quarantine and move on. Repair tooling, if any, is offline-only.
- The recovery prompt does not violate the rule "replay unavailable when not RECORDING" (§10.4); the prompt blocks the media UI entirely until a new session state is chosen.

---

# 12. Metrics, Logging, and Observability

## 12.1 Per-feed metrics — target surface

This is the long-term target metric set. The diagnostics widget consumes a subset (the snapshot fields below), supplemented by cross-feed values on `UiState` and per-feed sub-state machines. The full target list is preserved here so future readers can see the eventual surface, not the current one.

### Currently shipped (`FeedMetricsSnapshot`, `app/core/telemetry.py`)

```text
feed_id
display_name
source_fps
preview_fps
recording_fps
dropped_per_sec
python_frames_per_sec
pipeline_mode             -- "python_push" | "native"
queue_depth_preview
queue_depth_recording
queue_max_preview
queue_max_recording
```

The diagnostics widget renders these directly. `feed_state` is read from each `FeedState` machine alongside the snapshot rather than duplicated into it; replay coverage is rendered cross-feed (`UiState.latest_replayable_session_time_ns` / `replay_available`) because the operator's mental model is "is there replay to scrub?" not "which feed has the most segments."

### Target additions (deferred — no current consumer)

```text
program_fps
dropped_buffers_preview
dropped_buffers_program
encode_latency_ms
segment_write_latency_ms
last_completed_segment_end_time
latest_replayable_session_time_ns      -- per-feed (today: cross-feed only)
recording_segments_available_for_replay
feed_state                             -- in-snapshot (today: parallel lookup)
```

Per-bucket status:

- **`program_fps` / split `dropped_buffers_preview` vs `dropped_buffers_program`** — the operator and program windows render through `MultiFeedOutputRenderer` instances that share the upstream tee. Today the snapshot collapses both into one `preview_fps` / `dropped_per_sec`. Splitting requires per-window-sink instrumentation; deferred until a use case appears (e.g. one window stutters while the other doesn't).
- **`encode_latency_ms` / `segment_write_latency_ms`** — the underlying data is already collected by `LatencySampler` (Phase 1.D), just keyed globally (`segment_write_video`, etc.) rather than per-feed. Joining requires re-keying as `(feed_id, name)`; deferred until per-feed latency divergence becomes a real diagnostic question.
- **`last_completed_segment_end_time` / per-feed `latest_replayable_session_time_ns` / `recording_segments_available_for_replay`** — derivable from `SegmentIndex` on demand without storing in the snapshot. The diagnostics widget reads cross-feed values from `UiState` because they match the operator's single-game-at-a-time mental model. Per-feed values would be additive when a multi-feed-aware diagnostics view is built.
- **`feed_state` in-snapshot** — minor inconvenience only; the parallel lookup works. Adding it would deduplicate one read.

None of these are blocked by missing infrastructure — each is a small additive wiring slice that lands when a consumer needs it.

## 12.2 System metrics — target surface

### Currently shipped

```text
disk_write_mb_s              -- DiskSampler (Phase 1.C), 5s cadence
disk_free_gb                 -- DiskSampler
active_feeds                 -- derivable from FeedRegistry / TelemetryHub
recording_state              -- RecordingState machine, observable
replay_state                 -- ReplayState machine, observable (per operator controller)
```

### Target additions (deferred — Phase 11)

```text
cpu_percent
memory_mb
gpu_encoder_usage_if_available
```

These three host-resource metrics have no producer today. Adding them requires:

- `cpu_percent` / `memory_mb`: a `psutil` dependency (new — not currently in the project) and a sampler in the telemetry hub.
- `gpu_encoder_usage_if_available`: vendor-specific querying (NVAPI for NVIDIA, Intel PresentMon, AMD AGS), with graceful fallback when the GPU isn't a recognized vendor or the runtime library isn't installed.

Phase 11 (Hardware Acceleration and Performance Tuning) is the natural home: that phase already measures CPU headroom against the chosen hwaccel encoder path, and the GPU encoder utilization is meaningful only once a specific encoder (NVENC / QuickSync / AMF) has been selected. Until then there is no decision the metric would inform.

## 12.3 Replay metrics — target surface

### Currently shipped

```text
replay_seek                  -- LatencySampler (Phase 1.D); count / avg / p95 / max
                                over the trailing window for operator-initiated lookups
                                (rewind_10_seconds resolves through the replay store).
```

### Target additions (deferred — implementation lands when a diagnostic need appears)

The metrics below split cleanly into two groups: **derivable** ones that are already computed for runtime state and just not logged, and **new-instrumentation** ones that would require fresh probes in the playback pipeline.

```text
# Derivable from existing state (cheap to expose)
replay_request_time                   -- known at the call site (transport methods)
requested_start_session_time          -- known at the call site
requested_end_session_time            -- known at the call site
latest_replayable_session_time_ns     -- already on UiState (cross-feed)
available_replay_duration_seconds     -- already on UiState (live_lag_behind_replayable_seconds derives this)
feeds_available                       -- SegmentIndex.feeds_with_coverage_at(target_session_time_ns)
feeds_missing                         -- complement of feeds_available
completed_segments_selected           -- per-feed nearest_frame_location() results
slow_motion_factor                    -- PlaybackController._playback_rate
rejected_not_recording_count          -- counter on PlaybackController; gated by ReplayState's
                                          REPLAY_UNAVAILABLE_NOT_RECORDING transition

# Needs new instrumentation
replay_index_lag_ms                   -- distance between latest finalized segment row and
                                          live wall-clock; would diagnose a wedged splitmuxsink
                                          beyond the existing live_lag_behind_replayable_seconds
replay_read_seek_latency_ms           -- per-tick SegmentDecoder seek (cv2.VideoCapture.set);
                                          the existing replay_seek covers store lookup, not decode
time_to_first_frame_ms                -- wall-clock from operator transport press to first
                                          rendered frame; currently un-instrumented
```

None of these are blocked by missing infrastructure — each is an additive logging or counter slice. The reason they aren't shipped is that replay performance in practice has not produced a diagnostic question worth answering. When one appears (e.g. an operator reports a perceptible "click Rewind, see frame" delay), the relevant metrics from this list become a small, focused slice rather than a speculative observability buildout.

## 12.4 Logging requirements

Logs should include:

- pipeline state transitions
- GStreamer bus errors/warnings
- segment creation/finalization
- recording start/stop
- replay requests
- clip markers
- post-session processor start/completion/failure
- export artifact creation
- failure recovery actions

## 12.5 Metrics display

At minimum, provide a debug/diagnostics panel or log output showing:

- per-feed FPS
- dropped frames
- disk throughput
- queue pressure
- current codec/container
- active segment count
- replay coverage from recording start through latest replayable finalized segment

---

# 13. Configuration Schema

The TOML schema below mirrors what `AppSettings.load`
(`app/config/settings.py`) actually reads today. Sections that the doc
once aspired to but the loader does not consume are listed under §13.2
"Future config sections" with the phase that would land them.

## 13.1 Shipped TOML schema

```toml
[app]
# Window titles and identity strings (purely cosmetic).
app_name              = "Sports Replay POC"
window_title          = "Sports Replay Control"
operator_window_title = "Sports Replay Operator"
program_window_title  = "Sports Replay Program"

# All session media + metadata lives under here.
base_data_dir = "C:\\SportsReplay"

# Phase 3.C — surfaces the python_push transitional banner loudly when
# any feed is still on the Python push path. "development" hides it.
app_mode = "development"   # development | production

# Phase 3.A.3 escape hatch. When true, NATIVE-mode sources still use
# the legacy appsink → QImage preview path (avoids the d3d11videosink
# binding bug on misbehaving hardware). Cost: preview stays Python-bound
# (~720p ceiling). Default off.
force_python_push_preview = false

# Live-preview cap. Raising past 1280×720@30 requires a working native
# preview path on every enabled feed (see §3.A.3 / §4); the synthetic
# dev source stays python_push so it caps the rig.
target_frame_width  = 1280
target_frame_height = 720
target_fps          = 30.0

# Audio (per-feed embedded — no master-source mixer). `enable_embedded_audio`
# is the upper bound; `[recording] audio_enabled` is the recording-side
# override; Phase 9.C decides per-feed at runtime based on whether the
# source actually produces audio buffers.
enable_embedded_audio       = true
live_audio_monitor_enabled  = true
audio_sample_rate           = 48000
audio_channels              = 2
audio_bitrate               = 128000

# Operator UI button height (touch-screen ergonomics).
touch_button_height = 72

[recording]
# Phase 4 splitmuxsink-driven segmented recording.
enabled                  = true
segment_duration_seconds = 4.0
codec                    = "mjpeg"   # only "mjpeg" wired today; see §5.2
container                = "mkv"     # only "mkv" wired today; see §5.3
# Phase 4.F + Phase 9.C. Upper-bound override: false forces video-only
# regardless of source capability. When true, the runtime detects audio
# presence per feed and wires the audio_record branch only if buffers flow.
audio_enabled            = true
# Phase 7.A. Aggregate disk-write budget in MB/s. Validator at startup
# compares estimated throughput (feed_count × frame size × fps × codec
# coefficient) against this number. 200 ≈ conservative SATA SSD; raise
# for NVMe, lower for spinning disks.
disk_budget_mb_s         = 200.0

# Multi-feed configuration (preferred). One [[feeds]] row per camera.
# Production deployments use kind = "ndi". The synthetic source is the
# dev-time fallback for camera-less environments.
[[feeds]]
feed_id      = "ndi_main"
display_name = "Main Camera"
kind         = "ndi"            # ndi | synthetic
ndi_name     = "HOSTNAME (Camera 1)"
enabled      = true

[[feeds]]
feed_id      = "ndi_angle"
display_name = "Angle 2"
kind         = "ndi"
ndi_name     = "HOSTNAME (Camera 2)"
enabled      = true

# Dev-only synthetic feed (no NDI hardware needed). Comment out for prod.
# [[feeds]]
# feed_id      = "feed_dev"
# display_name = "Synthetic Test Pattern"
# kind         = "synthetic"
# enabled      = true

# Legacy single-feed mode. Used only when no [[feeds]] rows are present.
# Modern configs should use [[feeds]] above.
# [source]
# feed_id      = "feed_main"
# display_name = "Test Source"
# kind         = "synthetic"
# ndi_name     = ""
```

### Loader behavior

- The loader is forgiving on missing keys: every field above has an
  in-code default, so a `[recording]` block with only `codec = "mjpeg"`
  works.
- `kind` accepts only `"ndi"` or `"synthetic"`. Any other value is a hard
  config error with a migration hint (the legacy `kind = "auto"`
  specifically calls out Phase 2.5's USB-camera removal).
- `app_mode` accepts only `"development"` or `"production"`; unknown
  values raise at load time.
- When `[[feeds]]` rows are present, the legacy `[source]` block is
  ignored. At least one feed must have `enabled = true`.

## 13.2 Future config sections (not read today)

These were in earlier drafts of the doc but the loader does not
consume them. Each is queued behind a phase whose work would land the
reader at the same time as the behavior the section gates.

| Section | Status | Lands with |
|---|---|---|
| `[app] mode` | doc had `mode`, code has `app_mode` (renamed for clarity) | already shipped |
| `[session] default_name_prefix / auto_create_session` | unimplemented; sessions are always auto-created and named `session_NNN` | no plan to add — naming convention is fixed |
| `[media] pipeline_mode` | unimplemented; `SourceInterface.pipeline_mode` is hard-coded per source kind | no plan to add — the per-source declaration is the right surface |
| `[media] hardware_acceleration` | unimplemented | Phase 11 |
| `[media] default_width / default_height / default_fps` | code reads these from `[app] target_frame_*` instead | doc-only rename |
| `[replay]` block | unimplemented; `requires_recording = true` is enforced unconditionally, `quick_replay_seconds` is contradicted by the code's single Rewind 10s button | no plan to add — the constants are correct |
| `[retention]` block | unimplemented; the live recording app never deletes anything (§6.8) | future cleanup CLI / post-session processor flag |
| `[post_processing]` block | unimplemented; the post-session processor uses hard-coded ffmpeg defaults (`libx264` / `aac` via subprocess) | future Phase-8 follow-up if a deployment needs to override the encoder |
| `[audio] mode = "master"` and friends | obsoleted by Phase 9's per-feed embedded-audio model. The doc's old master-audio TOML described a workflow that was never built. | n/a — drop from §13 in a future edit |
| `[preview] max_latency_ms / drop_when_late` | unimplemented; preview queue policy is a hard-coded `leaky=2 / 200ms / 4 buffers` (Phase 3.B) | future tuning slice if hardware testing demands per-deployment tuning |
| `[monitoring]` block | unimplemented; metrics, diagnostics overlay, and gst bus logging are unconditionally on | no plan to add — there's no use case for turning observability off |
| `[[feeds]] id / name / role` | doc used `id`/`name`/`role`; code uses `feed_id` / `display_name` and ignores `role` | doc-only rename (see §13.1 above) |

## 13.3 Config rules (still apply)

- Every feed must have a stable `feed_id`.
- Feed IDs must not depend on device order.
- Defaults must be explicit.
- `kind` accepts only `"ndi"` (production) or `"synthetic"` (dev). Any other value is a hard config error.
- Production mode must not silently fall back to synthetic test source unless configured.

---

# 14. Database / Metadata Model

## 14.1 Required entities

Minimum durable entities:

```text
Session
Segment
Play
ExportArtifact
```

Notes on entities that are **not** durable tables:

- `FeedTimeline` is a runtime-computed view of `(feed_id, [available_intervals])` derived from the `Segment` table on demand (see §8.7). Replay queries operate against `Segment` directly; there is no separate timeline table to keep in sync.
- `Feed` per-session state (`first_seen_session_time_ns`, `last_seen_session_time_ns`, `state`) is not persisted. The post-session processor and replay queries derive feed presence from segment rows; in-process feed state lives in the `FeedState` machine (§10.2) and the diagnostics widget. Add a `Feed` table only when a consumer needs cross-run per-feed forensics.
- `Recording` (the long-form game) is identified by `(session_id, game_subdir)` and is reconstructable from the segment rows that share that pair; no separate row is needed.
- `HealthEvent` is persisted as append-only JSONL under `<session>/logs/health_events.jsonl` (§14.6 below) rather than SQLite. Move it to SQLite when a consumer needs `resolved_at` semantics or joins against session/feed rows.
- `MetricSample` is not persisted. Live values flow through `TelemetryHub` and the diagnostics widget; the periodic log lines are the historical trail. Persist samples only if Phase 10/11 introduces a post-game performance review tool.

## 14.2 Session

The `sessions` SQLite row holds the durable identity:

```text
session_id
source_name
started_at
```

Operational state (`state`, `created_at`, `finalized_at`) lives in `<session>/session.json`, the manifest the §11.4 recovery flow reads to detect a `DIRTY` session — the absence of `finalized_at` is the dirty marker, so a single source-of-truth file avoids the manifest/SQLite-row consistency problem.

## 14.3 Segment

Fields listed in section 6.3.

## 14.4 Play

Schema in section 6.7. One row per play; spans are mutually exclusive within a game; `end_session_time_ns` is NULL only on the currently-open play.

## 14.5 ExportArtifact

The post-session processor (Phase 8) emits one row per attempted long-form encode. `(session_id, kind, game_subdir, feed_id)` is the natural key — re-runs use it for idempotent skip-on-success.

```text
artifact_id
session_id
kind                 -- 'long_form' (only kind today)
game_subdir          -- nullable
feed_id              -- nullable; populated for per-feed long-form
output_path
status               -- 'success' | 'failed'
error_message        -- nullable
size_bytes           -- nullable, populated on success
duration_ns          -- nullable, populated on success
started_at
finalized_at         -- nullable
```

Per-play sub-clip exports are intentionally absent (§6.7): the long-form MP4 plus the `plays.json` sidecar covers the consumer use case, and downstream tooling slices when needed.

## 14.6 HealthEvent (JSONL, not SQLite)

One JSON object per line under `<session>/logs/health_events.jsonl`:

```text
id                   -- monotonic counter, per-process
session_id
feed_id              -- nullable
severity             -- 'info' | 'warning' | 'error'
category
message
created_at           -- UTC ISO-8601
metadata             -- arbitrary JSON object
```

`resolved_at` is reserved for the future SQLite migration; the append-only JSONL log has no resolve step today.

---

# 15. Replay and Playback Semantics

## 15.1 Live mode

Live mode displays the most recent available frames.

For operator:

- live mode can transition to replay only while recording is active and completed recording segments are available.

For program:

- live mode only.

## 15.2 Replay request

A replay request is valid only when `recording_state == RECORDING`. Replay may target any completed recording segment range in the current active recording, from recording start through the latest replayable finalized segment. The configured instant-replay shortcuts are UI conveniences, not a storage or architecture limit.

> Phase 7.C lock-in: `tests/test_replay_safety_invariants.py` asserts both halves of this rule — `state="writing"` segments are excluded from every replay query (`resolve`, `resolve_session_time`, `nearest_frame_location`, `latest_replayable_pts`, `latest_replayable_session_time`), and the operator transport methods (`rewind_10_seconds`, `pause_playback`, `set_playback_rate`, `jump_to_live`, `_on_replay_timer_tick`, `_render_at_session_time_ns`) take no filesystem-mutation or DB-write actions. A regression that touched `Path.unlink` / `os.remove` / `MetadataDb.update_segment_state` from a transport path would fail those tests loudly.

A replay request is:

```text
start_session_time_ns
end_session_time_ns
feed_ids
playback_rate
```

## 15.3 Instant replay request

An instant replay shortcut request for the last 60 seconds is:

```text
end = current_session_time
start = end - 60 seconds
```

The system must then query completed recording segments per feed. If recording is not active, the request must be rejected and the UI must show replay unavailable. Instant replay shortcuts are convenience actions, not the maximum replay range.

## 15.4 Slow motion

Supported rates:

```text
1.0x
0.5x
0.25x
```

Optional future:

```text
reverse playback
frame step
```

Slow motion must be driven by playback timing, not by duplicating files.

## 15.5 Missing media behavior

If a feed lacks completed recording media for the requested session-time during replay:

- render the feed's **nearest available frame** as a freeze (its first frame for "before any coverage", its last frame before the gap for "in a gap" or "after coverage ends" — see §8.6.1 for the full clamping rule and worked example)
- continue other feeds at their actual session time
- surface degraded replay status (the operator UI should indicate which tiles are frozen vs sync-playing)
- do not crash
- do **not** blank the tile while replay is active — operators reason in session time and a frozen first/last frame is more legible than an empty tile

A tile is only blank when the feed has **zero** replayable segments at all (no completed segments yet), or when the source has never connected. Both surface as the standard "Awaiting video" placeholder, not as a per-rewind decision.

## 15.6 Jump to live

Jump to live must:

- stop replay playback
- return operator view to live source
- keep recording unaffected

## 15.7 Replay seek granularity

Replay start and end points are **frame-accurate inside any completed segment**, not snapped to segment boundaries.

Reason:

- The active recording codecs mandated in §5.2 (ProRes, DNxHR, MJPEG) are intra-frame. Every frame is effectively a keyframe, so the decoder can begin output at any frame inside a completed segment.
- The only segment-boundary constraint is **finalization**: the currently-writing segment is not safely readable until it is closed. Replay coverage is therefore `[recording_start, end_of_latest_finalized_segment]`, not `[recording_start, now]`. The "writing tail is excluded" half of this is locked in by `tests/test_replay_safety_invariants.py::WritingTailExclusionTests`.
- With the default `segment_duration_seconds = 4`, the operator view "live edge" of replay lags wall-clock live by 0–4 seconds. The UI must surface this as `latest_replayable_session_time_ns` (§12.1) and not pretend replay extends to the present moment.

Implications:

- Instant-replay shortcuts (e.g. "last 60 seconds") compute `end = latest_replayable_session_time_ns`, not `end = now`.
- Jump-to-live (§15.6) returns the operator view to the live source pipeline, not to the latest replayable timestamp — these two times are not the same.
- If an inter-frame codec is ever introduced as a fallback (§5.6), seek granularity collapses to the keyframe interval and segment boundaries must be forced keyframes. The replay query API does not change; only the achievable seek precision does.

---

# 16. Testing and Validation Strategy

## 16.1 Synthetic feed generator

A production refactor must include a deterministic synthetic feed mode.

Synthetic video should include visible overlays:

```text
feed_id
frame counter
PTS/session timestamp
wall-clock timestamp
moving test pattern
```

Reason:

- Allows visual verification of sync.
- Allows replay correctness tests.
- Allows testing without cameras.

## 16.2 Required test scenarios

Test:

1. One feed live.
2. Two feeds live.
3. Four feeds live.
4. Start/stop recording.
5. Replay last 60 seconds.
6. Replay while recording continues.
7. Program output remains live while operator replays.
8. **Feed disconnect during recording.** *(Deferred — Phase 10.)*
9. **Feed reconnect during recording.** *(Deferred — Phase 10.)*
10. **Disk slowdown simulation.** *(Deferred — Phase 10.)*
11. **Disk full simulation.** *(Deferred — Phase 10.)*
12. **App crash during segment write.** *(Deferred — Phase 10. The recovery side, scenario #13, is covered by `tests/test_session_recovery.py` against synthesized dirty fixtures, but no test injects a crash mid-write.)*
13. Restart and recover session.
14. Corrupt segment quarantine.
15. Missing feed during replay.
16. Slow motion playback.
17. Jump to live.
18. Replay unavailable when recording is stopped.
19. Replay uses completed segments only and reports latest replayable time.
20. Replay can request completed media back to the beginning of the current recording.
21. Instant replay shortcuts do not limit the maximum replayable range.
22. Post-session processor refuses to run against an active session.
23. Post-session processor creates long-form MP4 files after shutdown.
24. Post-session processor writes a `plays.json` sidecar per game whose play boundaries match the in-DB `plays` rows.
25. "Replay Play" seeks playback to the currently-open play's `start_session_time_ns` and resumes at 1.0x.
26. The play counter resets to 1 for each new game; "Next Play" advances within a game without skipping numbers; "Stop game recording" closes the currently-open play.

> **Deferred-to-Phase-10 note.** Scenarios #8–#12 cover camera disconnect/reconnect, disk pressure, and crash-during-write. All require either real failure injection (NDI source-side disconnect simulation, disk-pressure tooling) or harness scaffolding (a `_FakeFilesystem` that fails writes after N bytes, a `_FakePipelineManager` that abruptly transitions to `NULL` mid-buffer). They land alongside the Phase 10 hardening work that handles those failure modes in production code; running the tests in advance of the production handling would just lock in the current "no-op on failure" behavior.

## 16.3 Performance acceptance tests

Define hardware-specific acceptance tests.

Example primary target:

```text
4 feeds
1080p30
in-session replay back to recording start with recent instant-replay shortcuts
60-minute recording
operator replay during recording
preview latency < 200 ms
replay time-to-first-frame < 500 ms
no unbounded memory growth
```

## 16.4 Regression rule

Before changing media pipeline behavior:

- run synthetic feed test
- verify segment writing
- verify replay
- verify recording still works
- verify replay is unavailable when not recording
- verify post-session processor does not run against active sessions

---

# 17. AI Coding Assistant Implementation Rules

These rules are mandatory when using Cursor, Claude Code, or other AI coding tools.

## 17.1 Do not rewrite everything

Never ask the AI to:

```text
Rewrite the entire app to match this architecture.
```

Instead use vertical slices.

## 17.2 Required AI workflow

For each phase:

1. Ask AI to inspect existing code.
2. Ask AI to identify impacted files.
3. Ask AI to propose a plan.
4. Review the plan.
5. Implement only one bounded change.
6. Run tests/manual checklist.
7. Commit.
8. Continue.

## 17.3 Guardrails for AI

Every implementation prompt should include:

```text
Do not change unrelated behavior.
Do not rewrite the UI.
Do not introduce frame-based replay.
Do not push production video frames through Python.
Do not use MP4 for active recording or replay source media.
Do not remove existing working fallback modes unless explicitly asked.
Preserve current behavior unless the phase requires changing it.
```

## 17.4 Recommended branch strategy

```text
main
  stable current app

poc-working
  tagged known-good proof of concept

refactor/phase-1-instrumentation
refactor/phase-2-gstreamer-native-feed
refactor/phase-3-gstreamer-native-feed
refactor/phase-4-segmented-recording-store
refactor/phase-5-session-timeline
...
```

Commit after each working vertical slice.

---

# 18. Phased Implementation Plan

## Phase 0 – Freeze and Document Existing Behavior

Goal:

- Preserve the working proof-of-concept before refactoring.

Tasks:

- Create branch/tag: `poc-working`.
- Document how to run the current app.
- Document current config.
- Record current limitations.
- Add a manual smoke test checklist.

Exit criteria:

- App can be launched from clean checkout.
- Current behavior is documented.
- Known-good branch exists.

---

## Phase 1 – Observability and Metrics

**Status: ✅ Complete** (slices 1.A through 1.E).

Goal:

- Add measurement before major refactors.

Tasks:

- Add per-feed FPS metrics. *(1.A)*
- Add dropped-frame counters where available. *(deferred — depends on `qos` bus messages, slated for Phase 2.A)*
- Add GStreamer bus logging. *(1.B)*
- Add disk throughput metric. *(1.C)*
- Add replay seek timing. *(1.D)*
- Add segment write timing. *(1.D)*
- Add visible diagnostics panel or debug log output. *(1.E)*
- Add health events. *(1.E)*

Do not:

- Change media format.
- Change replay storage.
- Change UI behavior except diagnostics.

Exit criteria (all met):

- Operator can see/log feed health. *(diagnostics widget + `logs/health_events.jsonl`)*
- Replay requests produce timing logs. *(`replay_seek` latency sampler)*
- Segment writes are logged. *(`segment_write_video` / `segment_write_audio`)*
- Disk free/throughput is visible. *(5s disk log line + diagnostics widget)*

Slices delivered:

- **1.A — Per-feed FPS counters.** New `app/core/telemetry.py` (`RateCounter`, `FeedMetrics`, `TelemetryHub`). 1Hz log line per feed. Counters tick at the three `appsink` seams in `pipeline_manager.py` (preview, record, replay-write).
- **1.B — Unified GStreamer bus logging.** New `app/media/gst_bus_log.py`. Bus filter expanded from `ERROR | EOS` to `ERROR | WARNING | INFO | EOS`. Every bus log line tagged with `feed_id` and `pipeline_role` (`live` / `replay` / `replay-audio`).
- **1.C — Disk metrics.** `DiskSampler` + `DiskSnapshot`. Sustained-write MB/s estimated from the change in `shutil.disk_usage(...).free` between samples; emitted on a 5s cadence, surfaced in the diagnostics widget.
- **1.D — Write/seek timing.** `LatencySampler`, `LatencyRegistry`, and a `time_block(name)` context manager. Wraps `MuxedMediaWriter.write_frame` / `write_audio_chunk` (`segment_write_*`) and the operator-initiated `rewind_10_seconds` lookup (`replay_seek`). Roll-ups (count / avg / p95 / max) flushed by the hub each tick.
- **1.E — Health events + diagnostics widget.** New `app/core/health_events.py` with append-only JSONL persistence under `<session>/logs/health_events.jsonl`. Hub auto-emits `feed_lost` after 3 consecutive zero-source-fps samples and `disk_low` below 5% free, with paired `feed_recovered` / `disk_recovered` on recovery. New `app/ui/diagnostics_widget.py` mounted on the operator window only.

---

## Phase 2 – Explicit State Machines

**Status: ✅ Complete** (slices 2.A through 2.D).

Goal:

- Replace scattered booleans/control flags with clear state.

Tasks:

- Add app state model. *(2.D)*
- Add feed state model. *(2.A)*
- Add recording state model. *(2.B)*
- Add replay state model. *(2.C)*
- Ensure UI reflects state. *(2.D)*
- Ensure invalid state transitions are rejected/logged. *(2.A — `StateMachine` helper records `invalid_transition` health events; 2.D surfaces lifetime count in diagnostics widget)*

Do not:

- Rewrite pipelines yet.

Exit criteria (all met):

- Start/stop recording transitions are explicit. *(`RecordingState` + `SessionState`; `<session>/session.json` persisted)*
- Replay/live transitions are explicit. *(`ReplayState` per operator `PlaybackController`)*
- Feed disconnect can be represented cleanly. *(`FeedState` driven by bus events and zero-fps streak)*

Slices delivered:

- **2.A — State-machine framework + `FeedState`.** Generic `StateMachine[E]` (`app/core/state_machine.py`) rejecting illegal transitions and recording an `invalid_transition` health event each time. `FeedState` (§10.2) per `FeedRuntime`, driven by live-pipeline `ERROR` (→ `DISCONNECTED`), `qos` dropped-buffer rate (→ `DEGRADED`), and the telemetry hub's zero-fps streak as a fallback for sources that never raise a bus error. The deferred Phase 1 dropped-buffer counter (`FeedMetrics.dropped_per_sec`) is included here. The legacy `app_state.py:AppState` dataclass was renamed to `UiState` so the §10.1 enum could claim the `AppState` name in 2.D.
- **2.B — `RecordingState` + `SessionState`.** Both enums + transition tables. `RecordingState` is owned by `RecordingManager`; `SessionState` is owned by `SessionManager`. `<session>/session.json` is written atomically on every `SessionState` transition; the absence of a `finalized_at` timestamp on next launch is the marker §11.4's recovery flow will later use to detect `DIRTY`. `ApplicationCoordinator.toggle_long_session_recording` drives both machines through their start/stop sequences.
- **2.C — `ReplayState` enforcement (user-visible behavior change).** `ReplayState` enum (§10.4, with `REPLAY_AVAILABLE` folded into `LIVE_WHILE_RECORDING` because it described storage readiness rather than a distinct operator-view state — worth a one-line correction in §10.4). One state machine per operator `PlaybackController`. The transport methods (`pause_playback`, `rewind_10_seconds`, `set_playback_rate`, `jump_to_live`) drive explicit transitions. **The doc's rule from §10.4 / §15.2 is now enforced:** replay actions are rejected when `recording_state != RECORDING`, and a recording-stop mid-replay snaps the operator back to live. The status-bar message *"Replay unavailable: start game recording first."* surfaces the rejection.
- **2.D — Aggregate `AppState` enum + UI surfacing.** `AppState` (§10.1) is a *derived* enum, not a fifth state machine — `compute_app_state(feed_states, recording_state, replay_state, shutting_down)` aggregates from the four authoritative machines. Precedence (highest first): `SHUTTING_DOWN`, `ERROR`, `REPLAYING`/`PAUSED`/`SLOW_MOTION`, `DEGRADED`, `RECORDING`, `PREVIEWING`, `STARTING`, `IDLE`. `DEGRADED` is intentionally below the operator's active replay states so user-action context is not buried under a side indicator; the diagnostics widget surfaces all four sub-states regardless. `StatusBarWidget` got a top "App State" row; `DiagnosticsWidget` shows per-feed `FeedState` next to FPS + drops/sec, the operator's `ReplayState`, and lifetime counts of `invalid_transitions` / `feed_lost` / `disk_low`.

### Doc fix queued from Phase 2 — ✅ resolved

§10.4 has been updated: `REPLAY_AVAILABLE` is removed from the enum and the section now points at `RecordingSegmentReplayStore.is_replay_available` / `UiState.replay_available` for storage readiness.

### Was out of scope for Phase 2 (still open)

- The §11.4 startup recovery prompt UI ("Resume / End and finalize / Discard"). 2.B persists enough state to *detect* `DIRTY` on next launch but does not act on it. Belongs in a later phase or a §11.4-specific slice.
- The `ARCHIVED` transition. That belongs to the Phase 8 post-session processor, not the live app.

---

## Phase 2.5 – Narrow Source Surface to Production Targets

**Status: ✅ Complete.**

Goal:

- Remove the USB-camera / OpenCV / GStreamer-camera ingest paths that were a proof-of-concept convenience and are not architectural targets. Leave NDI as the production source and a synthetic test source as the dev-time fallback. Phase 3's "one feed" decision is unambiguous after this slice.

This is a single slice; no sub-slicing needed. ~150 LOC delta, one commit.

Tasks:

- Delete `app/media/gstreamer_camera_source.py` and any `gst-camera`-specific code path in `pipeline_manager.py`.
- Strip the OpenCV `cv2.VideoCapture` / DSHOW / MSMF logic from `app/media/test_source.py`. Keep the synthetic frame generator (deterministic timestamped test frames).
- Simplify `app/media/source_factory.py` to dispatch on `kind = "ndi"` or `kind = "synthetic"` only. Any other `kind` value (including the legacy `"auto"`) is a hard config error with a clear message — no silent fallback.
- Remove `test_camera_index`, `default_source_kind`, and `camera_index` (per-feed) from `AppSettings`. The remaining feed fields are `feed_id`, `display_name`, `kind`, `ndi_name`, `enabled`.
- Update `app_settings.toml.example` to show only NDI feed rows plus a commented-out synthetic row for dev.
- Update the README to drop webcam-capture guidance and point new contributors at the synthetic source for camera-less dev.
- Update `ARCHITECTURE.md` to note USB ingest is removed.
- Strip USB-specific test cases from `tests/test_test_source.py` and simplify `tests/test_source_factory.py`. Synthetic-source tests stay.

Do not:

- Touch the NDI ingest path (that's Phase 3.A's job).
- Touch state machines, telemetry, replay, or recording.
- Add a deprecation shim that maps `kind = "auto"` to `kind = "synthetic"`. A hard config error is the right break.

Exit criteria:

- Repo contains no references to `cv2.VideoCapture`, `CAP_DSHOW`, `CAP_MSMF`, `ksvideosrc`, `v4l2src`, `gstreamer_camera_source`, or `camera_index`.
- Synthetic source still works for dev (`python main.py` against a config with `kind = "synthetic"`).
- All previously passing tests pass after rewrite. Total test count drops by however many USB-specific cases existed.
- `app_settings.toml` files using `kind = "auto"` produce a clear startup error pointing at the new schema.

Notes:

- This is a **config-breaking change** for any dev environment that has `kind = "auto"` in its working `app_settings.toml`. The fix is one line per row. The example file is updated to make that obvious.
- After 2.5 the synthetic source remains on the `python_push` path; that's intentional. Phase 3 is about the production native path, not about hardening the dev fallback.

---

## Phase 3 – Native GStreamer Data Path for One Feed

**Status: 🟢 Complete.** All slices land:
- 3.A.1, 3.A.2: native NDI ingest + pipeline-mode contract.
- 3.A.3 retry: native preview video sink committed (see slice notes — the original "third window" symptom turned out to be the legacy replay pipeline's d3d11videosink, which Phase 4.D removed; the retry adds leaky per-window queues + a live↔replay surface flip in the widget).
- 3.B / 3.C: queue policies, queue-depth metrics, saturation-driven health rules, transitional pipeline banner, `app_mode` setting.

### Blocker discovered during 3.A.3 testing (drives Phase 4 acceleration)

While reliability-testing the post-3.A.2 baseline at 720p, the app deadlocked after ~12-13 frames on every launch (CPU near 0, threads stable, memory flat — classic deadlock signature, not throughput). Reproducible with both NDI and synthetic sources, so source-independent. Localized via a diagnostic disable of `pipeline_manager.start_replay_buffer`: with the rolling replay buffer disabled, live preview runs cleanly at 1080p; with it enabled, deadlock at ~12-13 frames every time.

**Root cause:** `MuxedMediaWriter` (`app/media/muxed_writer.py`) is invoked from GStreamer streaming-thread signal handlers (`_on_replay_sample`, `_on_record_sample`). Its lazy `_open()` and per-segment rotation perform blocking `pipeline.set_state(...)` calls. Calling `set_state` from a signal handler that's part of the *outer* pipeline's data flow blocks the streaming thread, which blocks the source tee, which deadlocks the entire per-feed graph.

This affects two paths today:

- **Rolling replay buffer** (`_on_replay_sample` → `replay_buffer.append_frame` → `MuxedMediaWriter.write_frame` → `_open` on first call, segment rotation thereafter). Manifests as the 12-13-frame freeze on every launch with replay enabled.
- **Long-form game recording** (`_on_record_sample` → `recorder.write_frame` → same `MuxedMediaWriter`). Manifests as a hard deadlock the moment the operator clicks "Start game recording."

**Why Phase 4 fixes both:** §5.4 / §6.3 / Phase 4's task list specify replacing `MuxedMediaWriter` (Python-driven, lazy-opens its own pipeline, blocking) with native GStreamer muxers (`splitmuxsink` / `mp4mux` / etc.) wired directly into the per-feed tee branches as part of the same pipeline. No more `set_state` calls from signal handlers, no more deadlock. Phase 4 also obsoletes the rolling JPEG replay buffer entirely (replay reads from completed recording segments per §6.6 / §15.2).

**Caveats currently in force in the working tree:**

- A diagnostic early-return at the top of `pipeline_manager.start_replay_buffer` keeps the rolling replay buffer disabled. Reverting this re-introduces the 12-13-frame deadlock; do not revert until Phase 4 lands and the rolling buffer is removed entirely.
- The "Start game recording" button still deadlocks the app on click. The button is left as-is (per operator preference) with the understanding that it must not be exercised until Phase 4 ships native recording. No guard installed.
- Operator-side replay actions (rewind, pause, slow, jump-to-live) require recording to be `RECORDING` per §10.4; with recording broken, these are also exercised at the operator's risk.

**Why 3.B and 3.C are deferred:**

3.B (per-branch queue policies + queue-depth metrics) and 3.C (validation banner + Phase-3 readout) are designed around a working recording branch. With recording broken, neither slice can be meaningfully validated end-to-end. The right ordering is: ship Phase 4 → confirm recording works → resume 3.B / 3.C with that as the substrate.

3.A.3 retry (native preview video sink) is also deferred until after Phase 4. The current symptoms may have been compounded by recording-branch resource contention; revisiting it after Phase 4 reduces concurrent unknowns.

---

Goal:

- Prove the production data path on one feed.

Tasks:

- Choose one feed.
- Move hot-path media handling into GStreamer.
- Add tee branch structure.
- Add explicit queues after tee branches.
- Keep Python as controller only.
- Preserve preview.
- Preserve recording.
- Preserve replay behavior as much as possible.

Do not:

- Convert all feeds at once.
- Remove fallback test source.
- Rewrite the UI.

Exit criteria:

- One feed runs through native GStreamer path.
- Preview works.
- Recording works.
- Replay still works or known temporary limitation is documented.
- Metrics show reduced Python-frame involvement.

### Slices

Phase 3 is the first phase that touches real media flow. The current pipeline pushes Python-side `MediaFrame` (NumPy BGR) into GStreamer via `appsrc`; Phase 3's job is to replace that hot path with a native GStreamer source on the production feed kind (NDI) while preserving the synthetic dev fallback. Phase 2.5 narrowed the source surface to NDI + synthetic, so 3.A's "one feed" target is unambiguous: NDI.

A subtlety surfaced during 3.A.2 implementation: getting frames *out of Python on ingest* is necessary but not sufficient for end-to-end full-resolution operation. The current operator preview also pulls frames *into Python* via `_on_preview_sample` to render via `QImage`. At 1080p@30 that's ~180 MB/s through the GIL, which freezes the Qt event loop. Slice 3.A is therefore split into three sub-slices: ingest contract (3.A.1), native NDI ingest (3.A.2), and **native preview video sink** (3.A.3). All three are needed before the Python display ceiling lifts; the operator-configurable `target_frame_*` defaults in `app/config/settings.py` stay at a Python-tractable size (720p@30) until a successful 3.A.3 implementation lands. 3.A.3's first attempt (d3d11videosink + per-window handle binding) was reverted — see the slice note below.

**3.A.1 — Pipeline-mode contract + diagnostics** *(✅ Complete)*

- Extended `SourceInterface` with a `PipelineMode` declaration (`PYTHON_PUSH` / `NATIVE`).
- `FeedMetricsSnapshot` exposes `pipeline_mode` and `python_frames_per_sec`; the diagnostics widget surfaces both, so the operator can see at a glance which feeds round-trip through Python.
- The synthetic test source declares `PYTHON_PUSH` (it is the dev fallback by design and never goes to production).

**3.A.2 — Native NDI ingest** *(✅ Complete)*

- NDI ingest (`ndi_receiver.py`) is now a native GStreamer element chain: `ndisrc → ndisrcdemux → videoconvert → videoscale → videorate → capsfilter (BGR target_w × target_h @ target_fps)` with a separate `audioconvert` audio src pad.
- `PipelineManager` consumes the chain via `source.build_native_chain(Gst)` + `source.link_native_chain_static()`, adds the elements directly to the parent pipeline (no `Gst.Bin` / ghost-pad encapsulation — that path was tried first and broke buffer flow on Windows + gst-plugins-rs despite working caps events), and links the returned video src pad to the source tee.
- `async=False` is set on every appsink (preview / record / replay / audio_*) and the `wasapisink` so a missing audio stream (e.g. NDI Tools Screen Capture) doesn't keep the pipeline stuck in `PAUSED` waiting for preroll on the dangling audio chain.
- `_feed_appsrc_loop` is skipped for native sources (no Python frame round-trip on ingest).

**Limitation discovered, deferred to 3.A.3:** the **preview** path still pulls every frame into Python via the appsink+`new-sample` signal handler for QImage rendering. At full source resolution and rate (e.g. 2560×1440@60 from a desktop NDI sender) that drowns the GIL and freezes the Qt event loop. Mitigated for now by capping the source-side `videoscale`/`videorate` chain at the operator-configured `target_frame_*` defaults (720p@30); production-grade 1080p@30 needs 3.A.3.

**3.A.3 — Native preview video sink** *(✅ Committed on retry after Phase 4)*

**Original failure mode:** the first attempt (pre-Phase 4) hit a "third Direct3D11 renderer" top-level window that kept appearing despite a working `prepare-window-handle` bus handler, and the preview tee fan-out froze within seconds. The most likely root cause was the legacy replay pipeline's own `d3d11videosink` (a third sink we weren't binding), which Phase 4.D removed entirely along with the rolling-replay buffer. With only operator + program sinks remaining, the binding race shrinks to a tractable set.

**Retry shape (committed):**

- `PipelineManager._add_preview_branch` dispatches based on `source.pipeline_mode`: NATIVE → `_add_native_preview_branch` (the d3d11 path), `python_push` → `_add_python_push_preview_branch` (the legacy appsink → QImage path). Synthetic dev source remains on the python_push path because it has no native GStreamer source to feed d3d11 anyway.
- Per-window queues in `_add_native_preview_branch` are now `leaky=2 (downstream)` + `max-size-time=200ms` + `max-size-buffers=4` so a blocked window (minimized, occluded, dead d3d11 device) drops frames rather than back-pressuring the source tee. This is the single most likely fix for the "tee fan-out froze within seconds" symptom.
- `VideoWidget` got a live↔replay flip: in native render mode, `set_video_surface_visible(enabled, live=...)` picks `_live_surface` for LIVE and the `_frame_label` QLabel for REPLAY/PAUSED. `display_frame()` also auto-flips to QLabel because receiving a Python frame in native mode means the playback controller is showing a non-live timestamp via `SegmentDecoder`. `MultiFeedVideoPanel.apply_tile_visibility(mode, ...)` derives the `live` flag from `mode == LIVE` and passes it through.
- `MainWindow` binds the d3d11 sinks for native feeds before `coordinator.initialize()` runs. The role (operator vs program) is derived from `program_live_only`. The bind goes through `coordinator.bind_native_preview_window_handle` which checks `is_native_preview_active(feed_id)` so python_push feeds and the `force_python_push_preview` escape hatch are both honored.
- `AppSettings.force_python_push_preview` (default `False`) is the operator-level escape hatch — flip to `true` in `[app]` if d3d11 still misbehaves on the local hardware. The qimage path keeps preview Python-bound (~720p ceiling) but is proven stable.

**10 new tests** in `tests/test_native_preview.py`: settings parsing (default / explicit / missing), VideoWidget render-mode flip matrix (qimage default, native live → live_surface, native replay → frame_label, display_frame in native mode flips to qimage, return-to-live flips back, qimage mode ignores live flag, SOURCE_LOST always shows placeholder).

**Outstanding follow-up:** if the tee freeze symptom returns on hardware, the next direction the doc previously suggested still applies — investigate gst-plugins-bad d3d11videosink version-specific behavior, or try gst-plugins-rs `d3d12sink` as a drop-in.

The intended design: replace the operator-window preview path's `appsink → numpy → QImage` round-trip with a native GStreamer video sink (`d3d11videosink` on Windows) that renders directly into each VideoWidget's native window handle. Two sinks per feed (operator + program), bound via `GstVideoOverlay.set_window_handle()` to the per-window `WId()` returned by Qt. A buffer pad probe on `videoconvert.src` would tick metrics and synthesize `FrameOverlayInfo` so the operator's `PlaybackController` state machine still sees frame-arrival events. No pixel data into Python on the preview hot path.

**What actually happened:** the implementation built (the bin shape was correct, tests passed), but on the user's hardware (Windows + PySide6 + gst-plugins-bad d3d11videosink) the binding misbehaved at runtime: a third "Direct3D11 renderer" top-level window appeared (sink falling back to creating its own window despite preemptive `set_window_handle()`), buffers froze in the per-branch tee within ~1 second, and Python eventually went unresponsive. Adding a `prepare-window-handle` sync-bus-message handler that explicitly recognized both per-window sinks (the canonical GstVideoOverlay protocol) did not change the symptoms.

**State after revert:**

- The actual native preview branch construction is bypassed: `pipeline_manager._add_preview_branch` always dispatches to `_add_python_push_preview_branch` regardless of source mode. Native sources still ingest natively (3.A.2 path) but their preview frames flow through the existing appsink + QImage path. `MainWindow.__init__` no longer calls `set_render_mode("native")` or `bind_native_preview_window_handle(...)`; every widget stays in the default `qimage` render mode.
- All the surrounding infrastructure stays in place and tested: `_add_native_preview_branch`, `_on_native_preview_buffer_probe`, the per-window handle setters, `bind_native_preview_window_handle`, `get_feed_pipeline_mode`, `VideoWidget.set_render_mode`, the `_promote_feed_state_on_arrival` helper, the bus-sync-message extension, and the `pipeline_mode` / `python_frames_per_sec` diagnostics. A future retry can flip the one-line dispatch in `_add_preview_branch` back without redoing the wiring.

**Consequence:** the live-preview Python ceiling is unchanged. `target_frame_*` defaults remain at **1280×720@30** until a successful native-preview approach is found. Phase 4 (segmented native muxers) does *not* lift this ceiling — it fixes the recording-side Python ceiling, which is a separate path. End-to-end 1080p still requires both 3.A.3-equivalent (preview side) and Phase 4 (recording side) to land successfully.

**Possible directions for a fresh 3.A.3 attempt** (in rough order of likely success):
- Use `qmlglsink` from `gst-plugins-good`/`bad` and integrate via QML rather than raw `WId()` handle binding. Tighter pygobject/Qt integration.
- Use `glimagesink` (or `autovideosink` letting it pick) in a single-window config (operator only) — sidesteps the multi-sink fan-out which may have contributed to the freeze.
- Investigate whether the third-window symptom is gst-plugins-bad d3d11videosink-version-specific. A newer `gst-plugins-rs` `d3d12sink` may behave differently.
- Throttle the appsink path with an explicit `videorate` to e.g. 15 fps and accept lower-fidelity preview while keeping recording at full rate. This is an architectural compromise (still Python-bound) but proven to work.

**3.B — Explicit per-branch queue policies + queue-depth metrics — ✅ committed**

- ✅ Preview branch in `pipeline_manager.py` configures `leaky=2` (downstream), `max-size-time=200ms`, `max-size-buffers=4`, `max-size-bytes=0` per §4.3. Record branch configures `leaky=0` (non-leaky), `max-size-time=segment_duration_seconds`, `max-size-buffers=256`, `max-size-bytes=0` per §4.4.
- ✅ Queue elements stored in `_branch_queues` and `_branch_queue_caps`; `PipelineManager.sample_queue_depths()` returns `{branch: {buffers, time_ns, max_buffers, max_time_ns}}` for the telemetry hub.
- ✅ `FeedMetricsSnapshot` gained `queue_depth_preview/recording` and `queue_max_preview/recording`. `FeedMetrics.set_queue_depths` / `set_queue_capacity` accept gauge updates from the hub's per-tick sampler refresh.
- ✅ `TelemetryHub.register_queue_depth_sampler(feed_id, sampler)` takes a per-feed callback; `_log_all_snapshots` calls each sampler before emitting the periodic log line and runs the saturation evaluator.
- ✅ Saturation rules: recording branch ≥75% utilization for ≥2 ticks → `RecordingState.RECORDING → RECORDING_ERROR` (only when currently recording) + `recording_branch_saturated` health event. Preview branch ≥75% utilization for ≥2 ticks → `FeedState.LIVE → DEGRADED`. Streak counters reset on drain so transient single-tick spikes don't flap the state.
- ✅ `ApplicationCoordinator` registers `recording_manager.recording_state` on the hub and threads each feed's `pipeline_manager.sample_queue_depths` as the per-feed sampler.
- ✅ Tests: `FeedMetrics` queue-depth round-trips through `snapshot()`, sampler exception caught, two-tick saturation streak triggers state transitions + health event, single tick is a no-op, drain resets the streak. 9 new tests in `tests/test_queue_saturation.py`.
- ✅ Applies uniformly to `python_push` and `native` modes.

**3.C — Validation + transitional banner + Phase-3 readout — ✅ committed**

- ✅ `AppSettings` gained `app_mode` (`"development"` | `"production"`, default `"development"`). TOML loader rejects unknown values.
- ✅ Coordinator emits a per-feed startup log line: `feed=X pipeline=native (clean)` or `pipeline=python_push (transitional)`. When `app_mode=production` and any feed reports `python_push`, an additional `WARNING` log fires with a pointer to Phase 3.A.3.
- ✅ `DiagnosticsWidget` shows a transitional pipeline banner when any feed is python_push. In development mode it's a muted gray notice; in production mode it switches to a high-contrast amber/dark-red callout. Each per-feed line now also shows `qprv N/MAX qrec N/MAX` queue gauges.
- ✅ Tests: `app_mode` defaults to development on missing config / unknown keys raise / explicit production accepted. The diagnostics widget is exercised via the controller tests; the banner toggle is a pure data-driven format change.

### Out of scope for Phase 3

- Codec / container changes (Phase 4 — segmented recording store).
- Replacing the JPEG-thumb rolling replay buffer (Phase 4).
- Hardware-accelerated encode/decode selection (Phase 11). Native ingest in 3.A may pick up GPU decode incidentally if the chosen source elements support it, but that is not the slice's goal.

---

## Phase 4 – Segmented Recording Store + Active Replay Index

**Status: 🔥 Accelerated.** Phase 4 was originally scheduled after Phase 3.B / 3.C, but a deadlock discovered during 3.A.3 reliability testing (see Phase 3 "Blocker discovered" note) makes Phase 4 the practical unblocker for the entire app: today the rolling replay buffer is disabled by a diagnostic early-return and the "Start game recording" button deadlocks the app on click. Both are caused by `MuxedMediaWriter`'s blocking `set_state(...)` calls being driven from streaming-thread signal handlers, and Phase 4's native segmented muxers replace that path entirely.

Goal:

- Replace JPEG/raw-frame replay storage with segmented encoded recording files that also serve as the in-session replay source.

Tasks:

- Implement segment metadata model.
- Write recording segments using ProRes/DNxHR/MJPEG.
- Use 2–5 second segments.
- Store start/end PTS and session time.
- Build in-memory timestamp index.
- Implement replay eligibility queries over completed segments only.
- Implement startup scan/recovery.
- **Remove `MuxedMediaWriter`-driven segment writing entirely.** Use native GStreamer muxers (`splitmuxsink` for time-bounded segment files, or per-segment `filesink`s driven by pad-add events on a downstream branch) wired directly into the per-feed tee. No `pipeline.set_state(...)` calls from signal handlers; no lazy pipeline creation per segment. This is what unblocks the rolling-buffer and "Start game recording" deadlocks documented in Phase 3.
- **Delete the rolling JPEG replay buffer.** `app/media/replay_buffer.py` (and its `ReplayStore` interface) is obsolete once segmented recording exists — replay reads from completed recording segments per §6.6 / §15.2. Removing it cleans up the diagnostic early-return in `start_replay_buffer` as a side effect.

Do not:

- Use MP4 for active recording or replay source media.
- Store JPEG frame dumps as production replay.
- Use H.264/H.265 for the primary in-session replay source.

Exit criteria:

- Replay source consists of encoded recording segments.
- Segment lookup by timestamp works.
- Replay queries use completed segments only and expose the latest replayable time.
- Crash recovery can detect incomplete segments.

Transitional note:

- The current code's single `replay_buffer_seconds` setting (a fixed rolling-window duration over JPEG thumbs + short muxed segments) is replaced by `segment_duration_seconds` plus a segment-availability query. The UI's "rewind 10s" / "rewind 30s" shortcuts become convenience time ranges (§15.3) rather than buffer-size limits. `replay_buffer_seconds` should be removed from `AppSettings` once the new path is in place.

### Slices

Phase 4 lands in five slices. 4.A is the unblock — it replaces `MuxedMediaWriter` with a native segmented sink and lets the app actually record. 4.B-E build the segment-metadata, replay-query, cleanup, and crash-recovery layers on top.

A pragmatic codec choice up front: §5.2 prefers ProRes/DNxHR/MJPEG, with ProRes as the first-choice. **4.A targets MJPEG-in-MKV** rather than ProRes. Reason: `jpegenc` lives in `gst-plugins-good` (universally available on the UCRT64 stack), while `proresenc` is in `gst-plugins-bad` and not always present. MJPEG is intra-frame, frame-accurately seekable per §15.7, and acceptable per §5.2's fallback list. A later slice can introduce a `[recording] codec` knob that switches between MJPEG/ProRes/DNxHR based on detected element availability; the design assumption is that it's a config swap, not a rearchitecture.

**4.A — Native segmented recording (the deadlock unblock)**

- Replace the recording branch's `appsink → MuxedMediaWriter.write_frame` path with `splitmuxsink` writing directly into `<session>/recording/<feed_id>/segment_%05d.mkv` from the recording tee branch. Internal element shape: `record_tee_pad → queue → videoconvert → jpegenc → splitmuxsink (max-size-time = segment_duration_seconds * Gst.SECOND)`.
- `splitmuxsink`'s `format-location` signal picks per-segment filenames; `split-file-closed` (or `format-location-full`) fires when each segment finalizes — for 4.A just log it, 4.B captures metadata.
- New `[recording]` config block per §13: `enabled`, `segment_duration_seconds` (default `4`), `codec` (default `"mjpeg"`), `container` (default `"mkv"`). Read into `AppSettings`.
- `_on_record_sample` becomes a buffer-counter only — it ticks `feed_metrics.tick_recording()` and returns. No `Recorder.write_frame` call. The old `Recorder` and `MuxedMediaWriter` classes stay in the tree (used by 4.D's deletion pass) but are no longer driven by the recording hot path.
- **Audio recording is deferred to a 4.A.bis follow-up.** Today the user's NDI Tools Screen Capture has no audio anyway; synthetic also has none. Wiring audio into `splitmuxsink` (so the segment files mux audio + video) is mechanical but adds a second branch that has to handle the no-audio case gracefully. Easier as a separate slice once 4.A's video path is solid.
- Tests: `splitmuxsink` element configuration (max-size-time, format-location callback round-trip); `[recording]` config parsing; `_on_record_sample` no longer calls `Recorder.write_frame`.

**Exit criteria:** "Start game recording" button no longer deadlocks; clicking it produces a stream of `segment_NNNNN.mkv` files under `<session>/recording/<feed_id>/` while recording is active, sized roughly `(segment_duration_seconds × bitrate)` each.

**4.B — Segment metadata model + persistence + in-memory index**

- New `Segment` dataclass matching §6.3 (minus audio fields for now): `segment_id`, `session_id`, `feed_id`, `file_path`, `codec`, `container`, `start_pts_ns`, `end_pts_ns`, `start_session_time_ns`, `end_session_time_ns`, `duration_ns`, `start_wall_clock_utc`, `end_wall_clock_utc`, `frame_count_estimate`, `size_bytes`, `state` (writing | complete | dirty | corrupt | quarantined), `created_at`, `finalized_at`, `pts_to_session_offset_ns`.
- Capture metadata on `splitmuxsink`'s `split-file-closed` signal — each finalized segment becomes a `Segment` row.
- Persist to SQLite (extend `app/storage/metadata_db.py` with a `segments` table). Per-session SQLite remains the durable source of truth (§6.4).
- `SegmentIndex` in-memory class keyed by `(feed_id, start_pts_ns)`, providing range queries: `segments_overlapping(feed_id, t_start, t_end)`, `latest_replayable_pts(feed_id)`, `earliest_pts(feed_id)`.
- Tests: synthesized `split-file-closed` populates a row; SQLite round-trip; `SegmentIndex` range-query correctness over an interval set with gaps.

**4.C — Replay query API + operator transport integration**

Split into two parts during execution; the data layer was committed cleanly, the operator integration was deferred:

- ✅ **4.C (data layer, committed):** New `RecordingSegmentReplayStore` (`app/storage/segment_replay_store.py`) over the slice 4.B `SegmentIndex`. API: `is_replay_available(recording_state)`, `resolve(feed_id, target_pts_ns, recording_state) -> SegmentReplayLocation | None`, `earliest_pts(feed_id)`, `latest_replayable_pts(feed_id)`, `available_pts_range(feed_id)`. Enforces §10.4 (replay refused when not RECORDING) and §6.6 (in-progress segments excluded). Wired into `ApplicationCoordinator.replay_store`. 13 tests.
- ✅ **4.C.tail (committed):** Operator transport methods now resolve through the replay store and render decoded segment frames via a new `SegmentDecoder` (`app/media/segment_decoder.py`). The replay clock operates in PTS-nanoseconds, the same domain as `SegmentIndex` / `RecordingSegmentReplayStore`. Decisions taken on the four deferred unknowns:
  1. **Rendering target = Python QImage path.** `SegmentDecoder` wraps `cv2.VideoCapture` and decodes MJPEG frames in Python, then hands them to `OutputRenderer.show_frame` like any other `MediaFrame`. Sidesteps the 3.A.3 d3d11 binding bug. 720p MJPEG decode lands well under the 40ms tick budget.
  2. **Replay clock = persistent capture per feed, one-shot decode per tick.** The decoder keeps the segment file open across same-segment ticks and only re-seeks when offsets change. No long-lived second pipeline.
  3. **Segment boundary handling = manual file switching.** `replay_store.resolve(target_pts)` returns `(segment, offset_in_segment_ns)` per tick; when the resolver hands back a different file path, `SegmentDecoder` releases the old capture and opens the new one.
  4. **Slow-motion = clock-side rate.** The replay clock advances `_playback_pts_ns` by `elapsed * rate`; the decoder is rate-agnostic. `set_playback_rate(0.0)` collapses into Pause.

  **Scope:** primary feed only. Multi-feed synchronized replay (rewinding all feeds at the same logical timeline position) requires Phase 5's `SessionClock` to bridge per-feed PTS origins; for now secondary tiles keep showing whatever live frame they last received.

  **Behavior:** during an active recording, the operator can Rewind 10s and see segment-file frames play back at 1.0x or fractional rates; Pause freezes on the current frame; Jump-to-Live snaps back. Replay actions are still rejected when `recording_state != RECORDING`.

  **Tests delivered:** 7 `SegmentDecoder` unit tests (stub `cv2.VideoCapture` for the logic + one real cv2 round-trip against a generated MJPG-MKV); 13 `PlaybackController` tests covering rewind→render, pause-freeze decode, slow-motion, jump-to-live, the eligibility gate, and replay-buffer-span reporting.

  **Known follow-ups (not blocking 4.C.tail):**
  - Multi-feed replay (Phase 5).
  - Audio playback during replay (slice 4.F — pairs with audio-in-segments).
  - Switching to a long-lived `splitmuxsrc` decoder if the per-tick reseek cost ever shows up in profiling on production hardware.

**4.D — Delete the rolling JPEG buffer + cleanup pass — ✅ committed**

- ✅ Deleted `app/media/replay_buffer.py` (`ReplayBuffer`, `ReplayStore`, `ReplayFrameRef`, `ReplayMediaSegmentRef`), `app/media/replay_store_manager.py`, `app/media/muxed_writer.py`, and `app/media/recorder.py`. `RecordingSegmentReplayStore` replaces them.
- ✅ Removed the `_add_branch("replay", ...)` call, `_on_replay_sample` / `_on_replay_audio_sample` / `_on_record_sample` handlers, the entire `_build_replay_pipeline` / `_build_replay_audio_pipeline` / `_teardown_replay_pipeline_locked` block, and the `start_replay_buffer` / `stop_replay_buffer` methods from `pipeline_manager.py`. Replay no longer fans off the live tee; segment-file replay rendering will land in 4.C.tail.
- ✅ `PipelineManager` constructor signature simplified — `recorder` and `replay_buffer` parameters removed. `FeedRuntime` no longer holds `recorder` / `replay_store` either.
- ✅ `RecordingManager` simplified to just owning the `RecordingState` machine — no longer holds per-feed `Recorder` instances.
- ✅ `PlaybackController` now takes `replay_store: RecordingSegmentReplayStore` instead of `replay_store_manager: ReplayStoreManager`. Eligibility checks go through `replay_store.is_replay_available(...)`. Replay rendering paths are stubbed with `TODO(4.C.tail)` markers — the operator's transport buttons drive `ReplayState` and the replay clock cleanly but no frames are decoded yet.
- ✅ Removed obsolete `AppSettings` fields: `replay_buffer_seconds`, `replay_buffer_jpeg_quality`, `replay_audio_segment_seconds`, `recording_filename`, `recording_manifest_filename`, `short_segments_subdir`, `short_segment_filename_prefix`, `audio_container`.
- ✅ Deleted `tests/test_recorder.py` and `tests/test_replay_store.py`. Rewrote `tests/test_playback_controller.py` against the new types. Updated `tests/test_app_settings.py` and `tests/test_recording_settings.py`.
- ✅ Updated `ARCHITECTURE.md` and `CLAUDE.md` to drop "JPEG-thumb rolling replay" language and reflect the segmented-recording + replay-query architecture.
- ✅ All 189 tests pass.

**Exit criteria for 4.D:** the legacy classes (`ReplayBuffer`, `MuxedMediaWriter`, `Recorder`, `ReplayStoreManager`) are gone; `PlaybackController` compiles and runs against `RecordingSegmentReplayStore`; the live preview path and `splitmuxsink` recording branch are unaffected.

**4.E — Crash recovery + startup segment scan — ✅ committed**

- ✅ New `app/storage/session_recovery.py` with three entry points:
  - `mark_dirty_sessions(sessions_root)` walks `<sessions_root>/session_*/session.json`. Manifests with `state ∈ {recording, stopped}` and a missing `finalized_at` are atomically rewritten with `state = "dirty"` (`finalized_at` stays null — that's the §11.4 prompt's marker). Idempotent: re-runs are no-ops.
  - `validate_session_segments(session_paths, db, *, validator=...)` walks `recording/<feed_id>/segment_*.mkv`, runs each through a configurable `SegmentValidator` (default = `cv2.VideoCapture` open + frame-count + first-frame read), and reconciles against SQLite. Three cases: valid file + complete row (no-op), invalid file + any row (move to `<session>/quarantine/<feed_id>/`, update DB row to `quarantined` and re-point `file_path`), valid file + no row (insert as `dirty` with best-effort PTS metadata derived from cv2 duration). Quarantine collisions get a `.recovered_NNN.` suffix so re-runs don't lose data.
  - `load_segment_index_for_session(db, session_id)` returns a populated `SegmentIndex` from SQLite, filtered to `complete` and `dirty` rows (quarantined/corrupt rows are excluded so they never surface in replay queries).
- ✅ `SessionPaths` / `FeedPaths` extended with `quarantine_dir`. The directory is created lazily by the recovery code only when something actually needs quarantining, so happy-path sessions don't accumulate empty directories. `FileManager.session_paths_for_existing(session_id)` returns the layout for an already-created session without re-creating it.
- ✅ `MetadataDb` gained `get_segment_by_path`, `update_segment_state`, `update_segment_file_path`, and `all_session_ids` so the recovery pass can mutate row state without rewriting them.
- ✅ `ApplicationCoordinator.initialize` now calls `mark_dirty_sessions` and `validate_session_segments` before creating the new session. Recovery runs synchronously on app launch; the cost is bounded by the number of prior sessions on disk and dominated by `cv2.VideoCapture` open latency. With cv2 unavailable, the validator falls back to "treat all files as valid" so a stripped-down install doesn't quarantine real recordings.
- ✅ 15 new tests covering: `mark_dirty_sessions` against `recording`/`stopped`/`finalized`/already-`dirty`/`recording`-with-`finalized_at`/no-manifest fixtures, `validate_session_segments` against corrupt + valid + already-cataloged + non-segment files + quarantine collisions + missing `recording/`, and `load_segment_index_for_session` filtering. Validator is injectable so the tests don't need a real cv2/FFmpeg toolchain.

**§11.4 recovery prompt UI — ✅ committed (Resume + Finalize + Discard).** The dialog (`app/ui/recovery_dialog.py`) is modal, blocks the media UI per §11.4 (no close button, Escape ignored), and is shown by `main._run_startup_recovery_flow` between coordinator construction and `coordinator.initialize()`. The recovery scan (`run_startup_scan` — combines `mark_dirty_sessions` + `validate_session_segments`) was factored out of the coordinator into `session_recovery.run_startup_scan` so the bootstrap owns the ordering. All three actions are wired:

- **Resume** calls `SessionManager.adopt_session(session_id)` (new), which loads the existing on-disk paths, builds a state machine starting from `DIRTY`, and transitions to `CREATED` so the operator can drive `CREATED → RECORDING` like a fresh session. The pre-crash `SegmentIndex` is rebuilt from SQLite via `load_segment_index_for_session` so replay queries can address surviving segments. Each feed's `PipelineManager._recording_segment_counter` is seeded from `find_next_fragment_index(recording_dir, db, session_id, feed_id)` — `max(disk_max, db_max) + 1` — which covers the edge case where the crash's tail segment was quarantined (gone from disk, but its `segments` row at the original `fragment_index` is still present and would trip the `UNIQUE(session_id, feed_id, fragment_index)` constraint if the next new file reused that index).
- **Finalize** writes `state = "finalized"` + `finalized_at = now()` to the manifest, leaves segments on disk for the post-session processor (Phase 8).
- **Discard** writes `state = "created"` + `finalized_at = null`, segments stay for §6.8 retention.

Resume is offered only on the **most recent** dirty session (`find_dirty_sessions` returns directory-sorted; `dirty[-1]`). Older dirty sessions are limited to Finalize / Discard — resuming two crashes at once isn't meaningful and complicates the UX. Only the first Resume choice is honored across the prompt loop.

The coordinator's `initialize` gained a `resume_session_id` keyword. When set, it adopts instead of creating a new session and pre-populates the in-memory `SegmentIndex` from the prior session's SQLite rows. The `enable_file_recording` callsite in `toggle_long_session_recording` always recomputes `start_fragment_index` from disk + DB, which incidentally also fixes a latent Stop/Start clobber bug for non-resumed sessions.

7 new tests cover `find_next_fragment_index` (empty / missing dir / disk-walk / non-segment skip / DB max wins), `SessionManager.adopt_session` (state transitions, manifest write), `resolve_dirty_session(RESUME)` (writes `created`), and `_on_splitmuxsink_format_location` honoring the seeded counter.

**Exit criteria for 4.E:** an unclean shutdown leaves `session.json` in `dirty` state on disk; corrupt segments end up in `<session>/quarantine/<feed_id>/`; in-progress (no DB row) segments that survived the crash get a `dirty` SQLite row so they're addressable by future tooling; the next launch sees the dirty state without crashing.

### Out of scope for Phase 4 (deferred)

- ~~**Audio in segments (slice 4.F).**~~ ✅ Committed. The audio tee feeds `queue → valve → audioconvert → audioresample → opusenc → splitmuxsink.audio_%u` when audio is observed on the source. Phase 9.C makes this wiring dynamic — the audio_record branch is built only after a buffer probe confirms audio buffers are flowing on the source's audio tee. Sources without audio produce video-only segments without operator intervention. The legacy `[recording] audio_enabled = false` config flag is preserved as a manual override for operators who want to force video-only regardless of source capability.
- **ProRes / DNxHR codec selection.** §5.2's first-choice codecs require `gst-plugins-bad` elements that aren't always available on UCRT64. Add a config-driven codec selector once MJPEG path is solid; this becomes a small Phase-4-bis or a Phase 11 task (hardware acceleration ties into encoder choice).
- ~~**§11.4 recovery-prompt Resume action.**~~ ✅ Committed. `SessionManager.adopt_session` adopts the existing session_id; `find_next_fragment_index` consults disk + DB to seed the next safe segment index; the bootstrap threads the chosen session_id into `coordinator.initialize(resume_session_id=...)`.
- ~~**Phase 3.B and 3.C resumption.**~~ ✅ Committed after Phase 4 (queue policies, queue-depth metrics, saturation-driven health rules, transitional pipeline banner, `app_mode` setting).
- ~~**Native preview video sink (3.A.3 retry).**~~ ✅ Committed. NATIVE feeds use d3d11videosink with leaky per-window queues; replay flips the QStackedLayout to the QImage layer so segment-decoder frames remain visible. `force_python_push_preview` config knob is the escape hatch.

---

## Phase 5 – Shared Session Timeline

Goal:

- Introduce true timestamp-based multi-feed replay.

Normative references:

- §8.1 SessionClock contract.
- §8.6 / §8.6.1 multi-feed sync rules and the per-feed frame-clamping rule.
- §15.5 missing media behavior (freeze-on-nearest-frame, not blank).

Tasks:

- Implement `SessionClock`.
- Implement `FeedTimeline`.
- Map feed PTS to session time.
- Update replay requests to use session timestamps.
- Handle feed startup offsets.
- Handle missing feed ranges via the §8.6.1 clamping rule.

Exit criteria:

- Replay request is based on session time.
- Multiple feeds can be queried for same replay window.
- Every operator tile renders something on every tick during replay (per §15.5 / §8.6.1) — no blank-during-rewind state.
- Missing feed media does not crash replay.

### Slices

Phase 5 lands in three slices:

- **5.A — `SessionClock` + per-feed PTS-to-session-time capture — ✅ committed.** New `app/core/session_clock.py` (monotonic anchor, `now_session_time_ns()`). `ApplicationCoordinator` constructs one instance and threads it into each `PipelineManager` alongside the metadata-db and segment index. `_on_jpegenc_buffer_probe` captures `session_time_ns` on the first buffer of each segment; `_finalize_pending_segment_locked` derives `pts_to_session_offset_ns = first_session_time_ns - first_pts_ns` and populates `start_session_time_ns` / `end_session_time_ns` / `pts_to_session_offset_ns` on the `Segment` row. Round-trips through SQLite + `load_segment_index_for_session`. 7 new tests cover the clock origin/advance, the per-feed offset capture, the finalize population, the no-clock fallback, and the recovery-load round-trip. No operator-visible behavior change.
- **5.B — read-side: session-time queries + `nearest_frame_location` (§8.6.1 clamping rule) — ✅ committed.** `SegmentIndex` gained `segments_overlapping_session_time`, `feeds_with_coverage_at`, `earliest_session_time`, `latest_replayable_session_time`, and `cross_feed_session_time_range`. `RecordingSegmentReplayStore` gained `resolve_session_time` (strict — returns `None` when out of coverage; the rewind-target picker uses this), `nearest_frame_location` (the §8.6.1 clamping rule — exact match in coverage, earliest segment with offset 0 when before any coverage, latest-before-`t` with offset clamped to segment duration when after coverage or in a gap), `earliest_session_time` / `latest_replayable_session_time` per feed, `available_session_time_range` across feeds, and `feeds_with_coverage_at` pass-through. Pre-5.A segments (NULL session-time fields) are silently excluded from session-time queries — they remain queryable by PTS. 23 new tests cover the late-joining-feed scenario, the four `nearest_frame_location` branches (in-coverage, before-earliest, in-gap, after-latest), the not-recording guard, and the empty/writing-only feed cases.
- **5.C — `PlaybackController` switches to session-time + multi-feed render + UX changes — ✅ committed.** `_playback_pts_ns → _playback_session_time_ns` throughout (replay clock, rewind targets, pause anchor, status overlay). `_resolve_rewind_target_locked` anchors on the operator's current playback position when in REPLAY/PAUSED so repeated Rewind 10s clicks accumulate (10s + 10s = −20s); LIVE/SOURCE_LOST anchors on the latest replayable session-time across all feeds so the first click from live always lands at "now − 10s". `_render_at_session_time_ns` iterates **every enabled feed**, calls `nearest_frame_location` per feed (slice 5.B clamping rule), decodes via a per-feed `SegmentDecoder` (each owns its own `cv2.VideoCapture`), and pushes frames into the renderer — every tile renders something on every tick during replay. `_update_state_timestamps_locked` reads cross-feed session-time bounds for span + seconds-behind-live so the operator UI shows the union of replayable history regardless of which feed joined when. Rewind 30s button + signal + `rewind_30_seconds` method + matching test all dropped. 6 new tests (`test_rewind_twice_from_live_accumulates_to_minus_20s`, `test_rewind_from_replay_anchors_on_current_position_not_live`, `test_rewind_from_pause_also_anchors_on_current_position`, `test_rewind_clamps_at_earliest_session_time`, `MultiFeedRenderTests.test_rewind_to_before_feed_b_renders_freeze_on_b`, `MultiFeedRenderTests.test_rewind_to_session_time_5_starts_b_playing`) verify the §8.6.1 worked example end-to-end against synthesized two-feed fixtures.

**Phase 5 status: 🟢 Complete.** All three slices (5.A / 5.B / 5.C) committed. Multi-feed synchronized replay works end-to-end: feeds joining at different session times stay aligned via the per-feed clamping rule, repeated Rewind 10s clicks accumulate, and the operator UI bound (span + seconds-behind-live) reflects cross-feed coverage. **293 tests passing.**

---

## Phase 6 – Multi-Feed Replay Controller

**Status: 🟢 Complete.** Most of Phase 6 was delivered as a side-effect of Phase 5 (the multi-feed render loop in 5.C, the `nearest_frame_location` clamping in 5.B, the per-feed `pts_to_session_offset_ns` in 5.A). The remaining concrete item — degraded replay indicators (§15.5) — landed as a single follow-up slice on top of Phase 5.

Goal:

- Make replay multi-feed and synchronized.

Tasks:

- ~~Update PlaybackController to control multiple feeds.~~ ✅ Slice 5.C — `_render_at_session_time_ns` iterates every enabled feed via `nearest_frame_location`.
- ~~Support pause, resume, slow motion, jump to live.~~ ✅ All four work multi-feed naturally because the replay clock advances in shared session-time and the render loop reads it on every tick.
- ~~Keep program output live-only.~~ ✅ The `live_only` flag (set on the program controller since Phase 2) skips replay machinery entirely.
- ~~Keep recording active during replay.~~ ✅ Recording branch is independent of preview/replay (since Phase 4.A); never paused during transport.
- ~~Add degraded replay indicators.~~ ✅ This slice — see below.

### Phase 6 slice — degraded replay indicators

- `SegmentReplayLocation` gained `is_freeze: bool = False`. `RecordingSegmentReplayStore.nearest_frame_location` sets `True` for the three §8.6.1 clamping branches (before-earliest / after-latest / in-gap) and `False` for exact coverage. The strict resolvers (`resolve`, `resolve_session_time`) always emit `False` since they only return on exact match.
- `UiState.feeds_in_freeze_frame: tuple[str, ...]` — the controller's `_render_at_session_time_ns` collects the list of feeds whose `nearest_frame_location` returned `is_freeze=True` and writes it into the state every tick. Cleared on `jump_to_live` and on the recording-stop snap-back.
- `VideoWidget.set_freeze_indicator(visible)` — small "FROZEN" badge in the top-right corner, amber background. Hidden by default.
- `MultiFeedVideoPanel.apply_freeze_indicators(feeds)` — sets each tile's badge by feed_id. Wired into `MainWindow._render_state` so Qt re-renders when the controller emits a state change.
- 5 new tests cover the four `is_freeze` branches in the replay store + controller-side state population (late-join scenario sets `("ndi_b",)`; in-coverage range sets `()`; jump-to-live clears the list) + widget badge toggle.

Exit criteria:

- ✅ Operator can replay multiple feeds for same time range.
- ✅ Program output remains live.
- ✅ Recording continues during replay.
- ✅ Missing feed does not break replay (clamps via §8.6.1, surfaces as a per-tile FROZEN badge).

---

## Phase 7 – Disk Budget Validation + Replay Availability Hardening

Goal:

- Make the single-writer recording-backed replay model explicit, measurable, and safe under real disk constraints.

Tasks:

- Validate configured feed count, resolution, bitrate, codec, and segment duration against expected disk throughput.
- Warn at startup if the configured recording workload exceeds the selected disk budget.
- Harden active replay availability calculations over completed recording segments from recording start through the latest replayable finalized segment.
- Expose latest replayable session time in diagnostics and UI.
- Confirm replay does not read currently-writing segments.
- Confirm replay navigation and instant-replay shortcut management never delete recording media.

Exit criteria:

- Disk write strategy is explicit and validated.
- App warns if configured bitrate/feed count exceeds disk budget.
- Replay availability is calculated from completed recording segments only, from recording start through the latest replayable finalized segment.
- The UI can show when replay is unavailable or lagging behind live because the latest segment is still being written.

### Slices

Phase 7 lands in three slices. 7.A and 7.B are independent (different surfaces) and could land in either order; 7.C is mostly a confirming pass against the prior slices and is most useful last.

**7.A — Disk budget estimation + startup validation**

- New setting `disk_budget_mb_s: float = 200.0` (default conservative SATA-SSD threshold; configurable per-deployment in `app_settings.toml` under `[recording]`).
- New `app/core/disk_budget.py` module:
  - `estimate_per_feed_mb_s(target_frame_width, target_frame_height, target_fps, codec)` — bitrate estimate from frame size × fps × codec coefficient. MJPEG default coefficient `0.10` (≈ jpegenc default quality 85 at typical broadcast content). Other codecs raise `NotImplementedError` for now (Phase-4 only ships MJPEG).
  - `estimate_total_mb_s(enabled_feeds, settings)` — sum across feeds.
  - `validate_budget(estimated_mb_s, budget_mb_s)` returns one of `BudgetVerdict.OK` / `BudgetVerdict.WARN` (≥ 80% of budget) / `BudgetVerdict.OVER_BUDGET` (≥ 100%).
- `ApplicationCoordinator.initialize` (or earlier in `build_default_application_coordinator`) calls the validator at startup. `OK` → `INFO` log. `WARN` → `WARNING` log + a `disk_budget_warn` health event. `OVER_BUDGET` → `ERROR` log + a `disk_budget_over` health event. Recording is still allowed (operator-overridable); the warning is surfaced through the diagnostics banner.
- `DiagnosticsWidget` adds a one-line readout: `disk: 75/200 MB/s est ✓` / `disk: 165/200 MB/s est ⚠` / `disk: 240/200 MB/s est ✗`.
- Tests: per-feed estimation against known frame size / fps fixtures; total-aggregate; threshold matrix (OK / WARN / OVER); health-event emission; TOML parsing of `disk_budget_mb_s` (default + override).

**Out of scope for 7.A:** dynamic re-validation when `target_frame_*` changes mid-session (settings are immutable per app run); auto-detection of disk type or measured throughput (a separate observability slice).

**7.B — Latest-replayable surface in diagnostics + status bar**

- `UiState` gains:
  - `latest_replayable_session_time_ns: int | None` — the cross-feed latest replayable session time (already computed for the overlay; expose explicitly).
  - `live_lag_behind_replayable_seconds: float` — distance between "now" (latest live overlay's `capture_timestamp` mapped to session time, or the controller's monotonic now) and the latest replayable session time. Typically equals `~segment_duration_seconds` while the in-progress segment is being written.
  - `replay_available: bool` — `False` until the first segment finalizes (covers the "operator just started recording, no replay yet" UX gap).
- `PlaybackController._update_state_timestamps_locked` populates the new fields from `replay_store.available_session_time_range()` and the per-feed running session-time at the latest live tick.
- `StatusBarWidget` adds an indicator: `Replay covers 0:00 – 0:42 (latest finalized -4s)` when `replay_available`; `Replay not yet available — first segment finalizing` when `replay_available is False`.
- `DiagnosticsWidget` shows the `live_lag_behind_replayable_seconds` value alongside the existing latency block — useful for diagnosing a wedged splitmuxsink (lag would grow unboundedly instead of hovering near `segment_duration`).
- Tests: state population for the three cases (no segments yet → `replay_available=False`; one finalized segment → range + lag); status-bar text formatting; behavior across `recording_segment_duration_seconds=4.0` and a hypothetical 8s configuration.

**Out of scope for 7.B:** the operator-facing "rewind back to" picker UI (a slice in the future scope-of-marker work). 7.B only adds the read-only surface so the operator knows what's available.

**7.C — Replay safety invariants + clip-shortcut audit**

- Defensive tests asserting:
  - `RecordingSegmentReplayStore.resolve` / `resolve_session_time` / `nearest_frame_location` all return `None` (or skip) for `state="writing"` rows even when their PTS / session-time range would otherwise overlap the target. (Some coverage exists from 4.C; 7.C consolidates.)
  - `SegmentIndex.latest_replayable_pts` and `latest_replayable_session_time` exclude writing segments. (Already true; lock in.)
  - `PlaybackController.rewind_10_seconds` / `pause_playback` / `set_playback_rate` / `jump_to_live` exercised against an index that contains a writing tail segment never resolve into it.
- Code-path audit asserting transport methods take no file-system mutation actions:
  - `_render_at_session_time_ns`, `_resolve_pause_anchor_locked`, `_resolve_rewind_target_locked`, `_on_replay_timer_tick` should never call `Path.unlink`, `Path.replace`, `Path.rename`, `os.remove`, or any `MetadataDb` write. Add a test that uses an instrumented `MetadataDb` / fake `Path` to detect any write call originating from those methods.
- Documentation pass: §15.2 / §15.7 already say replay reads from completed segments only; 7.C adds an inline note pointing at the test file that locks this in.

**Out of scope for 7.C:** retention / cleanup policy enforcement (§6.8 — separate phase). 7.C only confirms the read path is harmless; cleanup happens elsewhere via the recovery scan + future retention sweeper.

**7.D — Resume continues the crashed game**

Without 7.D, "Resume" in the §11.4 recovery dialog only adopts the session and rebuilds the in-memory `SegmentIndex`. The first Start press after Resume allocates a fresh `game_NNN/` folder, so pre-crash content lives in a different game folder and is excluded from replay by the per-game filter (Phase 7.B-ext). Operators who want to replay highlights from the crashed game cannot.

Worse, even if the per-game filter were relaxed to include pre-crash segments, the new `SessionClock` starts at `session_time = 0` for the resumed run, so post-resume `start_session_time_ns` values overlap pre-crash ones in raw integer terms. Comparison-based queries (`available_session_time_range`, `nearest_frame_location` clamping, the per-game filter) silently misbehave across the clock-domain seam.

7.D fixes both halves:

- **`SessionClock.rebase(anchor_session_time_ns)`** — sets `_start_monotonic_ns` so `now_session_time_ns()` returns the anchor at the moment of the call. Safe only before any buffer has been processed by the new clock (rebasing after the first segment's first-buffer probe would corrupt the writing segment's `start_session_time_ns`).
- **`ApplicationCoordinator._setup_resume_continuation(session_paths)`** — runs immediately after `_populate_segment_index_from_resume`. Walks `<recording>/` for the highest `game_NNN/` folder, filters loaded segments to that folder via path-component match (avoids the `game_001` ⊂ `game_0011` substring trap), computes `min(start_session_time_ns)` and `max(end_session_time_ns)` across them, calls `session_clock.rebase(latest_end + 1ms)`, and stashes a `_ResumeContinuation(game_subdir, game_start_session_time_ns)` on the coordinator.
- **`toggle_long_session_recording` Start path** — if `_resume_continuation` is set, reuse `continuation.game_subdir` (no new game folder is allocated), set the per-game filter to `continuation.game_start_session_time_ns` (so pre-crash segments stay visible to replay), then clear `_resume_continuation`. `find_next_fragment_index` against the existing `<game_NNN>/<feed_id>/` folder picks the next index past the pre-crash files, so segment filenames continue monotonically within the game.

Bail-out branches (continuation stays None, fall through to the normal new-game path):

- recording dir doesn't exist or has no `game_NNN/` folder
- no segment in the index lives under the crashed game's folder
- none of those segments has populated session-time fields (pre-5.A legacy rows can't anchor the rebase)
- the coordinator has no `SessionClock` attached (test fixtures only)

After 7.D the operator workflow for a crash mid-game becomes: app crashes → restart → recovery dialog → Resume → press Start → recording continues in the same game folder, fragment_index past the pre-crash tail, replay can scrub back into pre-crash highlights with no clock-domain confusion. Stop on the resumed game, then a subsequent Start, behaves normally — fresh `game_(N+1)/` folder, per-game filter resets to "now". Locked in by `tests/test_resume_continuation.py` and `tests/test_session_clock.py::SessionClockRebaseTests`.

**Out of scope for 7.D:** post-session UI affordance to replay across all games in a finalized session (that's a separate read-only viewer concern); recovery for sessions that crashed before any segment finalized in the crashed game (continuation falls back to fresh-game allocation).

**7.H — Play markers + Replay Play (✅ shipped)**

The replay model previously had only time-bounded rewind ("Rewind 10s" stacks 10-second offsets). 7.H added a play-bounded mode: the operator marks play boundaries during the game with the **"Next Play"** button, and the new **"Replay Play"** transport button seeks to the start of the currently-open play.

Shipped in four sub-slices:

- **7.H.1 ✅ — `plays` SQLite table + `PlayManager`.** Table per the §6.7 schema; `UNIQUE(session_id, game_subdir, play_number)`. `PlayManager` (`app/core/play_manager.py`) owns the in-memory currently-open-play pointer and persists boundary transitions. Hooks: `toggle_long_session_recording` Start opens the next play (Play #1 for a fresh game, `max(existing) + 1` on Phase 7.D resume continuation); Stop closes the current play; `coordinator.mark_next_play()` advances the boundary. Crash recovery via `_setup_resume_continuation` calls `auto_close_open_plays_for_session` to close any NULL-end plays at the latest finalized segment's end, flagging `auto_closed_on_crash = TRUE`.
- **7.H.2 ✅ — operator UI.** "Next clip" button (formerly bound to the no-op `advance_short_segments` stub) renamed to "Next Play" and rebound to `coordinator.mark_next_play`. New `ControlsWidget.set_recording_state(bool)` toggles enabled state. The `advance_short_segments` stub was removed.
- **7.H.3 ✅ — overlay current-play badge.** `UiState.current_play_number` and `PlaybackOverlayInfo.current_play_number` populated by `PlaybackController._update_state_timestamps_locked` from `PlayManager.current_play_number()`. The playback overlay renders `Play #N` directly under the mode badge; the operator status bar gained a "Play" row.
- **7.H.4 ✅ — "Replay Play" transport.** `PlaybackController.replay_current_play()` seeks playback to the currently-open play's `start_session_time_ns` (clamped defensively to the per-game replay scope's earliest), transitions the replay state machine through `SEEKING → REPLAYING`, and resumes at 1.0x. New "Replay Play" button on the operator controls panel; same recording-state gating as Next Play.

Tests:

- `plays` table CRUD round-trip; `UNIQUE` constraint enforcement.
- PlayManager state machine: Start opens Play #1; Next Play closes current and opens next; Stop closes current; resume after crash auto-closes the in-flight play and sets the flag.
- Per-game scope: starting game_002 resets the counter to Play #1 even within the same session.
- `replay_current_play()` seeks to the right session_time; behaves correctly when called from LIVE / REPLAY / PAUSED.
- UI toggle disabled when recording inactive; overlay badge populated.

**Out of scope for 7.H:** per-play tags / notes / scoring metadata; multi-feed-aware play markers (one feed in a play, another not); a "Replay Play -2" / scroll-back-through-plays UI. The data structure supports those — the UI doesn't expose them in the MVP.

### Phase 7 sequencing notes

- **7.A and 7.B are independent.** Either can land first; both are needed before Phase 7 is ✅. Recommend 7.A first because the disk-budget signal is what catches a misconfigured production deployment before the operator hits a wedged-recording bug — higher operational value than the latest-replayable surface, which is mostly diagnostic.
- **7.C should land last among the original three.** It's a confirming/locking-in pass; the tests it adds are most valuable after the surfaces it covers exist.
- **7.D depends on 7.B** because it reuses the per-game filter from 7.B-ext. Without that filter, the continuation has nothing to scope against.
- **7.E (audio-missing health event), 7.F (segments-table UNIQUE-constraint migration), and 7.G (audio re-link on splitmuxsink rebuild) are independent bug-fix slices** that landed after the original three. None gate the others.
- **7.H depends on the per-game scoping of 7.B-ext** (plays are per-game, like segments). It doesn't depend on 7.D, but the resume-after-crash play auto-closure rides on 7.D's resume path.
- **8.D depends on 7.H** because the `plays.json` sidecar reads the `plays` table that 7.H lands.
- **No hardware-specific risk** in any Phase 7 slice. All testable against synthesized fixtures.

---

## Phase 8 – Post-Session MP4 Processor

Goal:

- Add the separate manual processor that creates long-form MP4 deliverables and per-game `plays.json` sidecars after the recording app is shut down.

Tasks:

- Add a separate program in the same repository.
- Accept a session folder as input.
- Refuse to run if the session is active, recording, dirty, or not finalized.
- Read `session.sqlite`, recording segments, and the `plays` table.
- Produce one long-form MP4 per feed/game recording.
- Produce one `plays.json` sidecar per game, describing operator play boundaries.
- Write `ExportArtifact` metadata for successful and failed outputs.
- Preserve source recording segments unchanged.

Exit criteria:

- Processor can be run manually after shutdown against a finalized session folder.
- Long-form MP4 outputs are created.
- `plays.json` sidecars match the in-DB `plays` rows for each game.
- Failed exports are recorded without damaging source media.

### Slices

Phase 8 ✅ shipped in four slices.

**8.A ✅ — CLI scaffold + session validation**

- New module: `app/tools/post_session_processor.py`. Standalone entry point: `python -m app.tools.post_session_processor <session_path>`.
- Reads `<session_path>/session.json`, validates `state == "finalized"`. Refuses every other state (`created`, `recording`, `stopped`, `dirty`, `archived`) with an explicit error message naming what's wrong.
- File-locks `<session_path>/.processing.lock` for the duration of the run so two concurrent processes can't trample each other.
- Reads the segments table from the shared `metadata.db` (filter by `session_id`) to produce a *plan* — list of long-form artifacts that would be exported, grouped by `(game_subdir, feed_id)`. `--dry-run` prints the plan; the default also prints it for visibility.
- No actual encoding yet; 8.A is the gate-and-plan pass.
- Tests: each invalid `state` rejected with a clear error; valid state accepted; lock acquire / release; plan generation against a synthetic session fixture.

**Out of scope for 8.A:** any encoder logic. 8.A is plumbing.

**8.B ✅ — Long-form MP4 export**

- For each `(game_subdir, feed_id)` in the 8.A plan:
  - Resolve the ordered list of `state="complete"` segments belonging to that game folder + feed.
  - Build a GStreamer pipeline: `concat → decodebin → x264enc → mp4mux → filesink` (or equivalent ffmpeg subprocess if `gst-plugins-bad` x264enc isn't available on the deployment). MJPEG decode → H.264 encode → MP4 mux. Output goes to `<session_path>/processed/<game_subdir>/<feed_id>.mp4`.
  - Audio handling: when segments contain audio, pass it through to MP4 via `aacenc` or `avenc_aac`. When audio is absent (Phase 9.C video-only segments, or `audio_enabled = false`), mux video-only.
- Source MKVs stay untouched on disk.
- Per-artifact progress is logged so a long export shows incremental status.
- Tests: synthesized MKV fixtures (single 1-frame segments) → verify MP4 output exists, has expected duration via `gst-discoverer-1.0` or ffprobe; verify source files are unmodified.

**Out of scope for 8.B:** GPU-accelerated encoding (NVENC, etc.). Software x264 is fine for a manual post-process; speed matters less than reliability. As shipped, 8.B uses an ffmpeg subprocess (`-f concat -safe 0 -i list.txt -c:v libx264 -preset medium -crf 23 -c:a aac -b:a 128k`) — the GStreamer pipeline alternative was deferred since ffmpeg is universally available on MSYS2 UCRT64 via `pacman -S mingw-w64-ucrt-x86_64-ffmpeg`.

**8.C ✅ — `ExportArtifact` metadata table**

- New SQLite table in `metadata.db`:

  ```sql
  CREATE TABLE export_artifacts (
      id INTEGER PRIMARY KEY,
      session_id TEXT NOT NULL,
      kind TEXT NOT NULL,            -- 'long_form' (only kind today)
      game_subdir TEXT,              -- 'game_NNN'
      feed_id TEXT,                  -- nullable for multi-feed clips
      output_path TEXT NOT NULL,
      status TEXT NOT NULL,          -- 'success' | 'failed'
      error_message TEXT,
      size_bytes INTEGER,
      duration_ns INTEGER,
      started_at TEXT NOT NULL,
      finalized_at TEXT,
      FOREIGN KEY(session_id) REFERENCES sessions(session_id)
  );
  ```

- Schema migration applied by `MetadataDb._initialize_schema` so existing DBs gain the table on first run.
- 8.B writes one row per attempted artifact: `success` rows include `size_bytes` + `duration_ns`; `failed` rows include `error_message` and the partial output path (if any) for forensics.
- Re-running the processor against the same session is **idempotent**: 8.B skips artifacts whose `(session_id, kind, game_subdir, feed_id)` already has a `success` row. `--force` re-runs.
- Tests: schema round-trip (insert / query); idempotent re-run skips successes but retries failures.

**8.D ✅ — `plays.json` sidecar per game**

- Depended on Phase 7.H landing the `plays` SQLite table; both shipped together.
- For each `game_subdir` in the long-form export plan (independent of MP4 encode success — the sidecar is useful forensically even on partial-failure runs), `app/tools/plays_json_export.py` queries `db.plays_for_game(...)`, renders `<session_path>/processed/<game_subdir>/plays.json`:

  ```json
  {
    "session_id": "session_NNN",
    "game_subdir": "game_NNN",
    "play_count": 2,
    "game_duration_seconds": 7.7,
    "plays": [
      { "play_number": 1, "start_seconds": 0.0, "length_seconds": 4.5 },
      { "play_number": 2, "start_seconds": 4.5, "length_seconds": 3.2 }
    ]
  }
  ```

- `start_seconds` is **game-relative** (zero is the first play's start). `length_seconds` is the play's duration. Open plays (`end_session_time_ns is None`) are excluded with a warning — by the time the post-processor runs the session must be finalized, so every play should be closed; an open play is a sign of a recovery edge case.
- One JSON per game (not per feed) because plays are operator-scoped. `auto_closed_on_crash` is not surfaced in the JSON — consumers don't need to know.
- Tests in `tests/test_plays_json_export.py` lock the shape, the empty-plays case, the open-play exclusion, and idempotent rewrite behavior.

**Out of scope for 8.D:** per-play sub-clip MP4s. The decision (per §6.7) is that the JSON sidecar plus the long-form MP4 covers the consumer use case; downstream tooling handles slicing if needed.

### Phase 8 sequencing notes

Historical (all shipped):

- **8.A landed first.** The validation gate kept the processor from corrupting an active session before the encoder was wired.
- **8.C landed alongside / just before 8.B.** 8.B writes to 8.C's `export_artifacts` table; the schema needed to exist for 8.B to be testable end-to-end.
- **8.D landed after 7.H.1.** The `plays` SQLite table that the sidecar reads only existed once 7.H.1 shipped, so 8.D was deferred until then.
- **Encoder shipped is ffmpeg subprocess**, not GStreamer. The deployment dependency is `pacman -S mingw-w64-ucrt-x86_64-ffmpeg` on MSYS2 UCRT64 (or any equivalent ffmpeg install on PATH). `--ffmpeg-path` overrides the binary location.

---

## Phase 9 – Audio Integration

Goal:

- Make audio recording self-healing — no operator config flag, no preview freeze when the NDI source has no audio stream.

Phase 9 was originally framed around master-audio strategy (mixed-source, commentator overlay, replay audio). The actual deployment use case is simpler: each NDI feed has its own embedded audio, that audio is muxed into the feed's own segment MKV / long-form MP4, nobody listens during live operation, and there is no commentator. The per-feed model is already implemented (slice 4.F + Phase 7.G). What's missing is robustness when a source doesn't actually produce audio.

Tasks:

- Detect at runtime whether each feed's NDI source is producing an audio stream.
- Wire the audio_record branch into `splitmuxsink` only when audio is observed.
- When audio is absent, record video-only segments without operator intervention.
- Surface the runtime decision so the operator can verify recording mode at a glance.

Exit criteria:

- Operator does not have to know whether the source produces audio in advance — pipeline adapts at runtime.
- A no-audio NDI source produces video-only segments with no live-preview stall.
- An audio-producing NDI source produces video+audio segments as today.
- A source that gains audio mid-session picks it up on the next Stop/Start cycle (mid-game re-attach is out of scope — `splitmuxsink` can't add a sink pad mid-stream).

### Slices

**9.C — Dynamic no-audio handling** (replaces the Phase 7.E config-flag workaround). Phase 9 collapses to this single slice: the original 9.A "master audio source selection" is N/A (per-feed audio is the model and is already implemented), and 9.B "audio in replay + export" is N/A (the operator never plays back audio during operation, and `ffmpeg` already passes audio through to the long-form MP4 in Phase 8.B when segments contain it).

Today the audio chain is wired into `splitmuxsink` unconditionally based on `[recording] audio_enabled`. If the source has no audio, splitmuxsink waits indefinitely for audio buffers, the record queue back-pressures the tee, and the live preview freezes. Phase 7.E logs a `category=audio_missing` health event after a 5s grace period — the paper trail exists, but the workaround (`audio_enabled = false`) is operator-driven. Phase 9.C makes the pipeline self-healing:

- At pipeline build time: build the audio infrastructure (tee, source, live audio branch, no-op record-side appsink to drain the tee). Do **not** wire the audio_record branch into `splitmuxsink` yet.
- Install a buffer probe on the audio tee's input. On the first audio buffer ever observed, set a sticky `_audio_present_observed` flag.
- On each "Start game recording" press (initial wiring) AND on each `_rebuild_splitmuxsink_locked` (Stop/Start cycle): check the flag.
  - If True and the audio_record branch isn't already built: build it now (audioconvert → audioresample → opusenc), request `audio_%u` on splitmuxsink, link encoder.src → audio pad. Future segments are video+audio.
  - If False: leave splitmuxsink without an `audio_%u` pad. Segments are video-only.
- The `audio_missing` health event from Phase 7.E becomes redundant for the freeze-prevention purpose (since there's no longer a freeze) but stays as informational telemetry — the operator's diagnostics widget still shows the count of "audio expected but not produced" events.

Edge cases:

- **Operator presses Start before audio's first buffer arrives.** Decision is made on whatever's been observed so far. Game 1 may be video-only even though audio would have come a second later. Game 2 (after Stop/Start) re-evaluates and picks audio up.
- **Source produces audio mid-game.** Game 1 stays without audio (can't add a pad to splitmuxsink mid-stream); game 2 has it after the Stop/Start rebuild.
- **Source loses audio mid-game.** Game 1 stays with audio. The audio chain is in place but no buffers flow — same behavior as the rest of the pipeline. Operator will see the audio_missing health event fire and can decide to Stop/Start to drop into video-only mode for game 2.

Out of scope for 9.C:

- Mid-stream audio attach/detach within a single game. Splitmuxsink doesn't support adding sink pads to a running mux, and the per-game-folder model means a Stop/Start is the natural seam.
- Removing the `[recording] audio_enabled` config option. Keeping it as a manual override (e.g., for an operator who wants video-only by policy regardless of source capability) is harmless.

This eliminates the `audio_enabled = false` config-flag foot-gun: an operator deploying a no-audio NDI sender (Screen Capture, muted mic, audio-disabled camera) doesn't have to know in advance — the pipeline adapts.

---

## Phase 10 – Failure Recovery and Production Hardening

Goal:

- Make system resilient enough for real games.

Tasks:

- Camera disconnect/reconnect handling.
- Disk full handling.
- Slow disk handling.
- Corrupt segment quarantine.
- Startup recovery.
- Graceful shutdown.
- Health event UI.

Exit criteria:

- Required failure scenarios are tested.
- Operator receives visible warnings.
- App can recover usable session data after crash.

---

## Phase 11 – Hardware Acceleration and Performance Tuning

Goal:

- Scale to target feed count/resolution.

Tasks:

- Add hardware acceleration selection.
- Add software fallback.
- **TODO: Add ProRes / DNxHR codec support** (§5.2 deferral). Concretely:
  (a) detect `gst-plugins-bad` element availability at startup and gate the
  `[recording] codec` selector accordingly; (b) wire `proresenc` /
  `avenc_dnxhd` into the splitmuxsink branch alongside the existing `jpegenc`
  path; (c) add codec coefficients to `_CODEC_RATIO_VS_RAW_RGB` in
  `app/core/disk_budget.py` so Phase 7.A's startup validator covers them;
  (d) extend `[recording] container` to accept `mov` (the natural ProRes/DNxHR
  container) and validate against the chosen codec at load time. This is
  bundled into Phase 11 because hwaccel encoder paths (NVENC, QuickSync) and
  codec choice are co-decided.
- Tune queue sizes.
- Tune segment duration.
- Tune encoder settings.
- Validate 2-feed, 4-feed, and stretch targets.
- Record performance profiles.

Exit criteria:

- Primary hardware target passes acceptance test.
- CPU/GPU/disk metrics are within safe limits.
- No unbounded memory growth.
- ProRes / DNxHR ships as a config-selectable codec (or the §5.2 ranking is
  rewritten to reflect MJPEG-as-final).

---

# 19. Recommended Implementation Defaults

Use these defaults unless hardware testing proves otherwise. The "shipped" column shows the current state; the "target" column shows the long-term default.

| Setting | Shipped today | Long-term target |
|---|---|---|
| Recording / replay-source codec | MJPEG | ProRes or DNxHR (Phase 11; see §5.2) |
| Recording / replay-source container | MKV | MOV or MKV |
| Recording segment duration | 4 seconds | 4 seconds |
| In-session replay scope | recording start through latest finalized segment while recording is active | same |
| Instant replay shortcuts | Rewind 10s only | 10 / 30 / 60 / 120 seconds (additional shortcuts queued — see §15.3) |
| Preview latency target | configured queue policy: leaky=2 / 200 ms / 4 buffers | < 200 ms measured |
| Replay seek target | not formally measured | < 500 ms |
| Audio mode | per-feed embedded (Phase 9.C dynamic) | per-feed embedded |
| Post-session export | H.264 / AAC MP4 via ffmpeg subprocess (Phase 8.B) | H.264 / AAC MP4 |
| Config format | TOML (subset; see §13) | TOML (full schema in §13) |
| Metadata DB | SQLite | SQLite |
| Hot index | in-memory | in-memory |
| Storage | NVMe SSD strongly preferred | NVMe SSD strongly preferred |

---

# 20. Non-Negotiable Design Rules

Do not violate these without explicitly updating this architecture document.

## Forbidden

- Production replay based on JPEG frame dumps.
- Production hot path pushing every frame through Python.
- MP4 as active recording/replay container.
- Frame count as primary replay timeline.
- Primary-feed-only replay assumptions.
- Unbounded queues.
- Silent recording frame drops.
- Export blocking live recording.
- Feed failure crashing entire session.
- Wall-clock time as media synchronization source.
- Hardcoded camera device order as feed identity.

## Required

- Timestamp-based replay.
- Segment-based storage.
- Explicit per-branch queue policies.
- Shared session timeline.
- Per-feed health state.
- Durable segment metadata.
- Crash recovery behavior.
- Operator-visible degraded state.
- Manual post-session MP4 processor that refuses active sessions.
- Configurable codec/container/hardware settings.
- Synthetic feed test mode.

---

# 21. What to Tell an AI Coding Assistant

Use this prompt at the start of each coding session:

```text
Read docs/r3_app_architecture.md. Treat it as authoritative.

Do not implement anything yet. First:
1. Summarize the relevant requirements from the document.
2. Inspect the current codebase.
3. Identify the gap for the current phase only.
4. Propose the smallest safe implementation plan.
5. List files you expect to modify.
6. Wait for approval.

Do not rewrite the whole app.
Do not use MP4 for active recording or replay source media.
Do not use JPEG frame dumps as production replay.
Do not push production video frames through Python hot paths.
Do not change unrelated UI behavior.
Do not skip metrics or validation.
Replay must be unavailable when recording is stopped.
Replay must use completed recording segments only.
Post-session MP4 processing must be a separate manually run program that targets a finalized session folder.
```

---

# 22. Final Architecture Position

The production system should be understood as:

```text
A timestamped, segment-based, multi-feed media system controlled by Python,
with GStreamer owning the real-time media graph, SQLite owning durable metadata,
an in-memory index owning hot replay lookup over completed recording segments, and play markers referencing timestamped segments for later post-session processing.
```

This architecture supports:

- live viewing
- long recording
- in-session replay while recording
- synchronized multi-angle replay
- slow motion
- clip marking
- post-session MP4 export
- crash recovery
- future expansion

without forcing the application to become a fragile Python-frame-processing system.

---

# 23. Immediate Next Action

The next engineering step is not to rewrite the app.

The next step is:

```text
Phase 0: Freeze current behavior.
Phase 1: Add observability.
Phase 2: Add explicit state machines.
Phase 3: Refactor one feed to the native GStreamer data path.
```

Do not start with codec conversion or replay rewrite until metrics and state are in place.

---

End of document.
