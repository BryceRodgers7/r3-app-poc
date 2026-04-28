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
- Create short MP4 files for each marked play and selected feed/angle.
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

Recommended structure:

```text
C:\SportsReplay\
  sessions\
    2026-04-26_18-30-00_GameName\
      session.json
      session.sqlite
      logs\
      metrics\
      recording\
        feed_001\
          segment_000001.mov
          segment_000002.mov
        feed_002\
          segment_000001.mov
          segment_000002.mov
      processed\
        game\
          feed_001_game.mp4
          feed_002_game.mp4
        play_0001\
          play_0001_angle1.mp4
          play_0001_angle2.mp4
      quarantine\
```

## 6.3 Segment metadata

Each segment must have metadata:

```text
segment_id
session_id
feed_id
segment_type: recording
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
first_keyframe_pts_ns
last_keyframe_pts_ns
frame_count_estimate
size_bytes
state: writing | complete | dirty | corrupt | quarantined
pts_to_session_offset_ns
created_at
finalized_at
```

Each segment carries its own `pts_to_session_offset_ns`. The feed clock is allowed to jump across a reconnect — a new segment is started with a fresh offset. The feed timeline is therefore not stored as a separate table; it is a runtime view computed from segments (see §14.1).

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

Rules:

- Replay is unavailable unless recording is active.
- Replay may use completed recording segments from the current active recording, from recording start through the latest replayable finalized segment.
- Currently-writing segments are excluded from replay until finalized.
- Never delete recording segments as part of replay navigation or instant-replay shortcut management.
- If disk pressure is severe, enter degraded mode and warn operator rather than evicting recording media.

## 6.7 Clip model

A clip/play should be metadata first, exported file second.

A play marker should store:

```text
play_id
session_id
start_session_time_ns
end_session_time_ns
feed_ids
primary_feed_id
tags
notes
created_at

source_segments_by_feed: { feed_id -> [segment_id, ...] }
```

The marker's truth is the time range. `source_segments_by_feed` is a per-feed cache of which segments cover that range, recorded at marker-creation time so the post-session processor can locate source media without re-querying. A play crossing a segment edge on one feed but not another is handled naturally — each feed's list is independent. Clip cuts at export time are timestamp-based seek + trim inside those segments, not segment-aligned.

Do not immediately create a physical video file for every play unless requested.

Reason:

- Clip creation should be instant.
- Export can happen later.
- Multiple export formats/angles may be generated from the same metadata.

## 6.8 Retention and cleanup

The live recording app **never** deletes anything on disk. Cleanup is a separate, manually invoked operation (a flag on the post-session processor or a dedicated CLI), never an automatic action during a session.

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
3. Handle missing segments independently.
4. Start playback in sync as closely as possible.
5. Keep feeds aligned during slow motion/pause/resume.
6. Surface degraded status if one feed cannot participate.

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
REPLAY_AVAILABLE
SEEKING
REPLAYING
PAUSED
SLOW_MOTION
JUMPING_TO_LIVE
REPLAY_DEGRADED
```

Rules:

- `REPLAY_UNAVAILABLE_NOT_RECORDING` is the replay state whenever recording is not active.
- `REPLAY_AVAILABLE` means completed recording segments exist in the current active recording and can satisfy at least part of a requested replay range.
- Replay requests must be rejected when `recording_state != RECORDING`.
- Replay availability is based on completed segments only, so the UI should expose the latest replayable session time.

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

```text
CREATED         # session folder + sqlite exist, no recording yet
RECORDING       # at least one feed actively recording
STOPPED         # operator pressed End Game; finalization in progress
FINALIZED       # all per-feed segments closed, manifest written, sqlite committed
DIRTY           # crashed or hard-killed mid-session; not yet recovered or discarded
ARCHIVED        # post-session processor has produced deliverables
```

Transitions:

```text
CREATED  → RECORDING            (operator starts game recording)
RECORDING → STOPPED             (operator ends game)
STOPPED  → FINALIZED            (last segment closed, manifest written) — automatic
RECORDING/STOPPED → DIRTY       (crash or hard-kill; detected on next launch)
DIRTY    → FINALIZED            (operator chose Resume → finalize completes)
DIRTY    → CREATED              (operator chose Discard, leaving an empty shell session)
FINALIZED → ARCHIVED            (post-session processor success)
```

Rules:

- The post-session processor refuses any session whose state is not `FINALIZED` or `ARCHIVED`.
- The operator UI must surface session state plainly. "Game ready for export" maps to `FINALIZED`/`ARCHIVED`; "Game in progress" maps to `RECORDING`/`STOPPED`. The volunteer operator must be able to tell at a glance whether it is safe to walk away.
- The transition `RECORDING/STOPPED → DIRTY` is detected by the absence of a `finalized_at` timestamp on the session row at next launch — there is no separate heartbeat needed.
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

- **Resume** (default highlighted): scan `recording/{feed_id}/` for incomplete segments, mark partial files `quarantined`, rebuild the in-memory index from completed segments only, continue with new segment numbering, transition the session back into `RECORDING`. Replay coverage during the resumed session uses the surviving completed segments; the gap caused by the crash shows up as missing media (§15.5).
- **End and finalize**: do not resume recording; close the session into `FINALIZED` so the post-session processor can run against whatever was successfully captured.
- **Discard**: transition the session to `CREATED` (empty shell), leaving its source segments on disk for retention rules (§6.8) to handle later.

Rules:

- Auto-resume without operator confirmation is forbidden — the cause of the crash (disk full, broken camera, OS update) often persists, and a silent retry loop is worse than a visible prompt.
- Partial segments are never repaired in-place. Quarantine and move on. Repair tooling, if any, is offline-only.
- The recovery prompt does not violate the rule "replay unavailable when not RECORDING" (§10.4); the prompt blocks the media UI entirely until a new session state is chosen.

---

# 12. Metrics, Logging, and Observability

## 12.1 Required per-feed metrics

Track:

```text
feed_id
source_fps
preview_fps
program_fps
recording_fps
dropped_buffers_preview
dropped_buffers_program
queue_depth_preview
queue_depth_recording
encode_latency_ms
segment_write_latency_ms
last_completed_segment_end_time
latest_replayable_session_time_ns
recording_segments_available_for_replay
feed_state
```

## 12.2 Required system metrics

Track:

```text
disk_write_mb_s
disk_free_gb
cpu_percent
memory_mb
gpu_encoder_usage_if_available
active_feeds
recording_state
replay_state

```

## 12.3 Required replay metrics

Track:

```text
replay_request_time
requested_start_session_time
requested_end_session_time
latest_replayable_session_time_ns
available_replay_duration_seconds
replay_index_lag_ms
feeds_available
feeds_missing
completed_segments_selected
replay_read_seek_latency_ms
time_to_first_frame_ms
slow_motion_factor
rejected_not_recording_count
```

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

Use explicit config rather than hardcoding.

Example TOML:

```toml
[app]
mode = "production" # development | production
base_data_dir = "C:\\SportsReplay"

[session]
default_name_prefix = "Game"
auto_create_session = true

[media]
pipeline_mode = "gstreamer_native" # gstreamer_native | python_test
hardware_acceleration = "auto" # auto | none | nvidia | intel | amd
default_width = 1920
default_height = 1080
default_fps = 30

[replay]
enabled = true
requires_recording = true
default_instant_replay_seconds = 60
quick_replay_seconds = [10, 30, 60, 120]
max_replay_scope = "current_recording"
completed_segments_only = true

[recording]
enabled = true
segment_duration_seconds = 4
codec = "prores" # prores | dnxhr | mjpeg
container = "mov" # mov | mxf | mkv | ts

[retention]
keep_source_segments_days = 14
keep_processed_exports_days = 90
keep_quarantine_days = 7

[post_processing]
enabled = true
mode = "manual_after_app_shutdown"
output_container = "mp4"
long_clip_codec = "h264"
short_clip_codec = "h264"
audio_codec = "aac"
refuse_active_session = true


[audio]
mode = "master" # none | master | per_feed
master_feed_id = "feed_001"
include_audio_in_replay = true
include_audio_in_recording = true
include_audio_in_exports = false

[preview]
max_latency_ms = 200
drop_when_late = true


[monitoring]
metrics_enabled = true
diagnostics_overlay = true
log_gstreamer_bus = true

[[feeds]]
id = "feed_001"
name = "Main Camera"
kind = "ndi" # ndi | synthetic
ndi_name = "HOSTNAME (Camera 1)"
enabled = true
role = "primary"

[[feeds]]
id = "feed_002"
name = "Angle 2"
kind = "ndi"
ndi_name = "HOSTNAME (Camera 2)"
enabled = true
role = "secondary"

# Dev-only synthetic fallback (no NDI hardware required). Comment out for prod.
# [[feeds]]
# id = "feed_dev"
# name = "Synthetic Test Pattern"
# kind = "synthetic"
# enabled = true
```

Config rules:

- Every feed must have a stable ID.
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
Feed
Segment
Recording
PlayMarker
ExportArtifact

HealthEvent
MetricSample
```

`FeedTimeline` is **not** a durable entity. It is a runtime-computed view of `(feed_id, [available_intervals])` derived from the `Segment` table on demand (see §8.7). Replay queries operate against `Segment` directly; there is no separate timeline table to keep in sync.

## 14.2 Session

Fields:

```text
id
name
created_at_utc
started_at_utc
ended_at_utc
base_path
state
config_snapshot_json
```

## 14.3 Feed

Fields:

```text
id
session_id
name
kind
source_identifier
role
state
first_seen_session_time_ns
last_seen_session_time_ns
```

## 14.4 Segment

Fields listed in section 6.3.

## 14.5 PlayMarker

Fields listed in section 6.7.

## 14.6 ExportArtifact

Fields:

```text
id
session_id
play_id
feed_ids
start_session_time_ns
end_session_time_ns
output_path
codec
container
state
error_message
created_at
started_at
completed_at
```

## 14.7 HealthEvent

Fields:

```text
id
session_id
feed_id nullable
severity
category
message
created_at
resolved_at
metadata_json
```

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

If a feed lacks completed recording media for the requested replay range:

- show blank/placeholder for that feed
- continue other feeds
- surface degraded replay status
- do not crash

## 15.6 Jump to live

Jump to live must:

- stop replay playback
- return operator view to live source
- keep recording unaffected

## 15.7 Replay seek granularity

Replay start and end points are **frame-accurate inside any completed segment**, not snapped to segment boundaries.

Reason:

- The active recording codecs mandated in §5.2 (ProRes, DNxHR, MJPEG) are intra-frame. Every frame is effectively a keyframe, so the decoder can begin output at any frame inside a completed segment.
- The only segment-boundary constraint is **finalization**: the currently-writing segment is not safely readable until it is closed. Replay coverage is therefore `[recording_start, end_of_latest_finalized_segment]`, not `[recording_start, now]`.
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
8. Feed disconnect during recording.
9. Feed reconnect during recording.
10. Disk slowdown simulation.
11. Disk full simulation.
12. App crash during segment write.
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
24. Post-session processor creates short play MP4 files from play metadata after shutdown.

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

### Doc fix queued from Phase 2

§10.4 lists `REPLAY_AVAILABLE` and `LIVE_WHILE_RECORDING` as separate states. In implementation 2.C folded them — `REPLAY_AVAILABLE` describes the storage's readiness, not the operator's view, and giving it a separate operator-state slot was redundant. §10.4 should be tightened to describe `REPLAY_AVAILABLE` as a property of the replay store rather than as a state of the operator's playback session. Tracking, not blocking.

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

**Status: 🟡 In progress, partially blocked.** 3.A.1 and 3.A.2 are complete; 3.A.3 was attempted and reverted (see slice notes); 3.B and 3.C are **deferred until after Phase 4** for the reasons described in "Blocker discovered during 3.A.3 testing" below.

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

**3.A.3 — Native preview video sink** *(⚠️ Attempted, reverted — needs another approach)*

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

**3.B — Explicit per-branch queue policies + queue-depth metrics**

- Codify the §4.2 policies in code, not in defaults: every branch after a `tee` gets a `queue` element with explicit `max-size-buffers`, `max-size-bytes`, `max-size-time`, and `leaky` properties. Preview branch is leaky-downstream and time-bounded (target < 200 ms per §4.3); recording branch is non-leaky and uses pressure to drive `RecordingState.RECORDING_ERROR` rather than silently drop (§4.4).
- Add `queue_depth_preview` and `queue_depth_recording` per-feed metrics from §12.1. Sample via GStreamer `current-level-buffers` on the queue element; expose in `FeedMetricsSnapshot` and the diagnostics widget.
- Hub-driven health: sustained queue saturation on the recording branch (e.g. >75% for >2s) drives `RecordingState.RECORDING → RECORDING_ERROR` and emits a `recording_branch_saturated` health event. Preview saturation drives `FeedState.LIVE → DEGRADED` (it is the natural meaning of "preview can't keep up with frames the source is producing").
- Applies to both `python_push` and `native` modes uniformly so the queue contract is independent of the source-side change in 3.A.
- Tests: queue policy applied as configured (inspect element properties); saturation health-event emission threshold; `FeedState` transitions on sustained preview drops.

**3.C — Validation + transitional banner + Phase-3 readout**

- Add a startup log line and a diagnostics-widget banner per feed: `pipeline=native` (clean) or `pipeline=python_push (transitional)`. The banner surfaces the §17.3 guardrail visibly so refactor work doesn't accidentally regress feeds back to Python push.
- Smoke-test exit criteria: a manual checklist embedded in the diagnostics widget when `mode = "production"` (from §13 config) and any feed reports `python_push`. This is just a visible warning; nothing is enforced beyond a log line.
- Document the Phase 3 verdict in `ARCHITECTURE.md`: which feeds are native, which still aren't, and what's blocking the rest. Confirm 3.A.3 has lifted the preview-path Python ceiling and that `target_frame_*` defaults can be raised to 1080p without freezing the operator UI.
- Tests: banner rendering for each pipeline mode; warning emitted only in production mode.

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

**§11.4 recovery prompt UI remains deferred.** This slice records the data that prompt will need; the actual "Resume / End and finalize / Discard" dialog is its own slice.

**Exit criteria for 4.E:** an unclean shutdown leaves `session.json` in `dirty` state on disk; corrupt segments end up in `<session>/quarantine/<feed_id>/`; in-progress (no DB row) segments that survived the crash get a `dirty` SQLite row so they're addressable by future tooling; the next launch sees the dirty state without crashing.

### Out of scope for Phase 4 (deferred)

- **Audio in segments (slice 4.F).** Was attempted inline during 4.A as "4.A.bis" — wiring `audio_tee → queue → valve → audioconvert → audioresample → opusenc → splitmuxsink (audio_%u request pad)`. On the test hardware (Windows + NDI Tools Screen Capture + gst-plugins-good splitmuxsink) the change broke the video pipeline (live preview froze on a stale frame, no recording files produced). Reverted; promoted from inline follow-up to a properly-scoped 4.F slice. Likely root cause directions worth investigating: splitmuxsink's behavior when audio data starts flowing after video data (timestamp alignment); whether `audio_%u` request pad needs to be requested *before* the video pad is implicitly bound; and whether the live audio path's wasapisink and the new audio_record path conflict on the audio tee. Recommended workaround until 4.F: leave audio recording disabled in 4.A; if audio is needed for a play, handle it separately at the post-session processor (Phase 8).
- **ProRes / DNxHR codec selection.** §5.2's first-choice codecs require `gst-plugins-bad` elements that aren't always available on UCRT64. Add a config-driven codec selector once MJPEG path is solid; this becomes a small Phase-4-bis or a Phase 11 task (hardware acceleration ties into encoder choice).
- **§11.4 recovery prompt UI.** 4.E marks `DIRTY` sessions correctly so the prompt has data to react to, but the prompt itself ("Resume / End and finalize / Discard") is its own slice.
- **Phase 3.B and 3.C resumption.** Once Phase 4 lands and recording works end-to-end, queue policies (3.B) and the transitional banner (3.C) become meaningful again.
- **Native preview video sink (3.A.3 retry).** Same — revisit after Phase 4 reduces concurrent unknowns.

---

## Phase 5 – Shared Session Timeline

Goal:

- Introduce true timestamp-based multi-feed replay.

Tasks:

- Implement `SessionClock`.
- Implement `FeedTimeline`.
- Map feed PTS to session time.
- Update replay requests to use session timestamps.
- Handle feed startup offsets.
- Handle missing feed ranges.

Exit criteria:

- Replay request is based on session time.
- Multiple feeds can be queried for same replay window.
- Missing feed media does not crash replay.

---

## Phase 6 – Multi-Feed Replay Controller

Goal:

- Make replay multi-feed and synchronized.

Tasks:

- Update PlaybackController to control multiple feeds.
- Support pause, resume, slow motion, jump to live.
- Keep program output live-only.
- Keep recording active during replay.
- Add degraded replay indicators.

Exit criteria:

- Operator can replay multiple feeds for same time range.
- Program output remains live.
- Recording continues during replay.
- Missing feed does not break replay.

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

---

## Phase 8 – Post-Session MP4 Processor

Goal:

- Add the separate manual processor that creates long-form and short-play MP4 deliverables after the recording app is shut down.

Tasks:

- Add a separate program in the same repository.
- Accept a session folder as input.
- Refuse to run if the session is active, recording, dirty, or not finalized.
- Read `session.sqlite`, recording segments, and play metadata.
- Produce one long-form MP4 per feed/game recording.
- Produce short MP4 files for each play and selected feed/angle.
- Write `ExportArtifact` metadata for successful and failed outputs.
- Preserve source recording segments unchanged.

Exit criteria:

- Processor can be run manually after shutdown against a finalized session folder.
- Long-form MP4 outputs are created.
- Short play MP4 outputs are created from play metadata.
- Failed exports are recorded without damaging source media.

---

## Phase 9 – Audio Integration

Goal:

- Add or harden audio according to the master-audio strategy.

Tasks:

- Configure master audio source.
- Map audio timestamps to session time.
- Include audio in recording.
- Include audio in replay if enabled.
- Include audio in export.
- Handle missing audio gracefully.

Exit criteria:

- Master audio records correctly.
- Audio remains aligned during replay.
- Missing audio does not break video replay.

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
- Tune queue sizes.
- Tune segment duration.
- Tune encoder settings.
- Validate 2-feed, 4-feed, and stretch targets.
- Record performance profiles.

Exit criteria:

- Primary hardware target passes acceptance test.
- CPU/GPU/disk metrics are within safe limits.
- No unbounded memory growth.

---

# 19. Recommended Implementation Defaults

Use these defaults unless hardware testing proves otherwise:

```text
recording/replay-source codec: ProRes or DNxHR
recording/replay-source fallback codec: MJPEG
recording/replay-source container: MOV or MKV
recording segment duration: 4 seconds
in-session replay scope: recording start through latest completed segment while recording is active
instant replay shortcuts: 10, 30, 60, and 120 seconds
preview latency target: < 200 ms
replay seek target: < 500 ms
audio mode: one master feed
post-session export: H.264/AAC MP4
config format: TOML
metadata DB: SQLite
hot index: in-memory
storage: NVMe SSD strongly preferred
```

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
