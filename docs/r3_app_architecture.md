# r3-app Production Architecture Design Document
## Multi-Feed Sports Recording, Instant Replay, and Clip Export System

**Document purpose:**  
This document defines the target production architecture for a Windows desktop multi-feed sports recording and replay application.

**Important:**  
This is the authoritative architecture document. Do not treat any unstated media, storage, timing, or replay behavior as an implementation detail. The decisions below are part of the design.

**Status:**  
This document describes the **target** production architecture. The current codebase does not yet conform to it — notably, today's replay storage uses JPEG thumbs plus short muxed segments, frames are pushed through Python on the hot path, and the active recording container is MP4. Those are explicitly forbidden by this document and are tracked as gaps to close in the phased plan in §18. For a description of the current code's object graph, see `ARCHITECTURE.md`.

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
kind = "ndi" # ndi | camera | test
source = "Camera 1"
enabled = true
role = "primary"

[[feeds]]
id = "feed_002"
name = "Angle 2"
kind = "ndi"
source = "Camera 2"
enabled = true
role = "secondary"
```

Config rules:

- Every feed must have a stable ID.
- Feed IDs must not depend on device order.
- Defaults must be explicit.
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

Goal:

- Replace scattered booleans/control flags with clear state.

Tasks:

- Add app state model.
- Add feed state model.
- Add recording state model.
- Add replay state model.
- Ensure UI reflects state.
- Ensure invalid state transitions are rejected/logged.

Do not:

- Rewrite pipelines yet.

Exit criteria:

- Start/stop recording transitions are explicit.
- Replay/live transitions are explicit.
- Feed disconnect can be represented cleanly.

### Slices

Phase 2 lands in four slices. Each is independently revertable; each commits separately. The work touches `app/core/`, `app/media/feed_runtime.py`, `app/storage/session_manager.py`, and the two UI widgets — no media-pipeline shape change.

**Naming note before starting:** the existing `app/core/app_state.py` dataclass `AppState` collides with the doc's top-level `AppState` enum (§10.1). Slice 2.A renames the dataclass to `UiState` (it really is per-output Qt UI state) so the enum can claim the `AppState` name in 2.D.

**2.A — State-machine framework + `FeedState`**

- New `app/core/state_machine.py` — small generic `StateMachine[E]` with: declared transitions table, current state, `transition_to(new)` that rejects illegal moves, emits a `health_event(category="invalid_transition")`, and returns the resulting state. No threading-model change beyond a single internal lock.
- `FeedState` enum from §10.2 (`DISABLED`, `CONNECTING`, `LIVE`, `DEGRADED`, `DISCONNECTED`, `RECONNECTING`, `FAILED`).
- One `StateMachine[FeedState]` per `FeedRuntime`. The existing `is_started` / `is_connected` booleans become read-throughs over the state machine.
- Bus-logger hook: `gst_bus_log` upgrades a sustained `WARNING` to a `DEGRADED` transition; an `ERROR` triggers `DISCONNECTED`. The 1.E `feed_lost` emission is rerouted so it flows from the state machine instead of the telemetry hub's zero-fps streak (the streak heuristic stays as a fallback for sources that never raise bus errors).
- Pulls in the Phase 1 deferred item — `qos` (dropped-buffer) bus messages — by adding them to the bus filter and counting them per feed; sustained drops drive `LIVE → DEGRADED`.
- Rename `app/core/app_state.py:AppState` → `UiState` in this slice, so the new top-level enum can take the name in 2.D without a noisy rename commit later.
- Tests: `StateMachine` helper, `FeedState` transition table, `qos`-driven degraded transition.

**2.B — `RecordingState` + `SessionState`**

- `RecordingState` enum from §10.3 (`NOT_RECORDING`, `STARTING_RECORDING`, `RECORDING`, `STOPPING_RECORDING`, `FINALIZING`, `RECORDING_ERROR`). One global `StateMachine[RecordingState]` owned by `RecordingManager`.
- `SessionState` enum from §10.6 (`CREATED`, `RECORDING`, `STOPPED`, `FINALIZED`, `DIRTY`, `ARCHIVED`). One global `StateMachine[SessionState]` owned by `SessionManager`.
- `ApplicationCoordinator.toggle_long_session_recording` and the per-feed start/stop drive `RecordingState`. `SessionManager.start_new_session` / `close()` drive `SessionState`.
- Persist current `SessionState` to `<session>/session.json` on every transition. The presence of a session whose persisted state is `RECORDING` or `STOPPED` (no `finalized_at`) is the marker §10.6 / §11.4 will use to detect `DIRTY` on next launch — but the recovery prompt UI itself is later phase work.
- Tests: both transition tables, file persistence, idempotency of `STOPPED → FINALIZED`.

**2.C — `ReplayState` (replaces/extends `PlaybackMode`)**

- `ReplayState` enum from §10.4 (`REPLAY_UNAVAILABLE_NOT_RECORDING`, `LIVE_WHILE_RECORDING`, `REPLAY_AVAILABLE`, `SEEKING`, `REPLAYING`, `PAUSED`, `SLOW_MOTION`, `JUMPING_TO_LIVE`, `REPLAY_DEGRADED`).
- One `StateMachine[ReplayState]` per `PlaybackController` (operator only — program is permanently `LIVE_WHILE_RECORDING` once recording starts, and undefined otherwise).
- Replace the controller's free-form `PlaybackMode` checks in `pause_playback`, `rewind_10_seconds`, `set_playback_rate`, `jump_to_live` with explicit transitions.
- **Behavior change worth flagging:** the doc requires replay actions to be rejected when `recording_state != RECORDING` (§10.4, §15.2). Current code allows rewind from a fresh session before the operator has started long recording. This slice enforces the doc's rule. Pre-Phase-4 the rolling replay buffer is still JPEG-backed and decoupled from recording, so the rejection is enforced by checking `RecordingState`, not by absence of segments. Worth socializing this with the volunteer-operator workflow before merging — the upside is the doc and code finally agree; the downside is a UX regression for the "review a play before formally pressing Record" case.
- Tests: replay-state transitions, recording-required guard, `LIVE → REPLAY_*` only when recording is `RECORDING`.

**2.D — Aggregate `AppState` + UI surfacing + invalid-transition logging**

- `AppState` enum from §10.1 (`STARTING`, `IDLE`, `PREVIEWING`, `RECORDING`, `REPLAYING`, `PAUSED`, `SLOW_MOTION`, `DEGRADED`, `ERROR`, `SHUTTING_DOWN`). Computed by `ApplicationCoordinator` from the four sub-states.
- Update `StatusBarWidget` to display `AppState` and `RecordingState`. Update `DiagnosticsWidget` to also show per-feed `FeedState` next to FPS, and current `ReplayState` for the operator controller.
- Audit every rejected transition: each one becomes a `health_event(severity=warning, category="invalid_transition", metadata={"from": ..., "to": ..., "machine": ...})`. The diagnostics widget surfaces a count of recent invalid transitions.
- Tests: aggregation logic for `AppState`, status-widget label rendering for each state, invalid-transition health event shape.

### Out of scope for Phase 2

- The §11.4 startup recovery prompt UI ("Resume / End and finalize / Discard"). 2.B persists enough state to *detect* `DIRTY` on next launch but does not act on it.
- The `ARCHIVED` transition. That belongs to the Phase 8 post-session processor, not the live app.
- Anything that touches the GStreamer pipeline shape, codec, container, or replay storage path. Phase 2 is a state-only refactor.

---

## Phase 3 – Native GStreamer Data Path for One Feed

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

---

## Phase 4 – Segmented Recording Store + Active Replay Index

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
