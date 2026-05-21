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
- Own referee and operator playback controllers (operator is live-only; referee has full transport).
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

> **Currently shipped:** MJPEG-in-MKV only. Phase 11.B trialed ProRes and DNxHR in MOV containers (encoder factory wired, settings matrix accepting them, codec-aware caps for the videoconvert→encoder edge) but couldn't reliably mux audio into qtmux at record time without hardware-class capture infrastructure. The §5.2 ranking is broadcast-archive bias — broadcasters prefer those formats for *deliverables*, not for live record-and-replay paths. For archive deliverables, use the post-session processor to transcode from the MJPEG-MKV master. The encoder factory's ProRes/DNxHR rows + disk-budget coefficients remain in the codebase for future re-investigation (e.g. if hardware capture is added) but `[recording] codec` rejects anything but `mjpeg` at config-load.

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

> **Currently shipped:** MKV (matroskamux) only on the live recording path. MOV/qtmux is the broadcast-archive natural pairing for ProRes/DNxHR but those codecs are off the live path (see §5.2 note above). The post-session processor handles MOV transcode for archive deliverables.

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

### 6.2.2 Per-game recording contract

The per-game folder layout in §6.2.1 is the *file system* shape; the rest of the system relies on a stronger **data-quality contract** that the recording side must uphold:

1. **Recording is gated by the operator's button presses.** Between "Stop game recording" and the next "Start game recording", the live preview path stays alive (operators must keep watching the feed), but **no audio or video frames are written to disk**. The session is in `STOPPED` (§10.6); `recording/` gains no new bytes until the next Start press.
2. **Each game's media is self-contained from frame zero.** The first MKV segment in `game_N/<feed_id>/segment_00000.mkv` opens with the first audio/video frame captured *after* that game's Start press. No data from game N−1, and no data from the inter-game STOPPED interval, may appear in game N's files. In particular: audio and video PTS within game N's segments must align (within normal sub-frame priming offsets — typically tens of milliseconds; see §C of [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md)).
3. **Post-session output starts at 0:00.** Because (2) holds, the post-session processor concatenating game N's segments produces a `<game>.mp4` whose timeline starts at 0:00 representing that game's first captured frame. The processor does not need to re-time, trim, or normalize the timeline beyond what `-avoid_negative_ts make_zero` already does at MP4 mux time. Operators see "0:00" at the start of every game's deliverable, regardless of how many games preceded it within the session.

This contract is what makes the per-game folder layout useful: the deliverable for game N is a function of `game_N/`'s contents alone, not of the wider session. Any cross-game leakage — stale encoder packets, queued audio buffers from the STOPPED interval, video chain priming delays after the splitmuxsink rebuild — is a contract violation and a recording-side bug, not a post-processing problem.

Known violations and their fixes are catalogued in [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md) §C. When a new violation is found, the order of operations is: reproduce on a real session, identify the leak source on the recording side, fix it there, then add an invariant entry. Patching the post-processor to mask the symptom is a temporary workaround at best — it doesn't fix in-session replay (which reads the same segment files) and it blocks any future tooling that reads `game_N/` directly.

#### Note: source segment `start_time` is in pipeline running-time, not per-game time

The contract above is about *content fidelity*, not timestamp values. The PTS that matroskamux writes into each segment file is the GStreamer pipeline's running-time at first buffer — and the running-clock is set once at pipeline start (app launch / preview start) and ticks monotonically across every Stop/Start cycle within a session. A source segment file therefore has `start_time` ≈ "seconds since the app launched," not "seconds since this game's Start press."

Concretely: in `session_179`, game 1 seg 0 has `start_time=8.0s`, game 2 seg 0 has `start_time=71.467s`, game 3 seg 0 has `start_time=115.066s`. **This is normal**, not a bug. Resetting the clock per game would break the live preview path that shares the pipeline. The non-zero start_time is invisible to every consumer that matters:

- The **post-session processor** normalizes to 0:00 via ffmpeg's `-avoid_negative_ts make_zero` (and the `-ss` audio-leak trim for already-recorded sessions that predate the §6.2.2 fix).
- **Replay** queries by `session_time_ns`, which is mapped from PTS via the per-segment `pts_to_session_offset_ns` column (§8.3). Each segment carries its own offset, so PTS values don't need to be globally consistent.
- **`SegmentIndex`** stores raw PTS, but every public query takes session-time inputs.

If you open a source segment directly in VLC and see "0:01:11" at frame 1 instead of "0:00", that's the running-time semantic surfacing. The post-processed `<game>.mp4` (the actual deliverable) is unaffected.

**Manual test for replay correctness on game N>1's first play.** The §7.H.4 "Replay Play" transport seeks to the currently-open play's `start_session_time_ns`, which on game N's first play maps back into seg 0 of `game_N/`. With seg 0 carrying a non-zero `start_time` in PTS terms, this is the test case that exercises the per-segment `pts_to_session_offset_ns` math end-to-end. Recommended sanity check whenever the recording-side timestamp logic is touched: record a 2-game session, press "Replay Play" on game 2's Play #1, confirm playback starts at game 2's actual first frame (not somewhere in game 1, not 71 seconds of black, not the wrong PTS clamping).

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

- **`operator_fps` / split `dropped_buffers_referee` vs `dropped_buffers_operator`** — the referee and operator windows render through `MultiFeedOutputRenderer` instances that share the upstream tee. Today the snapshot collapses both into one `preview_fps` / `dropped_per_sec`. Splitting requires per-window-sink instrumentation; deferred until a use case appears (e.g. one window stutters while the other doesn't).
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
referee_window_title  = "Sports Replay Referee"
operator_window_title = "Sports Replay Operator"

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

# Phase 11.C — queue policy per branch. Preview is leaky-downstream
# (drop oldest), recording is non-leaky (disk pressure surfaces as
# RECORDING_ERROR). The recording max_size_time_ms auto-derives from
# `recording_segment_duration_seconds` unless overridden.
[media.queue_policy.preview]
leaky            = 2                  # GstQueueLeaky: 0=none, 1=upstream, 2=downstream
max_buffers      = 4
max_size_time_ms = 200
[media.queue_policy.recording]
leaky            = 0
max_buffers      = 256
# max_size_time_ms = 4000             # omitted → auto-derived from segment duration

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
codec                    = "mjpeg"   # mjpeg only on the live path (see §5.2)
container                = "mkv"     # mkv only on the live path (see §5.3)

# Phase 11.C — per-codec encoder property overrides. Defaults match
# what `app/media/encoder_factory.py` applies; override only what you
# need to tune. Only `mjpeg` is on the live path today; the prores/
# dnxhr entries remain in the encoder factory for future reuse and
# the validators accept them harmlessly.
[recording.encoder_settings.mjpeg]
quality = 85                          # jpegenc/qsvjpegenc, range 1-100
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
- **1.E — Health events + diagnostics widget.** New `app/core/health_events.py` with append-only JSONL persistence under `<session>/logs/health_events.jsonl`. Hub auto-emits `feed_lost` after 3 consecutive zero-source-fps samples and `disk_low` below 5% free, with paired `feed_recovered` / `disk_recovered` on recovery. New `app/ui/diagnostics_widget.py` mounted on the referee window only today; future work will move most of it to the operator window (see Phase 13 notes), keeping only replay-feature-relevant diagnostics on the referee window.

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

**Original failure mode:** the first attempt (pre-Phase 4) hit a "third Direct3D11 renderer" top-level window that kept appearing despite a working `prepare-window-handle` bus handler, and the preview tee fan-out froze within seconds. The most likely root cause was the legacy replay pipeline's own `d3d11videosink` (a third sink we weren't binding), which Phase 4.D removed entirely along with the rolling-replay buffer. With only referee + operator sinks remaining, the binding race shrinks to a tractable set.

**Retry shape (committed):**

- `PipelineManager._add_preview_branch` dispatches based on `source.pipeline_mode`: NATIVE → `_add_native_preview_branch` (the d3d11 path), `python_push` → `_add_python_push_preview_branch` (the legacy appsink → QImage path). Synthetic dev source remains on the python_push path because it has no native GStreamer source to feed d3d11 anyway.
- Per-window queues in `_add_native_preview_branch` are now `leaky=2 (downstream)` + `max-size-time=200ms` + `max-size-buffers=4` so a blocked window (minimized, occluded, dead d3d11 device) drops frames rather than back-pressuring the source tee. This is the single most likely fix for the "tee fan-out froze within seconds" symptom.
- `VideoWidget` got a live↔replay flip: in native render mode, `set_video_surface_visible(enabled, live=...)` picks `_live_surface` for LIVE and the `_frame_label` QLabel for REPLAY/PAUSED. `display_frame()` also auto-flips to QLabel because receiving a Python frame in native mode means the playback controller is showing a non-live timestamp via `SegmentDecoder`. `MultiFeedVideoPanel.apply_tile_visibility(mode, ...)` derives the `live` flag from `mode == LIVE` and passes it through.
- `MainWindow` binds the d3d11 sinks for native feeds before `coordinator.initialize()` runs. The role (referee vs operator) is derived from `live_only_window`. The bind goes through `coordinator.bind_native_preview_window_handle` which checks `is_native_preview_active(feed_id)` so python_push feeds and the `force_python_push_preview` escape hatch are both honored.
- `AppSettings.force_python_push_preview` (default `False`) is the operator-level escape hatch — flip to `true` in `[app]` if d3d11 still misbehaves on the local hardware. The qimage path keeps preview Python-bound (~720p ceiling) but is proven stable.

**10 new tests** in `tests/test_native_preview.py`: settings parsing (default / explicit / missing), VideoWidget render-mode flip matrix (qimage default, native live → live_surface, native replay → frame_label, display_frame in native mode flips to qimage, return-to-live flips back, qimage mode ignores live flag, SOURCE_LOST always shows placeholder).

**Outstanding follow-up:** if the tee freeze symptom returns on hardware, the next direction the doc previously suggested still applies — investigate gst-plugins-bad d3d11videosink version-specific behavior, or try gst-plugins-rs `d3d12sink` as a drop-in.

The intended design: replace the referee-window preview path's `appsink → numpy → QImage` round-trip with a native GStreamer video sink (`d3d11videosink` on Windows) that renders directly into each VideoWidget's native window handle. Two sinks per feed (referee + operator), bound via `GstVideoOverlay.set_window_handle()` to the per-window `WId()` returned by Qt. A buffer pad probe on `videoconvert.src` would tick metrics and synthesize `FrameOverlayInfo` so the referee window's `PlaybackController` state machine still sees frame-arrival events. No pixel data into Python on the preview hot path.

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

### State of the world entering Phase 10

A scan of the §11.2 failure table against current code:

| Failure scenario | Today |
|---|---|
| Feed disconnect | Live-bus `ERROR` drives `FeedState → DISCONNECTED` (`pipeline_manager.py`); telemetry zero-fps streak does the same as a fallback. ✅ |
| Feed reconnect | `RECONNECTING` is a defined state but has no producer that *re-attempts* the source build. Hop into `RECONNECTING → LIVE` only fires if data spontaneously starts flowing again. **Gap.** |
| Disk full | Phase 10.C: pre-flight check at Start (`evaluate_disk_preflight`) refuses when free space < `disk_full_grace_seconds × estimated MB/s`; live ENOSPC on the bus drives `RecordingState → RECORDING_ERROR`; new `disk_critical` (ERROR) tier joins existing `disk_low` (WARNING). ✅ |
| Slow disk | Phase 10.D: `disk_slow` (WARNING) fires when `DiskSampler.write_mb_s_estimate ≥ disk_budget_mb_s` for 3 consecutive ticks; banner picks it up. `FeedMetricsSnapshot.record_queue_saturation_pct` exposes queue depth as a percentage; diagnostics widget shows write rate vs budget with ✓/⚠/✗ glyph. ✅ |
| Corrupt segment | Phase 10.E: `SegmentValidatorWorker` runs the same `cv2`-based validator as startup recovery on each just-finalized segment, on a daemon thread; on invalid → file moves to `<session>/quarantine/<feed_id>/`, DB row → `quarantined`, evicted from `SegmentIndex`, `segment_quarantined_runtime` (WARNING) for the banner. Plus the existing startup quarantine. ✅ |
| Startup recovery | Phase 7.D shipped (`IMPLEMENTATION.md` §7). ✅ — Phase 10 inherits it; the exit-criterion bullet "App can recover usable session data after crash" is already met. |
| Graceful shutdown | Phase 10.F: `coordinator.shutdown()` runs the same `disable_file_recording` `split-now` ritual the operator's Stop press uses when a game is in flight, drives `RecordingState` through `STOPPING_RECORDING → FINALIZING → NOT_RECORDING`, then proceeds to teardown. `_shutting_down` flag short-circuits `toggle_long_session_recording`, `mark_next_play`, and `PlaybackController` transport methods. ✅ |
| Health event UI | `DiagnosticsWidget` shows category counts only. The §11.3 list of operator-visible warnings has no foreground surface. **Gap.** |

Phase 10 closes the **Gap** rows and upgrades the **Partial** rows to operator-visible signals. Slice 10.A lands first because every later slice raises a health event that needs to be foregrounded.

### Slices

**10.A — Operator alert banner (✅ shipped)**

The §11.3 operator-visible warnings list (feed disconnected / recording degraded / replay unavailable / disk nearly full / disk too slow / dropped frames high / encoder failure / session not safely recording) was previously buried in `health_events.jsonl` and reduced to a category-count line in the diagnostics widget. 10.A adds a foregrounded `AlertBanner` widget at the top of the operator (live-only) window — that's where the recording transport (Start/Stop, Next Play) lives, so the persistent operator who can act on a recording-error is the one who sees the banner. (Pre-Phase-13.C the banner was on the referee window; the move was made because the referee is occasional-use and could miss the alert.)

- **`HealthEventLog.open_events()`** — additive accessor that returns the full `HealthEvent` payloads for currently-open `(feed_id, category)` pairs, alongside the existing `has_open_event` / `clear_open_event` markers. The original `_last_categories` dict is preserved so existing readers (e.g. `test_telemetry.py`) keep working — a parallel `_open_events` dict mirrors the markers with full payloads.
- **`AlertBanner` widget (`app/ui/alert_banner.py`)** — polls `default_log().open_events()` on a 1s `QTimer` (matching `DiagnosticsWidget` cadence), filters to a §11.3 allowlist, picks the highest-severity event (ERROR > WARNING > INFO; most-recent id tie-break), and renders it as a colored bar (amber for WARNING, red for ERROR). When more than one operator-visible event is open at once, an inline `+N more` badge surfaces the overflow.
- **Allowlist mapping** — `feed_lost → "Feed disconnected"`, `feed_degraded → "Recording degraded"`, `replay_degraded → "Replay unavailable"`, `disk_low → "Disk nearly full"`, `recording_branch_saturated → "Disk too slow — recording degraded"`, `recording_error → "Encoder failure"`, `session_dirty → "Session not safely recording"`. Diagnostic-only categories (`audio_missing`, `invalid_transition`, `feed_recovered`, `recording_started`, `recording_stopped`, `disk_recovered`, `session_finalized`) are deliberately excluded — they stay in the diagnostics widget and JSONL log.
- **Wiring** — banner is inserted at the top of `MainWindow`'s central layout when `controls_role == "operator"` (the live-only window with the recording transport). The referee window (`controls_role == "referee"`) has no banner — operators don't watch the referee window continuously. Health events still fire into the JSONL log regardless of which window has the visual surface.
- **Tests** — `tests/test_alert_banner.py`: empty log → hidden; single WARNING → visible + label; ERROR outranks concurrent WARNING; `+N more` populates correctly; recovery clears the banner; diagnostic-only categories are excluded from both the primary slot and the count.

**Out of scope for 10.A:** click-to-dismiss (the recovery event is the dismissal); per-feed routing of feed-specific banners to a per-feed widget; operator-visible "dropped frames high" (no producer category exists yet — would need a new telemetry rule that emits one).

**10.B — Per-feed reconnect supervisor**

Today, after `FeedState → DISCONNECTED` the live ingest pipeline is dead until the operator restarts the app. The state machine permits `DISCONNECTED → RECONNECTING → LIVE`, but the only producer of the `RECONNECTING` hop is `_promote_feed_state_on_arrival` in `feed_runtime.py:111`, which fires only after a buffer arrives — and a buffer can't arrive because nothing is rebuilding the source. 10.B closes the loop:

- **`ReconnectSupervisor`** — one per feed, owned by `FeedRuntime`. On entering `DISCONNECTED`, schedules a rebuild of the source-side ingest pipeline on backoff (1s, 2s, 4s, 8s, capped at 30s). Each attempt drives `DISCONNECTED → RECONNECTING` *before* the rebuild call (so the banner from 10.A picks up the in-flight reconnect attempt) and back to `DISCONNECTED → RECONNECTING` cycles between attempts.
- **Permanent-fail floor** — after `MAX_RECONNECT_ATTEMPTS` (default 10, ≈ ~5 minutes of backoff total), transitions `RECONNECTING → FAILED` and emits a new `feed_failed_permanent` health event. The operator must Stop/Start the session to retry; this avoids a silent retry loop on a genuinely-broken cable / NDI sender.
- **Successful rebuild** — once a buffer lands, the existing `_promote_feed_state_on_arrival` already drives `RECONNECTING → LIVE` and emits `feed_recovered`. No new code path needed on the success side.
- **Recording branch caveat** — live and recording branches share one per-feed pipeline today, so the rebuild necessarily tears the recording branch down too. Other feeds are unaffected (each feed has its own pipeline), but the disconnected feed's recording does **not** auto-resume after reconnect: `pipeline_manager.stop_all()` closes the record valve and never reopens it, and the only producer of `enable_file_recording` is the operator's app-wide Stop/Start. Worse, `stop_all` skips the `disable_file_recording` `split-now` ritual, so the segment in flight at disconnect lacks an EBML trailer and is quarantined on next-launch recovery (or by 10.E once mid-session validation lands). Closing this gap is slice 10.B.2 below.
- **Tests** — `ReconnectSupervisor` unit tests against a fake source factory: backoff schedule produces the expected attempt times; cap at `MAX_RECONNECT_ATTEMPTS` transitions to `FAILED`; a successful rebuild during the schedule cancels remaining attempts; `feed_failed_permanent` health event fires exactly once on cap.

**Out of scope for 10.B:** mid-game seamless splice — when the source returns, post-reconnect frames carry a new PTS origin and the segment timeline has a gap. We don't backfill, and the per-game folder model means a Stop/Start is the natural seam if the operator wants a clean restart.

**10.B.2 — Auto-resume recording on rebuilt feed (deferred)**

After 10.B, a brief NDI flicker on one feed leaves that feed's live preview self-healing but its recording silent until the operator presses Stop/Start (which restarts recording for **all** feeds in a fresh `game_NNN/` folder). 10.B.2 closes the recording-side gap without splitting live and recording into independent pipelines:

- Before `stop_all`, if `RecordingState == RECORDING`, run the same `disable_file_recording` `split-now` ritual the operator's Stop press uses, so the pre-disconnect segment lands with an EBML trailer instead of being quarantined.
- After `connect_source` succeeds and the pipeline is back in PLAYING, if `RecordingState == RECORDING` AND the feed had `_recording_session_paths` set before the rebuild, re-call `enable_file_recording` for that feed only — same session paths, same `game_subdir`, `start_fragment_index = find_next_fragment_index(...)` so filenames don't collide with the pre-disconnect tail. The disconnect's gap shows up in replay as one missing segment for that feed; other feeds keep their continuous timeline.
- Operator-facing impact: after a 5–10s NDI blip, all feeds are recording again automatically, in the same game folder, with one expected gap on the affected feed. No Stop/Start required.

**Out of scope for 10.B.2:** mid-stream timeline splicing (the gap is real and visible in replay — that's §15.5's territory); preserving the audio chain's encoder state across the rebuild (audio re-link already runs on splitmuxsink rebuilds via Phase 7.G).

**10.C — Disk-full enforcement**

`disk_low` warns; nothing actually stops a recording when the filesystem is genuinely full or when the next `splitmuxsink` mux-start would `ENOSPC`. 10.C makes the response explicit:

- **Pre-flight at Start** — `toggle_long_session_recording` Start checks `shutil.disk_usage(recording_dir).free` against an estimate of N seconds of recording (default 60s, configurable as `disk_full_grace_seconds` under `[recording]`), using the Phase 7.A budget coefficients. If the free space wouldn't cover the grace window, refuse to Start, emit a `disk_full_blocked` health event, and let the 10.A banner surface it. The Start button stays in `Start game recording` mode. The check is wrapped in `evaluate_disk_preflight` (`app/core/disk_budget.py`) so it's testable without real disks.
- **Live `ENOSPC` interpretation** — `pipeline_manager._poll_bus_for_messages` recognises `Gst.ResourceError.NO_SPACE_LEFT` (with a substring fallback on the parsed error message for binding-version skew) as a disk-full condition. Drive `RecordingState → RECORDING_ERROR` via a new `set_recording_state` setter on `PipelineManager` (not `FeedState → DISCONNECTED` — the source is fine; the disk is the problem); preview keeps running, the operator gets a `disk_full_during_record` health event.
- **Two-tier disk-low** — the existing `disk_low` threshold (5% free) keeps WARNING semantics. New `disk_critical` (ERROR) fires at 2% free OR free bytes < 1 GB — the byte floor catches very large disks where 2% is still many GB but recording will hit ENOSPC soon regardless of percentage. `disk_critical` raises the banner to red; the `disk_low` marker stays open while `disk_critical` is also open, so the existing low-tier surface doesn't disappear.
- **Banner allowlist additions** — `disk_full_blocked`, `disk_full_during_record`, `disk_critical` all join the `_OPERATOR_VISIBLE_CATEGORIES` map in `alert_banner.py` so 10.A surfaces them.
- **Tests** — pre-flight refusal with a fake `disk_usage_fn`, including the byte-floor-on-huge-disk corner case; ENOSPC classification via both the GLib quark/code path and the message-substring fallback; two-tier emission and recovery; coordinator integration; settings round-trip from TOML.

**Known limitation — no hysteresis.** If free space oscillates around a threshold (sustained background process consuming and freeing 100MB), `disk_low` / `disk_critical` will flap: fire, clear, fire again. The current implementation matches the existing `disk_low` shape; adding hysteresis would slot in here. Not worth the complexity until field experience shows flapping is a real problem.

**Out of scope for 10.C:** auto-cleanup of old sessions to free space (retention policy is §6.8, deferred). 10.C only refuses cleanly; doesn't try to make room.

**10.D — Slow-disk runtime surface**

Slice 3.B already drives `FeedState → DEGRADED` and `RecordingState → RECORDING_ERROR` on sustained record-queue saturation. 10.D closes the operator-visibility loop and grounds the signal in the Phase 7.A budget:

- **`disk_slow` health event** — fires when `DiskSampler.write_mb_s_estimate ≥ disk_budget_mb_s` for `DISK_SLOW_STREAK_THRESHOLD = 3` consecutive disk-sample ticks (≈15s at the default 5s disk-tick interval), via `_evaluate_disk_throughput` on `TelemetryHub`. WARNING severity; clears as soon as one sample is back at-or-below budget. Distinct from `recording_branch_saturated` so the JSONL log can tell "the disk is too slow" apart from "this feed's recording queue is full" — they often co-occur because slow disk causes saturation, but the JSONL distinction matters for post-game forensics.
- **`TelemetryHub` constructor** — gains a `disk_budget_mb_s: float | None` kwarg, plumbed from `settings.disk_budget_mb_s` in the coordinator factory. None disables the rule (test fixtures that never set it).
- **`FeedMetricsSnapshot` properties** — `record_queue_saturation_pct` and `preview_queue_saturation_pct` derive percentages from the existing depth/capacity fields (clamped to [0, 100]). No new sampling — the data is there, just not surfaced.
- **DiagnosticsWidget** — per-feed line appends `qrec 12/24 (50%)` so the operator sees saturation as a percentage alongside the depth/capacity gauge. Disk line appends a ✓/⚠/✗ glyph next to the write rate (≥ budget → ✗, ≥ 80% → ⚠, else ✓). Health-counts row gets a `disk_slow` count.
- **AlertBanner allowlist** — `disk_slow` joins with the label "Disk write rate over budget".
- **Tests** (`tests/test_disk_slow.py`, 20 tests) — emission threshold matrix (under-budget no-emit, single-tick no-emit, three-tick emit, single emission while open); streak resets on under-budget sample; recovery clears event; unavailable snapshot skipped; budget-unset / budget-zero disable the rule; queue-saturation percentage clamps and zero-capacity edge case; banner allowlist; widget glyph thresholds.

**Out of scope for 10.D:** dynamic re-encode at lower bitrate (operator's option is to reduce feed count or accept the degradation); per-feed disk-rate attribution (the `DiskSampler` reads volume-wide free-space deltas, not per-process — fine for "is the disk too slow?" but doesn't tell you which feed is the heaviest writer).

**10.E — Mid-session corrupt-segment quarantine**

Before 10.E, `SegmentValidator` ran only on startup recovery (`session_recovery.validate_session_segments`). A segment that finalized mid-game with a corrupt header — rare but possible after a hard disk hiccup that survived the recording branch — would land in `SegmentIndex` and crash the next replay attempt against `cv2 / GStreamer`. 10.E makes the runtime path tolerant:

- **`SegmentValidatorWorker`** (`app/core/segment_validator_worker.py`) — one per `PipelineManager` (i.e. per feed), owns a `queue.SimpleQueue` and a daemon worker thread spawned lazily on first `submit(...)`. Reuses the existing `_default_segment_validator` (cv2-based) and `_quarantine_file` from `session_recovery.py`, plus the existing `MetadataDb.update_segment_state` / `update_segment_file_path` and `SegmentIndex.remove_by_path` operations.
- **Hook in `_finalize_pending_segment_locked`** — after the `complete` row is inserted and the segment added to the index, the worker is handed a `_ValidationTask(segment, quarantine_dir, metadata_db, segment_index)`. The task runs off-thread, so finalize stays cheap on the splitmuxsink streaming path. `getattr` fallback on `_segment_validator_worker` keeps the older `__new__`-bypassing test stubs working without per-test setup additions.
- **Quarantine action** — on invalid (or validator exception): move file to `<session>/quarantine/<feed_id>/` (collision-safe via the existing `.recovered_NNN.` suffix), update DB row → `state="quarantined"` with the new `file_path`, call `SegmentIndex.remove_by_path` so future replay queries skip it, emit `segment_quarantined_runtime` (WARNING) carrying the file basename and reason ("invalid" or "file_missing"). The 10.A banner picks it up via the allowlist.
- **Per-feed FIFO serialisation** — each feed has its own worker. Within a feed, validations run strictly in order (FIFO). Across feeds, validations are independent. Prevents a slow disk hiccup from queuing dozens of concurrent validations that would compete with the recording branch for I/O.
- **Lifecycle** — `pipeline_manager.stop_all()` shuts down the existing worker (5s drain budget) and constructs a fresh one for the next start, so the 10.B reconnect path doesn't leave a dead worker around.
- **Sync entry point** — `worker.run_one(segment, ...)` is the same code path the daemon thread runs, exposed for tests that want deterministic timing without `Event.wait` races.
- **Tests** (`tests/test_segment_validator_worker.py`, 13 tests) — `SyncQuarantineTests` (8) cover end-to-end through real MetadataDb + SegmentIndex + filesystem: valid segment is no-op; invalid segment moves file + updates DB state + path + evicts from index; missing-file edge case marks DB without a move; validator exception falls through to invalid; severity is WARNING; no-DB / no-index defensive path. `AsyncWorkerTests` (4) exercise the daemon thread: `submit` runs validator off-thread; FIFO serialisation enforced via barrier-controlled validators; shutdown is idempotent; submit-after-shutdown is a no-op. `AlertBannerWiringTests` (1) confirms allowlist.

**Out of scope for 10.E:** validating *every* segment on a long session (validating only the just-finalized one is enough; the older tail was validated when finalized and isn't being modified); cross-feed coordination of quarantine (each feed is independent per §2.5); auto-recovery of the `segment_quarantined_runtime` open marker (the latest quarantine event displaces the prior; the marker stays open until a future feature adds a "dismiss" action — operator's response is investigation, not banner clearing).

**10.F — Graceful shutdown drain**

Before 10.F, `aboutToQuit → coordinator.shutdown()` was synchronous: it stopped feed runtimes immediately, leaving `splitmuxsink` to write whatever it could during process teardown. The active segment's tail got truncated, no matroskamux trailer landed, and startup-recovery on the next launch flagged a needless dirty session for what was meant to be a clean exit. 10.F fixes the close path:

- **`coordinator._drain_active_recording_locked()`** — runs after the controllers are torn down (timers stopped) but before `runtime.stop()`. When `is_any_recording()`, drives `RecordingState` through `STOPPING_RECORDING → FINALIZING → NOT_RECORDING` while calling `pipeline_manager.disable_file_recording()` on every feed. This reuses the operator's existing Stop ritual (split-now → 300ms sleep → valve close → finalize) so each feed's in-flight segment lands with a proper trailer and a matching DB row.
- **Per-feed exception isolation** — if one feed's `disable_file_recording` raises (e.g. wedged splitmuxsink), the exception is logged and the loop continues with the remaining feeds. The state-machine arc still completes. Shutdown is best-effort by design — a single broken feed doesn't deadlock the close path.
- **Session-state arc parity** — the active session manifest is driven from `RECORDING → STOPPED` before `SessionManager.close()` runs `STOPPED → FINALIZED`, matching the manifest arc the operator's Stop press would produce. (The transitions table allows `RECORDING → FINALIZED` directly, but going through STOPPED keeps the manifest's recorded state changes consistent across operator-Stop and shutdown-Stop paths.)
- **Play-boundary closure + replay-scope reset** — `play_manager.stop_game(now)` closes any currently-open play with the right end-time, and `replay_store.set_current_game_start_session_time(None)` drops the per-game scope. Operator's Stop already does both; shutdown matches it.
- **`_shutting_down` flag is the transport gate** — set first thing in `shutdown()`. `toggle_long_session_recording`, `mark_next_play`, and `PlaybackController.{pause_playback, rewind_10_seconds, replay_current_play, jump_to_live, set_playback_rate}` all early-return when set. Uses `getattr(self, "_shutting_down", False)` so older `__new__`-bypassing test stubs don't crash.
- **`PlaybackController.shutdown()` sets the per-controller flag first** (before stopping timers / closing decoders) so a transport call already in flight sees the flag flip the moment teardown starts.
- **Hard-kill remains untouched** — a SIGKILL or power loss during shutdown still falls through to the §11.4 dirty-session recovery path on next launch. 10.F only improves the case where the OS asked the app to quit cleanly.
- **Tests** (`tests/test_shutdown_drain.py`, 15 tests) — `_ShutdownDrainTests` (7) cover drain when recording, skip when not recording, play-manager stop-game call, per-feed exception isolation (one feed raising doesn't block the others), session-manifest STOPPED transition, replay-scope reset, and that `_shutting_down` is set before any teardown side-effects observed. `CoordinatorTransportGateTests` (2) and `PlaybackControllerTransportGateTests` (5) lock in the no-op behavior for both layers. `PlaybackControllerShutdownSetsFlagTests` (1) confirms the controller flag flips before timers stop.

**Out of scope for 10.F:** per-feed timeout budget on the disable-file-recording call (the existing 300ms split-now sleep inside `disable_file_recording` is the only blocking path, and the spec's mention of a 2000ms format-location-full wait turned out to be unnecessary — the existing ritual already returns within that envelope); auto-invocation of the post-session processor (Phase 8 is operator-driven); recovery from a hard-kill *during* shutdown (§11.4 dirty-session path covers it).

### Phase 10 sequencing notes

- **10.A first.** Every later slice raises a health event; without the foreground banner the new emissions are invisible to the operator.
- **10.B and 10.C are independent** and both address §11.1 scenarios. Recommend 10.B before 10.C — disconnect/reconnect is the more frequent real-world failure (NDI drops, network blip) and the higher-value resilience win.
- **10.D depends on the Phase 7.A disk budget** (already shipped) and on 10.A's banner. Lands after 10.C so the operator has both disk-full and disk-slow signals in place at once.
- **10.E is independent** of 10.A–10.D for behavior, but only useful in operator-visible terms after 10.A is in place — the `segment_quarantined_runtime` event needs the banner to be noticed in real time.
- **10.F should land last** among the in-scope slices. It changes the shutdown ordering, and it's the easiest slice to mask bugs in earlier slices (a wedged shutdown can hide a partial reconnect failure).
- **Startup recovery is already shipped** via Phase 7.D — explicitly call this out so a future reader doesn't try to plan a "10.G — startup recovery" slice. The Phase 10 exit criterion "App can recover usable session data after crash" is already met by the existing recovery dialog + Phase 7.D continuation logic.

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

### State of the world entering Phase 11

| Task | Today |
|---|---|
| Hardware acceleration selection | `[media] hardware_acceleration` selects from `auto / none / nvidia / intel / amd`; `app/media/encoder_factory.py` probes `Gst.ElementFactory.find()` and emits a per-feed INFO log + `pipeline_mode`-style `recording_encoder` field on `FeedMetricsSnapshot`. **Closed in 11.A.** |
| Software fallback | Build-time link-failure path swaps in `jpegenc` when the chosen hwaccel encoder refuses the upstream caps; `_force_software_encoder` is sticky for the rest of the process. Bus-error path catches the runtime variant. **Closed in 11.A.** |
| ProRes / DNxHR codec support | `[recording] codec` accepts `prores` and `dnxhr`; `[recording] container` accepts `mov`; codec/container compatibility matrix validated at config-load. `avenc_prores_ks` / `avenc_prores` / `avenc_dnxhd` wired via the encoder factory; `qtmux` swaps in for `matroskamux` when the container is `mov`; segment naming + recovery regex pick up the new extension; `_CODEC_RATIO_VS_RAW_RGB` covers all three codecs. **Closed in 11.B.** |
| Queue sizes tunable | `[media.queue_policy.preview]` and `[media.queue_policy.recording]` accept `leaky / max_buffers / max_size_time_ms`. Defaults reproduce the pre-11.C hardcoded behavior. Recording's `max_size_time_ms` auto-derives from segment duration when omitted. **Closed in 11.C.** |
| Segment duration tunable | `recording_segment_duration_seconds` (existing) is now gated by `validate_segment_duration(codec, seconds)` in `app/media/encoder_factory.py` — no-op for the current intra-frame codecs, but the seam catches non-positive values and gates future long-GOP additions. **Closed in 11.C.** |
| Encoder settings tunable | `[recording.encoder_settings.{mjpeg|prores|dnxhr}]` exposes `quality` (mjpeg) and `profile` (prores/dnxhr). Profile names map to the integer enum values exposed by gst-libav. **Closed in 11.C.** |
| Acceptance harness | `tools/perf_acceptance.py` drives the coordinator headlessly against synthetic feeds, polls `TelemetryHub.snapshot()` at 1Hz, and applies §16.3 pass/fail rules at finalization. **Closed in 11.D.** |
| Performance profiles | `<base_data_dir>/perf_profiles/<hostname>/<utc_iso>_<feeds>x<resolution>.json` captures per-feed FPS p50/p95/min, queue-saturation peaks, disk write-rate p95, and the run's health events. Newest 50 retained per host. **Closed in 11.D.** |

Phase 11 closes these by way of four slices:

### Slices

**11.A — Encoder factory + hwaccel detection — ✅ committed.**

The hardcoded `jpegenc` element in `_build_pipeline` became one branch of an explicit factory. The factory probes element availability at GStreamer init, picks an encoder based on `[media] hardware_acceleration`, and falls back to software when the hwaccel element is missing or fails to negotiate caps.

- **`[media] hardware_acceleration`** — TOML key matching §7.4: `"auto" | "none" | "nvidia" | "intel" | "amd"`. Default `"auto"`. Persisted on `AppSettings`.
- **`app/media/encoder_factory.py`** — `select_encoder(hwaccel, codec, gst_module, encoder_settings, force_software)` returning an `EncoderSelection` (element name, factory args, fallback flag, reason text). Probes `Gst.ElementFactory.find()` per the priority table for the chosen codec.
- **Live MJPEG selection table** — `auto`/`intel`: `qsvjpegenc` → `jpegenc`. `nvidia`/`amd`: `jpegenc` (no NVENC/AMF MJPEG element on Windows). `none`: `jpegenc` always. The factory still has wired entries for ProRes/DNxHR for future reuse, but the `[recording] codec` validator narrows the live path to MJPEG (see 11.B note below).
- **Startup logging** — one INFO line per feed at pipeline-build time: `recording encoder feed=cam_a codec=mjpeg element=qsvjpegenc reason=Intel hwaccel; hardware_acceleration=auto`. Surfaced as `recording_encoder` on `FeedMetricsSnapshot` so the diagnostics widget shows it next to the existing `native / py-push` indicator.
- **Software fallback events** — when `hardware_acceleration != "none"` and the requested element is unavailable, emit a `hwaccel_fallback` INFO health event with metadata identifying which element was missing.
- **Build-time negotiation handling** — when the hwaccel element is found but the link from the upstream caps filter to it fails (driver-specific format quirk), the build path swaps in the software fallback in-place and emits a `hwaccel_negotiation_failed` WARNING. `_force_software_encoder` latches sticky-True so subsequent in-process rebuilds also pin to software.
- **Bus-error fast-path** — when caps negotiation fails at PLAYING transition (different from the build-time link failure), the bus-error handler at `pipeline_manager.py` distinguishes encoder-side faults from source-side faults and triggers an in-process pipeline rebuild via `FeedRuntime.request_encoder_software_fallback()` rather than routing through `FeedState.DISCONNECTED` and the 10.B reconnect supervisor (the source is fine, only the encoder needs replacement).
- **Tests** — `tests/test_encoder_factory.py` (selection matrix + reason strings + force-software override); settings round-trip in `tests/test_app_settings.py`.

**Out of scope for 11.A:** AMD VCN encoder paths; decoder hwaccel for replay; per-feed hwaccel override.

**11.B — ProRes / DNxHR codec support — partially landed; live-path codec matrix narrowed back to MJPEG-only.**

The full slice was implemented (encoder factory rows, codec/container compatibility matrix, qtmux muxer wiring, codec-aware caps for the videoconvert→encoder edge, disk-budget coefficients, profile-name → integer mapping). It was then reverted to MJPEG-only on the live path after extensive field testing surfaced an irreducible muxer-timing problem.

**The qtmux finding (the practical reason ProRes/DNxHR-on-MOV doesn't work in this pipeline):**

`qtmux` (the GStreamer MOV muxer that splitmuxsink wraps for `.mov` segments) refuses `audio_%u` request pads after `STREAM_START` flows through its video pad. In our pipeline, the operator clicks Start *after* the source has been delivering buffers (and thus events) for several seconds — by then qtmux has already received STREAM_START on its video pad and locked its pad set. Phase 9.C's "wire audio when first buffer arrives" pattern works fine for `matroskamux` (permissive about late pads) but produces `Not providing request pad after stream start` warnings + audio-chain build failures with qtmux. Game 1 records video-only; on game 2's rebuild a fresh qtmux instance accepts the audio pad, but the audio + video interleaving has been observed to wedge the pipeline within seconds in some configurations.

**Things that were tried and didn't work:**

- **Hold splitmuxsink in READY state via `set_state(Gst.State.READY)`** — parent state machine auto-syncs children to PLAYING regardless; the READY state didn't stick.
- **Use `gst_element_set_locked_state(true)` to keep splitmuxsink in NULL** — broke sticky-event propagation; downstream couldn't respond to upstream caps queries; recording branch backpressured immediately.
- **Don't add splitmuxsink to the pipeline at all until enable_file_recording** — left the encoder's `src` pad dangling, which similarly broke caps negotiation; queue stalled.
- **`gst_pad_add_probe` with `GST_PAD_PROBE_TYPE_BLOCK_DOWNSTREAM` on encoder.src** — same root cause: the probe blocks events that the pipeline's state propagation needs.

The pattern: anything that keeps STREAM_START from reaching qtmux during pipeline preroll also breaks the rest of the pipeline. There is no clean way to selectively block STREAM_START.

**How real instant-replay systems handle this** (and the architectural lesson):

Pro broadcast systems (EVS, Tedial, Evertz) decouple audio and video at record time — video to a file/DPX-sequence, audio to a separate WAV/PCM stream — and combine them only at edit/export time. They also use hardware capture cards with on-board ProRes/DNxHR ASICs and dedicated record-only pipelines. **The MOV/qtmux ecosystem was designed around that hardware model, not around general-purpose software-encoded pipelines fed by NDI.**

For our use case (operator-driven instant replay during a game + full-length recording for re-watch), the broadcast-archive properties of ProRes/DNxHR (4:2:2 chroma, 10-bit precision, NLE compatibility) don't carry over: instant-replay scrubbing only requires intra-frame coding (MJPEG qualifies), color quality at 4:2:0 8-bit is fine for sports replay, and "broadcasters expect ProRes" is a *deliverable* property addressed by the post-session processor's transcode step.

**What shipped:**

- The encoder factory's ProRes/DNxHR rows (`avenc_prores_ks` → `avenc_prores`, `avenc_dnxhd`), profile-name → integer mapping (`_PRORES_PROFILE_INT`, `_DNXHR_PROFILE_INT`), and per-codec encode caps (`I422_10LE` for ProRes, `Y42B` for DNxHR) all live in `app/media/encoder_factory.py` and `app/media/pipeline_manager.py`. They're well-tested but unreachable from the live config path; they're retained for reuse by a future post-session-processor internal pipeline or a future hardware-capture rebuild.
- Disk-budget coefficients for `prores` (0.25) and `dnxhr` (0.30) remain in `_CODEC_RATIO_VS_RAW_RGB` for the same reason.
- The audio-chain cleanup helper (`_discard_audio_record_chain_elements`) and the permanent-disable latch (`_audio_record_permanently_disabled`) are general-purpose defensive code — they protect against any future audio-build failure and are kept.

**What was reverted:**

- The `[recording] codec` matrix narrows back to `{"mjpeg"}`; `[recording] container` to `{"mkv"}`. `_validate_codec` and `_validate_container` reject `prores` / `dnxhr` / `mov` at config-load with explicit messages pointing at the post-session processor for archive deliverables.
- §13.1 / §5.2 / §5.3 / §19 reflect MJPEG-as-live, ProRes/DNxHR-as-export.

**Out of scope (now permanently — until the architectural model changes):** ProRes/DNxHR/MOV as live recording targets. The post-session processor is the canonical path to those formats.

**11.C — Pipeline tuning matrix**

Today's queue policy and encoder settings are baked into `pipeline_manager.py` constants. 11.C exposes them as `[media]` / `[recording]` tunables with documented defaults, and validates the chosen values against codec/segment-duration constraints.

- **`[media] queue_policy`** — new sub-table with `preview = { leaky = 2, max_buffers = 4, max_size_time_ms = 200 }` and `recording = { ... }`. Defaults match today's hardcoded values so an unspecified config is byte-identical to today's behavior.
- **`[recording] encoder_settings`** — codec-specific keys: `mjpeg.quality` (1–100, default 85), `prores.profile` ("proxy" | "lt" | "standard" | "hq", default "lt"), `dnxhr.profile` ("lb" | "sq" | "hq", default "lb"). The encoder-factory from 11.A applies them at element construction.
- **Segment-duration / keyframe consistency check** — at startup, refuse a `recording_segment_duration_seconds` shorter than the chosen codec's GOP. MJPEG (intra-frame) doesn't constrain it; ProRes / DNxHR (also intra-frame) don't either, but the validator is the seam where future GOP-bearing codecs would slot in.
- **Smoke benchmarks** — synthetic-feed harness exercises the pipeline at each tuning combination. Doesn't validate output quality; just confirms no crash, no queue saturation > 75%, no segment-finalization gap > segment_duration. Used as a regression gate for 11.C and 11.D.
- **Tests** — settings round-trip; default values match shipped behavior; out-of-range values rejected at load time; pipeline construction with each combination of queue policy + encoder settings.

**Out of scope for 11.C:** dynamic re-tuning at runtime (settings are immutable per app run, per Phase 7.A); per-feed encoder settings (single setting per codec, applied to every feed).

**11.D — Acceptance harness + performance profiles — ✅ committed.**

`tools/perf_acceptance.py` is an in-process harness: rather than subprocessing `python main.py`, it constructs the coordinator graph through `build_default_application_coordinator` and drives it under a `QCoreApplication` event loop (no widgets). The synthetic source supplies frames; the recording branch and `splitmuxsink` are exercised exactly as in production. Sampling is via `TelemetryHub.snapshot()` at 1Hz; health events are read from the run's `health_events.jsonl` at finalization.

- **`tools/perf_acceptance.py`** — CLI: `--feeds`, `--resolution WIDTHxHEIGHT`, `--fps`, `--duration`, `--smoke` (one-shot 1-feed/30s), `--config` (baseline `app_settings.toml`), `--data-dir` (override `base_data_dir`; defaults to `<cwd>/perf_acceptance_data` so harness sessions don't mingle with operator data), `--profile-dir`, `--hostname`, `--retention`. Programmatic feeds populate `feeds_table_rows` so `FeedRegistry.build_default` produces N synthetic feeds.
- **Profile artifact format** — `<base_data_dir>/perf_profiles/<hostname>/<utc_iso>_<feed_count>xWxH.json` (colons replaced with `-` for Windows). Schema (`tools/perf_acceptance.Profile.to_dict`): `schema_version`, `hostname`, `utc_iso`, `feed_count`, `resolution`, `target_fps`, `duration_seconds`, `warmup_seconds`, `disk_budget_mb_s`, `disk_write_rate_p95_mb_s`, `feeds[]` (per-feed: `pipeline_mode`, `recording_encoder`, `sample_count`, `source_fps_{p50,p95,min}`, `recording_fps_{p50,p95,min}`, `dropped_per_sec_max`, `queue_saturation_{preview,recording}_max_pct`), `health_events[]` (raw JSONL rows), `passed`, `failures[]`. `enforce_retention()` keeps the newest 50 per host directory.
- **Pass / fail rules** (§16.3, all enforced by `evaluate_pass_fail`): source `_fps_p50` within 1% of `target_fps`; recording `_fps_p50` within 1% of source `_fps_p50`; preview and recording queue-saturation peaks ≤ 75%; no `recording_branch_saturated` / `disk_full` / `disk_full_imminent` events. Exit code 0 / 1 mirrors the verdict for CI use.
- **Sample-window gating** — `warmup_seconds` (default 3.0) skipped at the head of every run; `recording_fps` only counted from samples taken after `toggle_long_session_recording()` returned (plus warmup) so build-time silence on the recording branch doesn't poison the verdict.
- **Synthetic-source caveats** — synthetic feeds run on `python_push`, so the harness measures pipeline plumbing (encoder branch, queue saturation, splitmuxsink finalization, disk throughput), not real NDI ingest performance. The `target_frame_*` 720p@30 ceiling applies — runs at 1080p with synthetic are expected to surface saturation. `recording_audio_enabled` and `enable_embedded_audio` are forced off because the synthetic source has no audio stream and `splitmuxsink` would stall waiting on it.
- **Diagnostics widget link** — `app/ui/diagnostics_widget.py` adds a `_perf_profile_label` row that reads the newest artifact under `<base_data_dir>/perf_profiles/<hostname>/`, parses `passed`, and renders `perf profile: passed ✓` / `failed ✗` with the `feed_count`, `resolution`, `utc_iso`, and filename. The row is hidden when no profile exists, so rigs that have never run the harness don't get a placeholder.
- **Smoke vs. full** — `--smoke` overrides `--feeds 1 --duration 30` for a fail-fast sanity check before a longer run.
- **Tests** — `tests/test_perf_acceptance.py`: percentile semantics; `compute_feed_stats` aggregation; `Profile`/`FeedStats` round-trip via `json.dumps` → `from_dict`; every §16.3 rule individually triggers a failure (source FPS, recording FPS, both queue-saturation paths, both health-event categories); `enforce_retention` cap behavior including 0/negative/missing-dir edges; `find_latest_profile` ordering; CLI argparse smoke flag and resolution validation.

**Out of scope for 11.D:** on-rig CI runners (the harness produces artifacts; orchestration is the operator's responsibility); cross-rig comparison tooling (single-rig regression detection only); replay-side acceptance (covered by §16.2 functional tests already); CPU / GPU utilization sampling (no `gpu_encoder_usage_if_available` integration yet — §12.2 deferral, would be a separate slice).

### Phase 11 sequencing notes

- **11.A first.** Both 11.B (codec wiring) and 11.C (encoder settings) reach into the encoder construction site; landing the factory seam first means each later slice extends rather than replaces it.
- **11.B turned out to be a partial-rollback slice.** The implementation work (encoder factory rows, codec/container matrix, qtmux wiring, profile mapping, disk-budget coefficients) shipped, but extensive field testing showed that `qtmux` refuses late audio request pads in our software-encoded NDI pipeline and there's no clean way to work around it. The live-path codec matrix narrowed back to MJPEG-only. The architectural lesson — that pro broadcast systems decouple audio from video at record time and use hardware capture — informs the §5.2 framing change: ProRes/DNxHR are *export* formats produced by the post-session processor, not live recording targets. See the 11.B writeup above.
- **11.C was partially shipped.** Queue policy + encoder settings + segment-duration validator landed cleanly. The smoke-benchmark sub-item was deferred to 11.D (where it slots into the same harness infrastructure). The encoder_settings keys for `prores` / `dnxhr` are still parsed and validated even though they don't reach the live pipeline — they remain useful for post-session-processor configuration if that work eventually lands.
- **11.D shipped as an in-process driver, not a subprocess.** The original spec text said "drives `python main.py`," but the GUI is going to be replaced anyway and adding a `--perf-acceptance` mode to `main.py` would have leaked benchmark concerns into the production entry point. Instead, the harness builds the coordinator graph itself under `QCoreApplication` and exercises the same `build_default_application_coordinator` seam production uses. Pass/fail is measured against the encoder selection landed by 11.A and the queue/encoder settings landed by 11.C.
- **The §5.2 ranking has been updated** based on 11.B's findings — ProRes/DNxHR are *export* targets, not live targets, in this codebase. The §19 defaults table reflects this.
- **No Phase 10 dependency.** Phase 10 is operator-experience hardening; Phase 11 is performance scaling. They can interleave if priority demands it.

---

## Phase 12 – Frame-by-frame replay stepping

Goal:

- Operator-driven single-frame and multi-frame stepping (forward + backward) for closer review of fast plays, sitting alongside (not replacing) the existing rate-based slow-motion (`Slow 1/2x`, `Slow 1/4x`).

Tasks:

- Add a step-frames primitive on `PlaybackController` that nudges the replay clock by a configurable number of frames in either direction, transitions the replay FSM into PAUSED, and re-renders.
- Expose two operator buttons (Step ◀, Step ▶) wired to the primitive.
- Make the per-click frame count configurable via `[replay] frame_step_count`.

Exit criteria:

- Operator can step forward and backward by the configured frame count from any `ACTIVE_REPLAY_STATES` member without leaking back into LIVE.
- Stepping past the live edge or before the earliest replayable session-time clamps cleanly (no decoder errors, no FSM whiplash) and surfaces an operator-visible "held at edge" status.
- All existing replay tests still pass; new tests cover clamping, FSM bouncing, and multi-feed coordination.

### State of the world entering Phase 12

| Task | Today |
|---|---|
| Frame-step forward | No surface. `set_playback_rate(0.5)` / `0.25` advance smoothly at fractional rate; there is no single-frame jog. |
| Frame-step backward | No surface. `rewind_10_seconds` is the closest, in 10s quanta. |
| Configurable step size | No. Rewind is a hardcoded 10s constant (`_REWIND_10S_NS`). |
| Live-edge / start-of-recording clamping | `_resolve_rewind_target_locked` already clamps via `_replay_store.available_session_time_range()`. The same helper extends to step. |
| `ReplayState` FSM support | PAUSED supports `SEEKING → PAUSED` and `REPLAYING/SLOW_MOTION → PAUSED`; no new state required. |
| Cross-segment stepping | `SegmentDecoder` + `RecordingSegmentReplayStore.nearest_frame_location` already resolve arbitrary session-time across segment boundaries. |
| Frame-period source | `AppSettings.target_fps` is the single global frame rate today. Per-feed fps drift is a separate (deferred) problem. |

Phase 12 closes these by way of two slices:

### Slices

**12.A — Core step primitive + tests — ✅ committed.**

`PlaybackController.step_frames(frame_delta: int)` — positive forward, negative backward, magnitude is the frame count. Constructor gains `frame_period_ns: int` so the controller is the single source of truth for "how many ns is one frame" within its run. The UI computes the value from `settings.target_fps` at coordinator-build time.

- **Behavior matrix** —
  - `live_only` controllers, `replay_state is None`, `_replay_actions_allowed() == False`, or `_shutting_down`: no-op with the existing operator-status emission pattern.
  - From `LIVE_WHILE_RECORDING`: snap to `latest_replayable_session_time`, bounce `LIVE_WHILE_RECORDING → SEEKING → PAUSED`, freeze, then apply `frame_delta`. Mirrors how `set_playback_rate(0.0)` enters replay from live.
  - From `REPLAYING` / `SLOW_MOTION`: bounce `→ SEEKING → PAUSED`, freeze, then apply.
  - From `PAUSED`: stay in PAUSED, apply `frame_delta` directly.
  - From `REPLAY_DEGRADED`: rejected with status "Replay degraded; step unavailable" (matches the existing degradation gating).
- **Step math** — `target_session_time_ns = current_session_time_ns + frame_delta * frame_period_ns`. Then clamp to `[earliest_replayable, latest_replayable]` via the same range helper Rewind uses. Update `_playback_session_time_ns`, set `_playback_rate = 0.0`, stop the replay clock, render via `_render_at_session_time_ns`.
- **Status messages** — `"Step +N frames"`, `"Step -N frames"`, `"Step held at live edge"` when forward step clamps to `latest_replayable`, `"Step held at start of recording"` when backward step clamps to `earliest_replayable`.
- **Multi-feed** — single shared replay clock, so one step call advances every tile via the existing `_render_at_session_time_ns` per-feed iteration. Feeds without coverage at the target time fall back to `nearest_frame_location` exactly as they do for Rewind.
- **Tests** — `tests/test_playback_controller.py` adds:
  - Forward step from PAUSED advances `_playback_session_time_ns` by exactly `frame_delta * frame_period_ns`.
  - Backward step from PAUSED retreats by the same amount.
  - Step from REPLAYING transitions through SEEKING → PAUSED before applying (verifying `replay_state.state` and `_playback_rate == 0.0`).
  - Step from LIVE_WHILE_RECORDING bounces through SEEKING → PAUSED and snaps to `latest_replayable` before the delta is applied.
  - Step beyond `latest_replayable` clamps; emits "held at live edge" status.
  - Step before `earliest_replayable` clamps; emits "held at start of recording" status.
  - Step with `_recording_manager.recording_state.state != RECORDING` is rejected (consistent with `_replay_actions_allowed`).
  - Step on a `live_only` controller is rejected with "This output is locked to live."
  - `frame_delta == 0` is a no-op (defensive; not a useful UI gesture).

**Out of scope for 12.A:** any UI surface, settings parsing, per-feed `frame_period_ns`, and runtime-adjustable step size.

**12.B — UI buttons + config wiring — ✅ committed.**

- **`[replay]` block** — new top-level section in `app_settings.toml`, parsing path added to `AppSettings.load` matching the `[recording]` / `[media]` patterns. `_validate_replay` rejects non-int and `< 1` values up-front so the misconfiguration surfaces at config-load instead of `step_frames` runtime.
  - **`[replay] frame_step_count`** — positive int, default `1`. Validated at config-load (`int >= 1`).
  - `app_settings.toml.example` gets a `[replay]` example with the new key documented.
- **`AppSettings.replay_frame_step_count: int = 1`** — new dataclass field; settings round-trip in `tests/test_app_settings.py`.
- **`controls_widget.py`** — two new `QPushButton`s, "Step ◀" and "Step ▶", with `step_back_requested` / `step_forward_requested` signals. Disabled outside RECORDING via the same `set_recording_state` toggle that already gates Replay Play and Next Play. Layout placement: between Slow 1/4x and Jump to Live, mirroring how the existing transport row reads left-to-right from "freshest content" (Pause) to "live edge" (Jump to Live).
- **`MainWindow.__init__` wiring** —
  - `step_back_requested` → `controller.step_frames(-settings.replay_frame_step_count)`
  - `step_forward_requested` → `controller.step_frames(+settings.replay_frame_step_count)`
- **Coordinator wiring** — `build_default_application_coordinator` computes `frame_period_ns = max(1, int(round(1_000_000_000 / settings.target_fps)))` and passes it into both `PlaybackController` constructors. The `max(1, ...)` floor protects against a misconfigured `target_fps <= 0` from sliding into a divide-by-zero or runaway step.
- **Tests** —
  - `tests/test_app_settings.py` covers the `[replay]` block round-trip + rejection of `frame_step_count <= 0` and non-int values.
  - `tests/test_controls_widget.py` (or co-located with the Phase 7.H.2 button tests if those live elsewhere) confirms the new buttons are gated on `set_recording_state` and emit the expected signals.

**Out of scope for 12.B:** runtime-adjustable step size (a +/- control on the operator UI). Could be a `12.C` later if the config-only knob proves too coarse in operator use.

### Phase 12 sequencing notes

- **12.A first.** The clamping + FSM-bounce semantics are subtle (which states bounce through SEEKING, what happens at REPLAY_DEGRADED, what message surfaces at the edges) and benefit from being landed under tests before any UI exposes the primitive. 12.B is mechanical glue once 12.A is solid.
- **No new `ReplayState`.** PAUSED already supports the bounce-to-SEEKING-and-back pattern via `_REPLAY_TRANSITIONS`. A `FRAME_STEP` state would just duplicate PAUSED with no behavioral difference, so we reuse PAUSED.
- **Single global frame-period is intentional.** `target_fps` is a single global setting today; per-feed fps drift is a separate problem the Phase 11.D harness can surface later. If drift becomes load-bearing, a per-feed override is a natural follow-on slice (12.C-bis) without needing to revisit the 12.A primitive.
- **Default `frame_step_count = 1`** matches the operator's stated preference for one-frame-per-click as the baseline; the config knob lets a sport with faster motion (puck, racquet, bat-on-ball) be turned up to 2 / 3 / 5 frames/click without recompile.
- **No Phase 11 dependency.** Phase 11 is performance scaling; Phase 12 is operator transport. They can ship in either order; Phase 12 reuses the encoder/decoder pipeline as-is.

---

## Phase 13 – Split the transport rows across the referee and operator windows

Naming context (see §6.2.2 / `CLAUDE.md` overview): the **referee window** hosts replay/review tools and is used occasionally by a referee. The **operator window** hosts the live-only feed; an operator sits in front of it for the whole session, pressing Start/Stop and Next Play.

Goal:

- Move `Start/Stop game recording` and `Next Play` from the referee window's `ControlsWidget` (where they live today, since the referee window is the only one with controls) to the operator (live-only) window — closer to the persistent operator who is actually using them.
- Replay transport (`Pause`, `Rewind 10s`, `Replay Play`, `Slow 1/2x`, `Slow 1/4x`, `Step ◀`, `Step ▶`, `Jump to Live`) stays on the referee window — those are the post-action review tools and they belong next to the replay-capable feed.

Rationale:

- Recording transport (start/stop game, mark plays) is a continuous-attention task tied to the live timeline. The persistent operator at the live-only window benefits from having those buttons under the same window they're already watching.
- Replay transport is an occasional-review workflow. It belongs alongside the referee's replay-capable feed.
- Today both are in one row on the referee window, which conflates two different roles into one button strip — and asks the operator to look across to the referee window to start/stop a game.

Reverses one design decision documented today: the §10.A AlertBanner note says the operator window (`show_controls=False`, live-only pane) "stays uncluttered." Phase 13 explicitly trades that cleanliness for ergonomic alignment between recording-transport and live-feed visibility — including the AlertBanner itself, which moves with the recording transport (see 13.C below). Whoever is pressing Start/Stop and Next Play is also the person who needs to see recording-error alerts; splitting transport from alerts across two windows would defeat the point of the move.

### State of the world entering Phase 13

| Concern | Today |
|---|---|
| Referee window controls | Single row in `ControlsWidget`: Pause, Rewind 10s, Replay Play, Slow 1/2x, Slow 1/4x, Step ◀, Step ▶, Jump to Live, Start game recording, Next Play. |
| Operator window controls | None. `show_controls=False` in `main.py`. The window has just video + status bar. |
| Recording state plumbing | `MainWindow._render_state` updates `controls_widget.long_recording_button.setText(...)` from `state.is_recording` and gates `next_play_button` enable via `controls_widget.set_recording_state(state.is_recording)`. Both signals route to the application coordinator, not the per-window `PlaybackController`. |
| Replay button gating | `set_recording_state` *also* gates `Replay Play`, `Step ◀`, `Step ▶` (they're meaningless outside RECORDING per §10.4). These stay on the referee window after the change, so the gating logic lives in the referee's controls widget. |
| Coordinator-level signals | `coordinator.toggle_long_session_recording` and `coordinator.mark_next_play` are application-level, not per-controller. Re-pointing the buttons to a different window is a re-wire, not a refactor. |

### Slices

**13.A — Split `ControlsWidget` into two widgets.**

- Rename current `ControlsWidget` → `RefereeControlsWidget`. Drops `long_recording_button` and `next_play_button` and their wiring; keeps everything else. `set_recording_state` still gates `replay_play_button`, `step_back_button`, `step_forward_button` — those buttons remain.
- New `OperatorControlsWidget`. Two buttons: `long_recording_button` (label flips between "Start game recording" / "Stop game recording") and `next_play_button`. Same touch-friendly button-height + stylesheet defaults as the referee widget. `set_recording_state` here gates only `next_play_button` enable (Start/Stop is always enabled — pressing it IS the toggle).
- Signals on `OperatorControlsWidget`: `long_recording_toggle_requested`, `next_play_requested`. Same semantics as today.

**13.B — `MainWindow` and `main.py` wiring.**

- `MainWindow.__init__` learns to build either widget. New flag `controls_role: str` with values `"referee"` / `"operator"` / `"none"`. Replaces the single `show_controls: bool`. Default `"referee"` keeps existing behavior for any test that constructs `MainWindow` with no kwargs.
  - `"referee"` → builds `RefereeControlsWidget`, wires the eight replay/transport signals to the controller (Pause, Rewind, Replay Play, Slow 1/2x, Slow 1/4x, Step ◀, Step ▶, Jump to Live).
  - `"operator"` → builds `OperatorControlsWidget`, wires Start/Stop and Next Play to the coordinator.
  - `"none"` → no controls (currently unused; preserves the option for a future "spectator" window).
- `main.py` referee window passes `controls_role="referee"`. The operator window passes `controls_role="operator"`. `live_only_window=True` on the operator window is unchanged.
- `_render_state` split: the recording-state-driven button updates (`long_recording_button.setText`, `next_play_button.setEnabled`) live in the operator-window branch. The referee-window branch keeps the gating for `replay_play_button` / step buttons. Recording state is read from the same `state.is_recording` field (UiState) — no new signal plumbing.
- Layout on operator window: keep the existing video panel + status bar; the two-button row sits directly above the status bar (matching the referee window's bottom-of-layout pattern). Two buttons centered or left-aligned — see open question Q2.

**13.C — Move the AlertBanner from referee to operator window.**

- Today (`MainWindow.__init__`): `AlertBanner(parent=self) if show_controls else None` — meaning referee-only.
- After 13.C: gate on `controls_role == "operator"` instead. Banner appears at the top of the operator window's layout, above the video panel, matching today's structural placement on the referee window.
- Referee window loses its banner. Operator-relevant alerts (per §11.3) still fire as health events into the JSONL log; the on-screen surface moves to the operator window.
- Update the §10.A AlertBanner doc note in this file to reflect the move: "the operator window is the alert surface because it's the recording-transport pane."
- Tests: `tests/test_main_window.py` (or wherever the banner-presence assertion lives today — TBD when 13.C lands) flips its assertion: banner present iff `controls_role == "operator"`.

### Exit criteria

- Pressing Start game recording on the operator window starts recording; the operator-window button's label flips to "Stop game recording" while RECORDING.
- Both windows' status bars continue to show recording state (existing `StatusBarWidget.update_state` path is unchanged).
- Next Play on the operator window is enabled iff `is_recording`; press marks a play boundary via `coordinator.mark_next_play`.
- Referee window has none of: Start game recording, Stop game recording, Next Play. Every other replay/review transport (Pause, Rewind, Replay Play, Slow 1/2x, Slow 1/4x, Step, Jump to Live) works as it does today.
- `tests/test_controls_widget.py` is split into per-widget classes (`RefereeControlsWidgetTests` / `OperatorControlsWidgetTests`); each asserts the buttons it owns and rejects (via absence) the buttons it doesn't.

### Open questions to resolve before implementing

1. **DiagnosticsWidget placement.** Today referee-only (gated on `show_controls=True`). The operator's preference (see CLAUDE.md / Phase 13 framing): most diagnostics are eventually hidden or hide-able; if displayed, they go to the operator window. Only replay-feature-relevant diagnostics belong on the referee window. This is a separate slice (13.D?) — flagging here so it doesn't get lost.
2. **Layout polish on the operator window.** Two buttons at the bottom — left-aligned, centered, or full-width-stretched? With only two buttons, full-width-stretched looks heavy; centered or left-aligned reads cleaner. (13.B can pick one; this isn't load-bearing.)

### Settled questions

- **AlertBanner moves to the operator window.** Phase 13.C. The "live-only pane stays uncluttered" framing is retired in exchange for keeping recording transport and recording alerts on the same window.

### Phase 13 sequencing notes

- **13.A first** since 13.B depends on the new widget classes existing.
- **No coordinator-side changes.** `toggle_long_session_recording` and `mark_next_play` are unchanged. The change is purely UI-layer button placement + signal-wiring.
- **No PlaybackController changes.** Both windows continue to drive their own controllers; the recording-transport buttons on the operator window route to the *coordinator*, not the operator controller.
- **Tests should be the easiest part.** No state-machine logic moves; `MainWindow._render_state` math stays the same, just split across two branches.

---

## Phase 14 – Window-spec UI rebuild: clips, challenge lockout, in-app post-process

Source of truth for this phase: [`docs/window-requirements.md`](window-requirements.md) (button-by-button requirements) and [`docs/window-layouts.pdf`](window-layouts.pdf) (layout mocks for both windows). Read both before implementing.

Goal:

- Replace the `plays`-as-only-clip-type model with a generalized `clips` model that supports four types (`pre-game`, `play`, `timeout`, `challenge`) plus a `marked` flag.
- Rebuild the operator-window control surface to match the spec: large Begin/End Game toggle with confirmation, Next Play / Time-out / Challenge / Mark Play, clip + play counters, per-window camera show/hide ribbon, "Post-process & Exit" link that runs the existing post-session processor in-app.
- Rebuild the referee-window transport row to match the spec: Play/Pause, 2x, 1/2x, 1/4x, 1/8x, Rewind 5s, Step ◀ / Step ▶, scrubber-slider seek widget. Remove Replay Play and Jump to Live (the operator's Challenge button drives both behaviors now). Add per-window camera show/hide ribbon + play-number badge (bottom-left).
- Add a **challenge lockout** to the referee window: when the operator presses Challenge, the referee window jumps to the start of the most-recent `type='play'` clip, paused, and is fenced to `[play.start, play.end]` until the operator opens a new clip (Next Play / Time-out / End Game).

### State of the world entering Phase 14

| Concern | Today | Phase 14 target |
|---|---|---|
| Per-game segmentation | `plays` table (one row per Next-Play boundary). `play_number` is 1-indexed and monotonic per game. `PlayManager` owns the in-memory pointer. | `clips` table; `play_number` derived from `WHERE type='play'`; `clip_number` is a separate 0-indexed monotonic counter that includes every clip type. `ClipManager` (renamed from `PlayManager`) owns the pointer. |
| Clip types | Implicit — every closed slice of the timeline is a "play." | Four explicit types: `pre-game` (exactly one per game, opened by Start Game), `play` (opened by Next Play, 1-indexed), `timeout` (opened by Time-out), `challenge` (opened by Challenge). |
| Marked clips | No surface. | `clips.marked BOOLEAN` toggled by the Mark Play button; persisted for downstream processes outside Phase 14 scope. |
| Operator window controls | `OperatorControlsWidget`: Start/Stop game recording, Next Play. (Phase 13.A.) | + Time-out, Challenge, Mark Play, Begin/End Game color-coded toggle with confirm modal, Post-process & Exit link. Clip counter (top-right) and play counter on the window chrome. |
| Referee window controls | `RefereeControlsWidget`: Pause, Rewind 10s, Replay Play, Slow 1/2x, Slow 1/4x, Step ◀, Step ▶, Jump to Live. (Phase 13.A.) | Play/Pause, 2x, 1/2x, 1/4x, 1/8x, Rewind 5s, Step ◀, Step ▶, scrubber slider. Replay Play and Jump to Live are removed — the operator's Challenge button replaces "jump to last play and pause." |
| Camera show/hide | `MultiFeedVideoPanel` always shows every enabled feed. | Per-window ribbon `[1][2][3][4]…` toggles tile visibility on that window's panel only. Hidden tiles do not affect ingest, recording, or the other window's panel. Grid reflows to fill the space. |
| Replay-button gating | `set_recording_state` disables Replay Play / Step buttons outside RECORDING. | Same gate applies to Step ◀/▶ and Pause-related affordances. Time-out and Challenge are additionally disabled until the first Next Play press of the game (game-has-started gate). |
| Challenge / lockout | No concept of clip-bounded replay. `_resolve_rewind_target_locked` clamps against `available_session_time_range()` (full recording extent). | New `PlaybackController.set_clip_bounds(start_ns, end_ns)` / `clear_clip_bounds()` API. While bounds are set, every seek primitive (Rewind, Step, scrubber, rate-change auto-snap) clamps to the range and snaps-to-PAUSED at either edge. Bounds drive the lockout described in `window-requirements.md` §Challenge. |
| Replay rate > 1.0 | `set_playback_rate(2.0)` already routes to `ReplayState.REPLAYING` per the `< 1.0` check at L378. Untested on the UI side; no 2x button exists. | Wire a `Speed 2x` button. Behavior: bounce through SEEKING into REPLAYING at 2.0× (mirrors how the slow-mo buttons enter from LIVE). |
| Post-process flow | `python -m app.tools.post_session_processor <session>` — separate CLI invocation; no UI surface. Still required as a fallback. | New "Post-process & Exit" link in the operator window. Stops recording (if running), then calls `post_session_processor.run(...)` in-process with a progress callback wired to a `QProgressDialog`. On success: closes both windows. On failure: shows the error in a modal and waits for OK before exiting. The CLI invocation continues to work for manual retry. |
| Crash recovery | `auto_close_open_plays_for_session` closes any open play and tags it `auto_closed_on_crash`. | Same shape, generalized to clips: continue whatever clip type was open at crash time. No special handling for in-flight timeouts/challenges — they re-open as the same type. |

### Slices

Phase 14 lands in five slices. 14.A is the schema + manager refactor (everything else builds on the new vocabulary). 14.B and 14.C are independent UI slices and can land in either order; 14.D depends on 14.A + 14.B (challenge button exists) + 14.C (referee window has a place to render the lockout state). 14.E is the post-process modal — independent of 14.D and could land at any point after 14.B.

**14.A — `plays` → `clips` schema + `PlayManager` → `ClipManager` refactor.**

- **Schema migration** — the old `plays` table is dropped wholesale (operator confirmed: existing data is throwaway). New table:

  ```sql
  CREATE TABLE IF NOT EXISTS clips (
      clip_id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      game_subdir TEXT NOT NULL,
      clip_number INTEGER NOT NULL,           -- 0-indexed, monotonic per game, includes every type
      type TEXT NOT NULL,                     -- one of: 'pre-game' | 'play' | 'timeout' | 'challenge'
      play_number INTEGER,                    -- non-NULL iff type='play'; 1-indexed, monotonic per game
      marked INTEGER NOT NULL DEFAULT 0,      -- Mark Play flag
      start_session_time_ns INTEGER NOT NULL,
      end_session_time_ns INTEGER,
      created_at TEXT NOT NULL,
      auto_closed_on_crash INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY(session_id) REFERENCES sessions(session_id),
      UNIQUE(session_id, game_subdir, clip_number),
      CHECK (type IN ('pre-game', 'play', 'timeout', 'challenge')),
      CHECK ((type = 'play') = (play_number IS NOT NULL))
  );
  CREATE INDEX IF NOT EXISTS clips_by_game ON clips(session_id, game_subdir, clip_number);
  CREATE INDEX IF NOT EXISTS clips_by_play_number ON clips(session_id, game_subdir, play_number) WHERE play_number IS NOT NULL;
  ```

  Migration code in `_initialize_schema`: if `plays` exists, `DROP TABLE plays;` after creating the new `clips` table. No data carry-over (operator confirmed). Follows the destructive-migration pattern of `_migrate_segments_unique_constraint_locked` (§8).
- **`Clip` model** — replaces `Play` in `app/core/models.py`. Fields mirror the table. `is_play` / `is_marked` convenience properties.
- **`ClipManager`** — renamed from `PlayManager` (file rename, class rename). Repo-wide grep before declaring the rename complete (`next_clip_button`, `play_manager`, `mark_next_play`, `current_play_number`, etc.) — `feedback_rename_grep` memory applies.
  - `start_game(...)` opens **clip #0 of type `pre-game`** (not play #1). Returns the `Clip`.
  - `mark_next_play(now_ns)` closes the current clip and opens a new clip with `type='play'`, `play_number = max(existing where type='play') + 1`, `clip_number = current + 1`. First call after `start_game` opens play #1.
  - `mark_timeout(now_ns)` closes the current clip and opens `type='timeout'`, `play_number=NULL`. Reject (no-op + status emit) if `current_play_number()` is None (i.e., no play has started yet).
  - `mark_challenge(now_ns)` closes the current clip and opens `type='challenge'`, `play_number=NULL`. Reject if current clip already has `type='challenge'` (no back-to-back challenges per spec). Reject if `current_play_number()` is None.
  - `set_marked(value: bool)` sets the `marked` flag on the in-memory current clip + the DB row. Toggle is the UI's responsibility.
  - `stop_game(now_ns)` closes the current clip and clears the pointer (unchanged semantics, generalized).
  - `current_play_number()` returns the play number of the most-recent `type='play'` clip in the current game (the in-memory pointer's value if its type is `play`, else the last `play` clip's number queried from the DB, else None during pre-game).
  - `current_clip()` / `current_clip_number()` mirror the existing `current_play()` / `current_play_number()` accessors.
  - `auto_close_open_clips_for_session(...)` — replaces `auto_close_open_plays_for_session`. Continues whatever type was open.
- **Coordinator wiring** — `ApplicationCoordinator` swaps `self._play_manager` → `self._clip_manager` and grows three new pass-throughs: `mark_timeout()`, `mark_challenge()`, `toggle_clip_mark()`. The Start/Stop game path calls `start_game` / `stop_game` with the same session-time anchors as today.
- **No UI in this slice** — buttons land in 14.B. The `OperatorControlsWidget` continues to expose only `long_recording_toggle_requested` and `next_play_requested`; the new signals get added in 14.B.
- **Tests** — `tests/test_clip_manager.py` (renamed from `test_play_manager.py`): covers `start_game` → pre-game; `mark_next_play` → play 1 / 2 / 3 with correct clip_number progression; `mark_timeout` rejection during pre-game; `mark_challenge` rejection during pre-game and back-to-back; `current_play_number` returns the last play across timeouts; `set_marked` toggles cleanly; crash-recovery `auto_close_open_clips_for_session` handles every type. `tests/test_metadata_db.py` covers schema migration from a pre-Phase-14 `plays`-only DB (drops cleanly; new `clips` table is queryable).

**Out of scope for 14.A:** any UI button or modal. `plays_json_export.py` (Phase 8) — defer the JSON-shape update to a follow-on slice or treat as breaking-change downstream of the post-processor; the operator confirmed the existing artifacts don't need preservation but the JSON exporter needs to know about types and `marked` if downstream tools consume it. Flag in 14.E open questions.

**14.B — Operator window controls + clip/play counters + camera ribbon.**

- **New buttons on `OperatorControlsWidget`** — Time-out, Challenge, Mark Play. Stacked on the right edge per the PDF (Next Play, Time-out, Challenge as a vertical group; Mark Play below them with a gap; Begin/End Game at the bottom-right). Existing Next Play stays.
  - Signals: `timeout_requested`, `challenge_requested`, `mark_play_toggle_requested`. Coordinator wires them to `mark_timeout` / `mark_challenge` / `toggle_clip_mark`.
  - **Begin/End Game restyle** — the existing `long_recording_button` becomes color-coded (green when not recording / "Begin Game"; red when recording / "End Game"). Pressing the red variant pops a `QMessageBox.question` "Are you sure you want to end this game?" before invoking `toggle_long_session_recording`.
  - **Gating** — Time-out and Challenge are enabled iff `is_recording AND current_play_number() is not None` (i.e., the operator has pressed Next Play at least once). Challenge is additionally disabled if `current_clip().type == 'challenge'`. Mark Play is enabled iff `is_recording`. Wire via a new `set_clip_state(is_recording: bool, has_play_started: bool, current_clip_type: str | None)` method on the widget; coordinator emits a signal after every clip transition so the widget can re-evaluate.
- **Clip + play counters** — new `OperatorStatusOverlay` widget showing `Play NNN` and `Clip NNN`. Free-floating overlay (same pattern as `MultiFeedVideoPanel`'s playback-status pill: `WA_TransparentForMouseEvents`, translucent background), positioned top-right of the operator window but offset from the button stack so it does not crowd Next Play / Time-out / Challenge. Updates on every `clip_changed` signal. `Play` shows `current_play_number()` or `N/A` during pre-game. `Clip` shows the 0-indexed `current_clip_number()`.
- **Post-process & Exit link** — `QLabel` with a clickable hyperlink-style "Post-process & Exit" at the bottom-center of the operator window. Click handler is a placeholder in 14.B (`LOGGER.info("Post-process requested")`) — actual wiring lands in 14.E.
- **Camera show/hide ribbon** — `CameraVisibilityRibbon` widget with one `QPushButton` per feed, labeled with the 1-based feed index (`1` / `2` / `3` / `4` per the mock). The tile titles inside `MultiFeedVideoPanel` continue to use `feed.display_name` — the ribbon is the compact toggle surface; the panel tile carries the human-readable name. Toggles emit `feed_visibility_toggled(feed_id, visible)`. `MultiFeedVideoPanel` gains a `set_tile_visible(feed_id, visible)` method that hides the cell from the grid and triggers a relayout — grid recomputes columns so remaining tiles fill the available width. The "Clip selector widget" label in the PDF mock is a future-enhancement placeholder; in Phase 14 it renders as a non-interactive label showing the current clip type/number alongside the ribbon (operator confirmed: future expansion is out of scope).
- **MainWindow layout (operator branch)** — three new structural pieces: top-right counters overlay, right-edge button stack, bottom row with ribbon + Post-process link. Existing AlertBanner (Phase 13.C) stays at top. The two-button Phase 13 controls strip is replaced by the new operator control surface.
- **Tests** — `tests/test_operator_controls_widget.py` extends: new signals emit on press; gating respects `(is_recording, has_play_started, current_clip_type)`; End Game button shows confirm modal (use `QMessageBox` patch). `tests/test_main_window.py` confirms the counters update on `clip_changed`.

**Out of scope for 14.B:** challenge → referee lockout wiring (14.D). Post-process modal (14.E). Scrubber slider on referee (14.C).

**14.C — Referee window transport rebuild + camera ribbon + play counter.**

- **`RefereeControlsWidget` rebuild** — remove `replay_play_button` and `live_button`. Add:
  - `pause_button` (existing, but label flips to `▶` / `⏸` depending on `_playback_rate`).
  - `speed_2x_button` → `set_playback_rate(2.0)`. Goes through the existing `SEEKING → REPLAYING` bounce (the rate-routing branch at L378 already picks REPLAYING when rate ≥ 1.0).
  - `half_speed_button`, `quarter_speed_button` (existing).
  - `eighth_speed_button` → `set_playback_rate(0.125)`.
  - `rewind_button` — relabel "Rewind 10s" → "Rewind 5s". The duration is config-driven so we can tweak it later without code changes. **Settings change:** the existing `_REWIND_10S_NS` constant becomes `settings.replay_rewind_seconds` (new `[replay] rewind_seconds: int = 5` setting). Update `app_settings.toml.example`, `AppSettings.load`, and `_validate_replay`. Rename `PlaybackController.rewind_10_seconds()` → `PlaybackController.rewind_configured_seconds()` and grep the full repo for every caller / test / signal name (`feedback_rename_grep` memory applies). The button label is set from `settings.replay_rewind_seconds` at construction time so future config tweaks don't need a UI change.
  - `step_back_button`, `step_forward_button` (existing, unchanged; honor `settings.replay_frame_step_count`).
- **Scrubber slider widget** — new `ScrubberSlider` (`QSlider(Qt.Horizontal)` subclass or composite). Live-updates from the controller's `_playback_session_time_ns` between user interactions; user drag emits `seek_to_session_time_requested(target_ns)` which routes to a new `PlaybackController.seek_to_session_time(ns)` primitive (extracted from the existing rewind logic — same clamp-against-bounds, same FSM bounce-to-PAUSED behavior). One scrubber per window, not per tile — the slider seeks the shared replay clock that drives all tiles.
- **Play-number badge (bottom-left)** — `QLabel` showing `Play NNN` (or `N/A` during pre-game). Updates on the same `clip_changed` signal the operator counters listen for. **Challenge mode coloring:** when the challenge lockout (14.D) is active, the label text color flips to red (`#d24747` or similar — match the End Game button red for visual consistency). This is the only visible indicator of challenge mode on the referee window; the bound-clamping behavior on Rewind / Step / scrubber is otherwise invisible. Color resets to normal on `challenge_state_changed(active=False)`.
- **Camera show/hide ribbon** — same `CameraVisibilityRibbon` widget as 14.B, owned by the referee window's `MultiFeedVideoPanel`. State is per-window (operator hiding camera 3 does not hide it on referee).
- **Removed buttons cleanup** — `replay_current_play_requested` and `live_requested` signals + their coordinator wiring are deleted. `PlaybackController.replay_current_play()` and `request_live()` stay on the controller (used internally by challenge lockout in 14.D and by `jump_to_latest_replayable` post-lockout-clear), but no UI button exposes them directly.
- **Tests** — `tests/test_referee_controls_widget.py` covers: new buttons emit expected signals; speed-2x routes through REPLAYING; eighth-speed routes through SLOW_MOTION; pause-button label flips with rate; scrubber emits with the right session-time. `tests/test_playback_controller.py` adds a 2.0× rate case (currently uncovered) and a `seek_to_session_time` primitive case.

**Out of scope for 14.C:** challenge lockout behavior (14.D). The scrubber's behavior during a challenge is enforced by 14.D's clamping logic — the slider widget itself doesn't need to know about challenges.

**14.D — Challenge lockout in `PlaybackController` + cross-window wiring.**

- **New primitives on `PlaybackController`:**
  - `set_clip_bounds(start_session_time_ns: int, end_session_time_ns: int | None)` — install a fence. `end` may be None initially (the play just closed, but if a downstream caller wants the most-recent finalized end-time, the controller queries the segment store). Stored as `self._clip_bounds: tuple[int, int | None] | None`.
  - `clear_clip_bounds()` — remove the fence; replay resumes against the full `available_session_time_range()`.
  - All seek-resolution helpers (`_resolve_rewind_target_locked`, the new `seek_to_session_time` from 14.C, `step_frames`, the auto-snap branches inside `set_playback_rate`) consult `_clip_bounds` first. If the requested target is outside the fence, clamp to the nearest fence edge and force `_playback_rate = 0.0` (PAUSED) before rendering. Emit a status: `"Held at start of play (challenge)"` or `"Held at end of play (challenge)"`.
  - The replay clock tick (`_render_at_session_time_ns` called from `_advance_replay_clock`) also consults the fence — if the natural-rate advance would cross `end`, snap to `end` and PAUSE (this covers the "play forward at 1× and auto-pause at end of play" case).
- **`replay_current_play()` is repurposed as the challenge-open hook** — public API: snap the playback clock to a given `start_session_time_ns`, install bounds `(start, end)`, bounce to PAUSED, render. The coordinator's challenge wiring calls it on the referee controller with the bounds from the just-closed play clip.
- **Cross-window signal wiring** — `ApplicationCoordinator.mark_challenge(now_ns)`:
  1. Reads the most-recent `type='play'` clip from `ClipManager` (or `MetadataDb.last_play_clip_for_current_game()`).
  2. Calls `self._clip_manager.mark_challenge(now_ns)` — opens the challenge clip; rejects if back-to-back or pre-play.
  3. On success: calls `self._referee_playback_controller.replay_current_play(play.start_session_time_ns, play.end_session_time_ns)`. (The operator controller is `live_only=True` — no fence applies.)
  4. Emits `challenge_state_changed(active=True)` so the referee window can flip its play-number badge to red (the only visible challenge-mode indicator; see 14.C).
- **Lockout clear** — `mark_next_play` / `mark_timeout` / `stop_game` all call `self._referee_playback_controller.clear_clip_bounds()` if a fence is installed, then emit `challenge_state_changed(active=False)`. After clear, the referee window stays paused at the current position (no auto-jump to live) — the referee can press Play, 2x, etc. to resume from wherever they are.
- **End-of-fence rendering** — the existing `nearest_frame_location` clamping at segment boundaries already returns the boundary frame frozen when no coverage exists past the end. The fence reuses this — at `end_session_time_ns`, every feed shows its last covered frame, paused. No new frame-rendering code; just clamping.
- **Tests** — `tests/test_playback_controller.py` adds: `set_clip_bounds` clamps Rewind; clamps Step; clamps `seek_to_session_time`; auto-clamps `set_playback_rate(1.0)` advance at the end edge; `clear_clip_bounds` re-exposes full range. `tests/test_application_coordinator.py` adds: Challenge press fences the referee controller with the right bounds; Next-Play / Time-out / End-Game press all clear the fence; back-to-back Challenge presses no-op on the second.

**Out of scope for 14.D:** any behavior on the operator window's controller (it's `live_only`; bounds don't apply). The visual challenge indicator is handled by 14.C's play-number badge color flip — no separate overlay widget.

**14.E — In-app Post-process & Exit modal.**

- **`PostProcessDialog` (new)** — `QDialog` with a `QProgressBar`, a status `QLabel` ("Processing game_001 / camera_1.mp4…"), and (during success) a Close button that closes both windows; (during failure) an OK button that surfaces the error message and closes both windows on press.
- **Wiring on the operator window's "Post-process & Exit" link:**
  1. If `state.is_recording`, call `coordinator.toggle_long_session_recording()` to stop the game (which also closes the open clip via `ClipManager.stop_game`). Block on the recording-stopped signal — the post-processor only sees finalized segments.
  2. Construct the run plan via `post_session_processor.build_plan(session_dir, ...)`.
  3. Instantiate `PostProcessDialog`, show it modally.
  4. Run `post_session_processor.run(plan, progress_callback=dialog.update_progress)` on a `QThread` (the processor shells out to ffmpeg per (game, feed) item — those subprocesses can take minutes; don't block the Qt event loop). Use the existing `app/core/segment_validator_worker.py` pattern as a model for the worker shape.
  5. On worker `finished(success: bool, error: str | None)`:
     - Success → close dialog → close both windows → `QApplication.quit()`.
     - Failure → swap progress bar for error message, keep OK button enabled, wait for click, then close.
- **Existing CLI invocation continues to work** as a manual retry path. No changes to `app/tools/post_session_processor.py` semantics; only a new callable entry point. Confirm the existing entry point already accepts a `progress_callback` parameter — if not, add one (signature: `Callable[[int processed, int total, str current_item], None]`) without changing the CLI surface.
- **`plays_json_export.py` update** (deferred call-out from 14.A) — the exported JSON shape needs to include `type` and `marked` so downstream tools see the new fields. The CLI invocation already runs as part of the post-session processor's plan. Update in place; no schema versioning needed (operator confirmed: downstream processes are in-tree and updated together). Rename the file to `clips_json_export.py` to match the new vocabulary; grep for every importer/caller (`feedback_rename_grep` memory applies).
- **Tests** — `tests/test_post_process_dialog.py` (new): success path closes the dialog; failure path holds open until OK pressed. `tests/test_post_session_processor.py` (existing): add a `progress_callback` interaction test.

**Out of scope for 14.E:** any retry-from-dialog behavior. Failure surfaces the error and the operator re-runs from CLI per the existing manual workflow.

### Exit criteria

- `clips` table replaces `plays`. Every clip has a type from {pre-game, play, timeout, challenge}, a 0-indexed monotonic `clip_number`, and a `marked` flag. Pre-Phase-14 DBs migrate cleanly (old plays data dropped — operator-confirmed).
- Operator window: Begin Game (green) → confirm-modal End Game (red); Next Play, Time-out, Challenge, Mark Play all create / mark the right clip type with the right gating (Time-out and Challenge disabled until first Next Play; Challenge ignored back-to-back). Clip + play counters reflect ClipManager state. Camera ribbon hides/shows tiles on the operator window only.
- Referee window: transport row has Play/Pause, 2x, 1/2x, 1/4x, 1/8x, Rewind 5s, Step ◀, Step ▶, scrubber. Replay Play and Jump to Live are gone. Camera ribbon and play-number badge work per-window.
- Challenge flow: pressing Challenge on operator window jumps the referee window to the start of the most-recent play, paused. Referee cannot seek before play.start or after play.end (auto-clamps to PAUSED). Lockout clears when operator presses Next Play / Time-out / End Game.
- Post-process & Exit: pressing the link stops recording (if running), runs the post-processor in-app with a progress modal, and closes both windows on success / waits for OK on failure. CLI invocation still works as manual retry.
- All existing replay / recovery / pipeline tests pass. New tests cover clip-type transitions, clip-bound clamping, the post-process dialog states, and the camera-ribbon visibility toggle.

### Settled questions

- **Counter placement on the operator window:** free-form `QLabel` overlay (same pattern as `MultiFeedVideoPanel`'s playback-status pill), top-right but offset from the button stack so it does not crowd Next Play / Time-out / Challenge.
- **Camera ribbon labels:** 1-based feed index on the ribbon buttons; `feed.display_name` continues to label the tile in `MultiFeedVideoPanel`.
- **`rewind_10_seconds` rename:** rename to `rewind_configured_seconds` on `PlaybackController`; full-repo grep before declaring complete. Duration is config-driven (`[replay] rewind_seconds`) so future tweaks are a settings change, not code.
- **Challenge indicator on the referee window:** flip the play-number badge text color to red while a challenge is active (re-using the End Game red for visual consistency). No separate overlay widget.
- **`plays_json_export.py` schema versioning:** none. Downstream consumers are in-tree and updated atomically with the JSON shape; rename the file to `clips_json_export.py` to match the new vocabulary.

### Phase 14 sequencing notes

- **14.A blocks everything else.** The new clips vocabulary is referenced by every UI change. Land it under tests first; the rest is mechanical.
- **14.B and 14.C are parallel-safe.** They touch different files (`operator_controls_widget.py` vs `referee_controls_widget.py`), different windows, different signals. If two people are working in parallel, split here.
- **14.D depends on the operator's Challenge button existing (14.B) and the referee window having no Replay-Play button to confuse the lockout semantics (14.C).** Land 14.D last among the UI slices.
- **14.E is independent of 14.D** but depends on 14.B for the Post-process link to exist. Can land between 14.B and 14.C, or after 14.D.
- **Repo-wide rename grep is mandatory before declaring 14.A complete.** `feedback_rename_grep` memory: a partial-grep rename of a Qt signal in Phase 13 missed `next_clip_button` and crashed the app at startup. Touch every reference to `play_manager`, `mark_next_play`, `current_play_number`, `next_play_button` (where it should stay as Next Play but new clip-aware accessors are nearby), `play_id`, `play_number`, `auto_close_open_plays_for_session`.
- **No GStreamer pipeline changes.** Phase 14 is entirely UI + state + schema. The existing recording / replay paths are unchanged.

---

# 19. Recommended Implementation Defaults

Use these defaults unless hardware testing proves otherwise. The "shipped" column shows the current state; the "target" column shows the long-term default.

| Setting | Shipped today | Long-term target |
|---|---|---|
| Live recording codec | MJPEG | MJPEG (Phase 11.B finding — ProRes/DNxHR couldn't be made to work on the live path; see §5.2) |
| Live recording container | MKV | MKV (matroskamux's permissive late-pad handling is needed for the Phase 9.C audio-wiring pattern) |
| Archive deliverable codec | H.264/AAC MP4 (post-session processor) | ProRes/DNxHR or H.264 (operator-selectable; produced by post-session transcode) |
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
