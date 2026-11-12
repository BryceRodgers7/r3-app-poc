# Replay glitch at segment seams — findings, scaling to 6-8 feeds / 60 fps, segment length, and the recommended fix

Date: 2026-11-10 (sections 6-8 added 2026-11-11). Status: **investigated, not fixed.**
Nothing under `app/` was changed. Read this before touching segment bookkeeping in
`PipelineManager`, `RecordingSegmentReplayStore.nearest_frame_location`,
`SegmentDecoder`, or `PlaybackController.step_frames`, or before changing
`[recording] segment_duration_seconds` (section 6) or the `[app]` target size and frame
rate (section 8). **Section 7 is the recommended fix for the timing jitter**; start there
if you are about to implement one. Section 7.8 says how often a repeated or skipped frame
is still expected once it is in.

Hardware for every measurement here: Intel i7-1165G7 (4 cores / 8 threads), Windows 11,
GStreamer 1.28 on MSYS2 UCRT64. On this machine every recording uses **software**
`jpegenc`: `qsvjpegenc` fails to link in `app.log` ("swapping in software fallback",
41 times), so the QuickSync path in `encoder_factory.py` is not in play.

---

## 1. Symptom

Referee window, single-stepping frame by frame across the seam between two mkv
segments: the same frame shows for 2-3 clicks, then 0, 1 or 2 frames of the next
segment are skipped. (Jog wheel and Step buttons both go through
`PlaybackController.step_frames`.)

Reproduced offline with the app's real `RecordingSegmentReplayStore` and `SegmentDecoder`
against real recordings, replaying `step_frames`' arithmetic (`T += 1e9 / target_fps`).
On session_018 (NDI, 30 fps), stepping through the middle of a segment was clean in
168 of 168 runs. Across a seam, **65% of 168 runs glitched**: longest same-frame run
1 / 2 / 3 clicks in 41 / 50 / 9%, frames skipped 0 / 1 / 2 in 41 / 51 / 8%.

## 2. Causes

### 2.1 Every seam has a hole in session time that no segment covers

- `pipeline_manager.py:2338-2342`: a segment's `start_session_time_ns` is a wall-clock
  reading taken in the encoder-src buffer probe at its first buffer, and
  `end_session_time_ns = last_pts + offset`, the *start* of its last frame with no frame
  duration added. The next segment starts about one frame later, so `(end_N, start_N+1)`
  belongs to nobody.
- `segment_replay_store.py:251-277` (case 5, gap): a click inside the hole freezes on
  segment N's last frame (`offset = duration_ns`, `is_freeze=True`). That is the repeated
  frame.
- The next click lands a few ms into N+1 and `segment_decoder.py:181` rounds
  `int(off_ms * fps / 1000 + 0.5)`: an offset past half a frame period becomes index 1,
  so frame 0 is skipped.
- With a nominal one-frame hole, about half of all click phases hit "repeat + skip". A
  hole wider than one click period holds two clicks, which gives the run of 3.

### 2.2 The recorded segment times disagree with the files

Found by comparing the `segments` table with `ffprobe -show_entries packet=pts_time`
for every file.

- **The probe runs a constant two buffers ahead of the file cut.** DB first/last pts =
  file first/last pts + 2 frames, in every segment of every session checked (1 frame for
  a game's first segment, whose first buffer arrives while `_pending_segment` is None).
  Cause inside `splitmuxsink` is not confirmed. By itself it only shifts the window, but
  replay lags true session time by about 2 frames, and clip/challenge fences (session
  time) are that far from the displayed frames.
- **Blind window.** `_finalize_pending_segment_locked` sets `_pending_segment = None`
  (then DB insert + `commit()`, `stat`, validator submit, `mkdir`, logging) and the new
  pending dict only exists at the end of `_on_splitmuxsink_format_location`
  (`pipeline_manager.py:2206-2266`). A buffer reaching the probe in between is counted in
  neither segment: DB `frames=119` for a 120-frame file, hole of 2 frames.
- **Per-segment wall-clock offset.** `first_session_time_ns - first_pts` is one sample per
  segment, so it carries the encoder/queue latency at that instant. Consecutive
  segments' offsets differ by the "jitter" in section 3.2, and that difference *is* the
  hole's deviation from one frame.

### 2.3 Reading past the real end of a file returns nothing

`SegmentDecoder` returns `None` when `read()` fails (`segment_decoder.py:203-207`) and
`_render_at_session_time_ns` then keeps the previous frame
(`playback_controller.py:1191-1194`). That makes extra "same frame" clicks:

- First segment of each game: the DB says 121 frames / 4000 ms, the file has 120, so the
  last click asks for index 120 and gets nothing.
- A source whose real rate differs from the 30/1 the mkv declares. `feed_main` (the VLC
  NDI feed) delivers about 28.3 fps (35.4 ms spacing); `duration_ns` maps to index ~118
  on a 112-frame file. Result in simulation: 3-4 clicks on one frame, then 0-1 skipped.

### 2.4 Context

`step_frames` steps by `1e9 / target_fps` of session time (`playback_controller.py:644`),
so a "frame" is a time quantum, not the next recorded frame. That is exact at a true
30 fps and wrong for a variable-rate source. Each feed has its own seams, so tiles
glitch on different clicks.

---

## 3. Answer: how it gets worse with 6-8 NDI feeds, and with 60 fps input

### 3.1 Short answer

- **More feeds make the *seam bookkeeping noisier*, and the data already shows it.**
  Going from one feed to two made the segment-start timestamp about **10x noisier**
  (sigma 1.4 ms to about 13 ms, in recordings from both before and after the 2026-11-07
  fixes). On the current code it also raised the share of seams that lose a frame from
  3.6% to **40%**. Recording CPU is the likely driver: on this machine the encode chain
  alone saturates at about 440 fps at 720p, so 8 feeds at 30 fps are already at 54% of it
  before NDI decode, preview and replay. **Nothing with 6-8 feeds has been recorded or
  measured**, so the 6-8 feed behaviour below is a projection from the 1 to 2 feed trend
  plus capacity measurements.
- **More feeds also multiply the number of seams on screen.** Every tile has its own seam
  every 4 s; with 8 feeds some tile glitches on roughly 8% of all clicks (about 1% with
  one feed), and the glitching spreads over a zone of several clicks around each 4 s
  boundary instead of one spot.
- **60 fps makes each seam about twice as wrong in frames.** The errors come from
  millisecond-sized timing (a 3 ms commit, 5-30 ms of wall-clock jitter, a callback
  that runs 20-30 ms late); a 16.7 ms frame period holds twice as many frames of it as a
  33.3 ms one. The glitch lasts about the same in milliseconds but the referee, stepping
  per frame, sees about twice as many wrong clicks.
- **The 60 fps case only applies if `target_fps = 60`.** With 60 fps cameras and the
  default `target_fps = 30`, `videorate` thins the video to 30 fps ahead of the `tee`
  (`ndi_receiver.py:209-263`, `pipeline_manager.py:1216-1225`), so the files, the step size
  and the seam structure stay as measured. Only the ingest conversion load doubles.
  `NDI_OPEN_BUGS.md` reports 1280x720@60 runs, which implies `target_fps = 60` was used there.
- **None of this is a new bug.** It is the same three causes in section 2, with their
  inputs growing. The fix that removes the dependence on feed count and fps is to stop
  deriving segment times from a wall-clock sample in a probe (section 5).

### 3.2 What was measured

All from `C:\SportsReplay\metadata.db` and the files under `sessions\`, opened read-only.

**Frame lost at the seam** (the DB's pts gap between segment N's last frame and N+1's
first is more than one frame period):

| Code | Feeds recording | Feed-runs | Seams | Seams that lost a frame |
|---|---|---|---|---|
| Post-fix (sessions 010 onward) | 1 (011, 014-017, 022) | 6 | 303 | **11 (3.6%)** |
| Post-fix (sessions 010, 018) | 2 | 8 | 636 | **256 (40.3%)** |
| Pre-fix (sessions 002-009) | 2 | 10 | 218 | **9 (4.1%)** |

"Post-fix" means recorded after commits `d0b4274` (11-07 20:52, "start game 1 like game
2+": the first Start now builds a fresh `splitmuxsink`) and `e437f01` (21:37). Sessions
002-009 ran at 18:23-18:51 on 11-07, **before** them. So the jump from 4% to 40% with two
feeds coincides with that code change *and* with whatever else differed between days
(load, content); this data cannot separate the two. Within post-fix code, one feed vs
two is 3.6% vs 40%, an 11x difference. Session_011 is a single-feed outlier (10 of 19
seams; without it single-feed is 1 of 284, 0.4%). Within a lossy two-feed run, one feed
loses a frame at 53-69% of its seams and the other at 4-18%, and it is not reliably the
feed that finalizes second, so database lock contention is *not* the explanation (see
3.3.4). The pre-fix recordings' mkv files have since been deleted (only their database
rows remain), so the probe lead in 2.2 could not be checked for them. For post-fix
session_010 the file audit confirms the loss is real: `frames=119` in the database for a
120-frame file, head delta 3 frames.

**Wall-clock offset jitter** between consecutive segments of a feed (robust sigma of
the successive differences of `pts_to_session_offset_ns`, first segment of a game
excluded):

| Feeds recording | Median sigma | Median p95 | Worst seen |
|---|---|---|---|
| 1 | **1.4 ms** | 4.9 ms | 28.9 ms |
| 2 | **13.1 ms** | 30.5 ms | 188 ms |

Hole width, all 1157 seams in the database, as a multiple of the frame period: about
1 frame 79%, under 0.5 frame 12%, about 2 frames 7%, 2.5 or more 2%. Healthy single-feed
sessions are about 100% "exactly one frame".

**Encode capacity** (`videotestsrc` ball, BGR to I420 bt601 to `jpegenc quality=85`,
N processes at once; encode only — no NDI receive, preview, Python or replay):

| Feeds | 720p: fps per feed | 720p: aggregate | 1080p: fps per feed | 1080p: aggregate |
|---|---|---|---|---|
| 1 | 94 | 94 | 59 | 59 |
| 2 | 126 | 251 | 56 | 112 |
| 4 | 111 | 443 | 45 | 181 |
| 6 | 75 | 450 | 30 | 177 |
| 8 | 55 | 442 | 22 | 178 |

So the CPU tops out at about **440 fps (720p) or 180 fps (1080p)**. 8 feeds need 240 fps at
30 fps and 480 at 60 fps; at 1080p 6 or more feeds at 30 fps already need all of it.
(The 1-feed row includes process start-up.) Synthetic content: real JPEG sizes differ.

**SQLite commit** (a scratch database with `MetadataDb`'s default pragmas, one
`insert` + `commit()` per call): median **3.1 ms** idle, 2.7-3.0 ms with 10, 20 and 40 MB/s of
concurrent file writes, worst 13 ms.

**Replay decode, N feeds, idle machine** (real mkv files as stand-ins, thread pool of
`max(2, N)` like `_decode_jobs`; decode only — the Qt draw is not included):

| Feeds | 720p seam click (open + seek + read all N) | 720p normal click | 720p max ticks/s | 1080p seam click | 1080p normal click | 1080p max ticks/s |
|---|---|---|---|---|---|---|
| 1 | 14 ms | 3.8 ms | 249 | 33 ms | 8.7 ms | 114 |
| 2 | 21 | 4.6 | 216 | 29 | 9.7 | 101 |
| 4 | 31 | 5.2 | 191 | 41 | 11.4 | 88 |
| 6 | 44 | 6.5 | 158 | 53 | 13.5 | 76 |
| 8 | 51 | 7.7 | 131 | 64 | 16.5 | 61 |

Replay decode scales gently. It is not what causes duplicate/skipped frames; it adds a
hitch (latency) on the click that crosses a seam, and it limits playback frame rate.

### 3.3 Why feed count makes it worse

1. **Timing noise grows with load (measured 1 to 2 feeds, projected beyond).** The hole's
   deviation from one frame is the offset jitter, so 13 ms sigma means holes of 0 to 2
   frames are routine at 30 fps. The jitter is just as large in the pre-fix two-feed
   recordings (median sigma about 11 ms) as in the post-fix ones (about 14 ms), so unlike
   the frame-loss rate it is not tangled up with the code change. At 6-8 feeds the CPU is
   much closer to saturation (above: encode alone takes 54% of the machine at 8 x 720p30,
   and over 100% at 6 x 1080p30),
   plus 8 NDI decoders, `videoconvert`/`videoscale` per feed, two preview windows, many
   Python pad probes sharing one GIL, and replay decoding while recording. Expect sigma
   at or above the 2-feed 13-30 ms, i.e. holes of 0 to 3 frames, and at true saturation
   real dropped frames (`recording_branch_saturated`), which no bookkeeping fix can hide.
2. **More tiles, more seams.** Each tile glitches about 1.27 clicks per 4 s seam (session_018
   `ndi_1`, a feed that loses a frame at 18% of its seams; lossier feeds are worse).
   With 120 clicks per 4 s at 30 fps that is p = 1.06% of clicks per tile. If seams do
   not coincide, the chance that *some* tile shows a glitch on a given click is
   `1 - (1 - p)^N`:

   | Feeds | 1 | 2 | 4 | 6 | 8 |
   |---|---|---|---|---|---|
   | Clicks with a glitch on some tile | 1.1% | 2.1% | 4.2% | 6.2% | 8.2% |

   This is a floor: it uses a feed that loses a frame at 18% of its seams, and the
   post-fix two-feed average is 40%.
3. **Seams form a zone.** All feeds start at Begin Game and rotate every 4 s of their own
   stream, so their seams cluster. Measured spread between two feeds' finalize times:
   median 37-41 ms (session_010), 71 ms (005), 669 ms (018 game 2). For N feeds spread
   uniformly over S, the expected range is `S (N-1)/(N+1)`: 0.33 S for 2 feeds, **0.78 S
   for 8 (2.3x wider)**. Stepping through the 4 s boundary, the referee sees glitches on
   *some* tile for several consecutive clicks.
4. **Database lock contention is a small term, not the main one.** All feeds share one
   `MetadataDb` connection and `_write_lock`, with a `commit()` per insert inside the
   blind window (`metadata_db.py:290-330`). At 3 ms per commit the last of 8 feeds waits
   at most about 21 ms, and only if all 8 boundaries land within a few ms of each other,
   which the measured spread makes rare. Worth moving out of the callback regardless.
5. **The seam click gets slower.** All N feeds open their next file inside one click:
   14 to 51 ms at 720p, 33 to 64 ms at 1080p on an idle machine (table above). Real use is
   slower because the 8 encoders are competing for the same CPU. The Qt draw of N tiles
   happens serially on the UI thread and was **not** measured here;
   `NDI_OPEN_BUGS.md` 3 reports about 180 ms per click at four 1080p feeds before the
   sequential-read fix.

### 3.4 Why 60 fps makes it worse

Case A, **60 fps cameras, `target_fps = 30`** (the shipped default): files are 30 fps,
`frame_period_ns` is 33.3 ms, so every number in 3.2 and 3.3 stands. The cost is on the
ingest side: `videoconvert` and `videoscale` run on all 60 input frames per second and
`videorate` then drops half, so conversion work per feed doubles (not measured), which
feeds back into 3.3.1.

Case B, **`target_fps = 60`** (files at 60 fps, `frame_period_ns` 16.7 ms,
`application_coordinator.py:159-166`):

| Quantity | 30 fps | 60 fps |
|---|---|---|
| Frame period | 33.3 ms | 16.7 ms |
| Structural hole | 1 frame = 33 ms | 1 frame = 17 ms (same frames, half the time) |
| Wall-clock jitter, 2 feeds, sigma / p95 | 0.4 / 0.9 frames | **0.8 / 1.8 frames** |
| Frames lost per seam from a 3 ms blind window | window / 33.3 | **window / 16.7 (2x)** |
| Frames repeated or skipped per seam | n | **about 2n** |
| Clicks per 4 s segment | 120 | 240 |
| Duration of a glitch in milliseconds | about the same | **about the same** |
| 8-feed software encode, 720p | 240 fps of 442 (54%) | **480 fps: over capacity** |
| 6-feed software encode, 720p | 180 fps (41%) | 360 fps (81%) |
| Segment size on disk | 5-15 MB per 4 s per feed | about double (8 feeds: tens of MB/s) |
| 8-feed 1080p replay decode ceiling | 61 ticks/s | **needs 60: no headroom left for drawing** |

At 60 fps the replay timer (`_adapt_replay_interval`) will fall back to whole-frame
multiples, so *playback* (not stepping) shows every 2nd-4th frame with 6-8 feeds. Stepping
is unaffected by that, but each click still renders every feed.

Also: `target_fps` and `frame_period_ns` are global. A mixed set of 30 and 60 fps cameras
is thinned or padded by `videorate` to the one setting; a 30 fps source recorded at
`target_fps = 60` is padded with duplicate frames, which look like stuck frames when
stepping and are not a seam bug.

### 3.5 What is not known

- No recording with more than 2 feeds, or with 60 fps files, exists on this machine
  (every `P` in the database is 33.3 or about 35.4 ms). Every 6-8 feed and 60 fps figure
  above is a projection.
- The blind-window loss rate cannot be derived from the 3 ms commit: it predicts about
  10% at most, yet one feed in each lossy run loses a frame at 53-69% of seams. The
  window is longer than the commit alone (GIL waits, logging, validator submit) or the
  callback's phase against the frame arrivals is not random. Which is not shown.
- Why the probe is exactly two buffers ahead of the file cut is not shown.
- Whether the `d0b4274` change (fresh `splitmuxsink` on the first Start, `async-handling`,
  re-arm after rebuild) is what turned a 4% two-feed loss rate into 40%, or whether it is
  load. Section 4 step 0 settles it.
- The Qt draw cost of 6-8 tiles per click, and replay timing while recording, were not
  measured.

---

## 4. How to measure it properly (next step before choosing a fix)

Use the real-app harness in [`ndi_test_harness/`](ndi_test_harness/), with one camera
configured as N `[[feeds]]` as the earlier four-feed runs did, for N = 2, 4, 6, 8 and
`target_fps` = 30 and 60. About 5 minutes per run, then:

0. **Pre-fix vs post-fix A/B, two feeds, before anything else.** Run the tree from before
   `d0b4274` (`git archive <commit> app main.py tests | tar -x -C <dir>`, see
   `NDI_OPEN_BUGS.md` section 5) against `HEAD`, same cameras, interleaved. If the
   pre-fix tree loses about 4% of seams and `HEAD` about 40%, the cause is in the
   `d0b4274` start-up path and is fixable without touching feed count; if both are high
   it is load.
1. **Lost frames per seam.** For each feed and game, take
   `round((start_pts[N+1] - end_pts[N]) / frame_period) - 1` from `segments` (a value of
   1 or more is a lost frame). Compare with `ffprobe -select_streams v:0 -show_entries
   packet=pts_time -of csv=p=0 <file>` for the real first/last pts; the offset between
   them is the probe lead (expect 2 frames).
2. **Offset jitter.** Robust sigma of successive differences of `pts_to_session_offset_ns`
   per feed.
3. **Saturation.** Count `recording_branch_saturated` health events and read the
   per-feed `source_fps` / recording fps from telemetry.
4. **The user-visible number.** Simulate stepping through every seam with the real
   `RecordingSegmentReplayStore` + `SegmentDecoder`: for 34 click phases per seam, record
   the displayed frame per click (identify frames by hashing a sequential decode of the
   whole file) and count repeated and skipped frames. Mid-segment windows are the control
   (they were 100% clean).

The scripts used for this document were scratch files and are not in the repo.

## 5. Fix options, and which ones survive scale

| Option | Fixes | Independent of feed count and fps? |
|---|---|---|
| Take segment bounds from the muxer, not a probe: `splitmuxsink-fragment-opened/closed` element messages (location + running time), or the first/last mkv timestamps after close. Use one stable pts-to-session offset per recording. | 2.1 (hole becomes exactly one frame), all of 2.2 | **Yes.** Probe lead, blind window and offset jitter all disappear, so seam errors stop depending on load |
| Step by **frame index** across the chain of segments (+-1 within a file, wrap to the next file's index 0), with the real frame count per segment | Dup/skip regardless of bookkeeping; also 2.3 and the 28 fps case | **Yes**, and best for the Step/jog UI |
| Resolver: treat a hole of up to about 2.5 frames as nearest-frame (split at the midpoint), or give a segment the span `[start, end + frame_period)` | Most of 2.1 at 30 fps | Partly. At 60 fps, or at sigma of 30 ms or more, holes exceed 2.5 frames and it stops helping. Pair it with the first row |
| Decoder: on a failed read return the last good frame; map time to index with real timestamps for variable-rate files | 2.3 | Yes for the stale-frame case |
| Do the DB insert, validator submit and `mkdir` off the format-location callback (post to a worker), and create the new `_pending_segment` before the old one is finalized | Shrinks the blind window | Reduces it; does not remove the 2-buffer lead or jitter |
| Prefetch the next segment's file in `SegmentDecoder`; move decode off the Qt thread | Seam-click hitch, 60 fps playback with many feeds | Needed for 6-8 feeds regardless |

Recommended order: the resolver change is the cheapest visible win at 30 fps; the
muxer-derived bounds plus frame-index stepping are what keep 6-8 feeds and 60 fps from
making it worse. Any change goes through the per-feed seams in `CLAUDE.md`, with
`unittest` cases beside `tests/test_segment_replay_store.py` and the decoder tests.

---

## 6. Segment length: 4 s versus 30 s, and variable-length segments

Date: 2026-11-11. Question asked: what happens if each mkv is extended from 4 s to 30 s,
and can segment length vary? Investigated only; nothing in `app/` was changed. All tests
below used scratch GStreamer pipelines outside the repo (see 6.7 for what that does and
does not cover).

### 6.1 Short answer

- **30 s is a one-line config change** and nothing in the code rejects it, but it badly
  hurts the referee's "review what just happened" workflow: replay can only reach the
  newest *finished* segment, so the lag behind live goes from 0-4 s (average 2 s) to
  0-30 s (average 15 s).
- **It does reduce seam glitches in proportion**: 7.5x fewer seams, so about 7.5x fewer
  glitched clicks and lost frames. Each individual seam is no better (section 2 is
  unchanged).
- **Seek and open cost does not change**, finished-file timing does not change, and
  clock drift stays small. The real costs are the live-edge lag, more footage unindexed
  after a crash, and more memory at risk when the encoder stalls.
- **Segments can vary in length, and already do.** A GStreamer test showed the length
  changing mid-recording and `split-now` / `split-at-running-time` cutting on demand,
  with no frames lost. The most promising design is a long cap plus a cut at Next Play /
  Timeout / Challenge (6.5).

### 6.2 Where the length is set and used

| Place | What it does |
|---|---|
| `settings.py:572-575` | Reads `[recording] segment_duration_seconds` as a float. No range check |
| `pipeline_manager.py:299-300` | Clamps to at least 0.5 s |
| `pipeline_manager.py:1981-1982` | `splitmuxsink max-size-time` |
| `pipeline_manager.py:1343-1378` | Record-queue time cap defaults to one segment's worth |
| `encoder_factory.py:299-319` | `validate_segment_duration`: any value > 0 is valid for MJPEG / ProRes / DNxHR (all intra-frame) |
| `tests/test_playback_controller.py:813` | An 8 s segment case; `tests/test_encoder_factory.py:355` accepts 60 s |

The replay layer (`RecordingSegmentReplayStore`, `SegmentDecoder`) works from each
segment's own recorded start/end session times and has no fixed-length assumption. Only
comments, the status-bar text (`latest finalized -4s`, `r3_app_architecture.md` §15.7 and
the "Phase 7" section) and the default in `app_settings.toml` mention 4 s.

### 6.3 What gets worse at 30 s

**Replay cannot reach the newest segment** (`r3_app_architecture.md` §15.7: coverage is
`[recording_start, end_of_latest_finalized_segment]`). Everything that anchors on the live
edge inherits the lag:

| Feature | Code | At 4 s | At 30 s |
|---|---|---|---|
| Pause from LIVE freezes on the newest finalized frame | `playback_controller.py:965-979` | up to 4 s stale | up to 30 s stale |
| Rewind 10 s from LIVE counts back from that edge | `:981-1010` | 10-14 s behind now | 10-40 s behind now |
| Stepping / slow-motion from LIVE snaps to that edge | `:632-641` | same | same |
| First replay after Begin Game needs one finalized segment | `:1282-1289` (`replay_available`) | up to 4 s | up to 30 s |
| Challenge review: the fence end is clamped to the last finalized frame | `:647-650`, `:955-963` | a just-finished play waits up to 4 s | waits up to 30 s; a play shorter than the open segment cannot be reviewed at all until it closes |
| `live_lag_behind_replayable_seconds`, status-bar "latest finalized -Ns" | `:1319-1333` | up to 4 s | up to 30 s |

The wait for a play's last frame to become reviewable is the time to the next segment
boundary: uniform over 0 to the segment length. This is the largest functional cost, and
the same effect `NDI_OPEN_BUGS.md` §4 already lists for the seek bar at the end of a
just-finished play, made 7.5x bigger.

**More footage unindexed after a crash.** Database rows exist only for finalized
segments, so up to one segment of footage has none. Tested by killing a recording
process mid-segment (a crash, not a power cut):

| Segment | Killed at | Readable afterwards | Seekable |
|---|---|---|---|
| 4 s | 2.5 s in | 70 frames (2.3 s) | yes |
| 30 s | 18 s in | 535 frames (17.8 s) | yes, 19 ms per seek at 0-17 s |

The bytes survive, but the app's recovery validator would probably reject the file:
with an unbounded live-style source, cv2 reports a garbage `FRAME_COUNT` for an
unfinished file (observed `-276701161105643264`), and the validator treats
`frame_count <= 0` as corrupt (`session_recovery.py:358-363`), which moves the file to
`quarantine/` (`:222-226`). Nothing is deleted, but up to 30 s instead of 4 s would be
quarantined. A recovered "dirty" row also carries no session time (`start_pts_ns=0`,
`:297-337`), so it is not replay-addressable. Caveat: tested with a synthetic live
source (`videotestsrc is-live=true`), not NDI.

**More memory at risk if the encoder stalls.** The record queue is non-leaky, sits
before `videoconvert` so it holds raw BGR frames, and is capped by the smaller of 256
buffers (`settings.py:62-63`) and one segment of time:

| | Cap at 30 fps | Per feed, 720p (2.64 MiB/frame) | 8 feeds | Per feed, 1080p (5.93 MiB/frame) |
|---|---|---|---|---|
| 4 s | 120 buffers (time cap binds) | 316 MiB | 2.5 GiB | 712 MiB |
| 30 s | 256 buffers (buffer cap binds) | 675 MiB | 5.3 GiB | 1.48 GiB |

At 60 fps the 4 s cap is 240 buffers, so it is already near 256 and nothing changes.
Related, by arithmetic and not observed: the `recording_branch_saturated` alarm trips at
75% of 256, i.e. 192 buffers, for two ticks (`telemetry.py:810-817`; depth is counted in
buffers). At 4 s and 30 fps the queue stops at 120 (47%), so the alarm cannot trip; at
30 s it can. It never fired in sessions 010-022 (`health_events.jsonl`).

### 6.4 What gets better, and what stays the same

**Better.** Seam events scale with 1 / segment length, and so do the glitches they cause.
Using the per-seam figure from section 3.3.2 (1.27 glitched clicks per seam):

| Segment length | Average lag behind live | Worst lag | Seams per feed-hour | Chance a given tile glitches on a click | 8 feeds: chance some tile glitches |
|---|---|---|---|---|---|
| 4 s (today) | 2 s | 4 s | 900 | 1.06% | 8.2% |
| 8 s | 4 s | 8 s | 450 | 0.53% | 4.2% |
| 15 s | 7.5 s | 15 s | 240 | 0.28% | 2.2% |
| 30 s | 15 s | 30 s | 120 | 0.14% | 1.1% |

These are arithmetic from measured per-seam values, assuming the seams of different
feeds do not coincide. File and database-row counts drop by the same factor, and the
~85 ms next-file-open hitch (`NDI_OPEN_BUGS.md` §3) happens every 30 s instead of 4 s.

**The same.** Measured on a 30 s versus a 4 s file written by the app's record-chain
shape:

| | 4 s file | 30 s file |
|---|---|---|
| Frames / size (synthetic ball, 720p30) | 120 / 1.9 MB | 900 / 14.0 MB (real content: 5-15 MB per 4 s) |
| Open + first read | 6-20 ms | 6-20 ms |
| Seek + read, median / p95 (40 random offsets) | 18.9 / 20.1 ms | 19.6 / 21.3 ms |
| Seeks landing on the wrong frame | 0 of 40 | 0 of 40 |
| Has an index (Cues) | yes | yes; real production files too |
| File complete after the rotation point where the app adds it to the index | 9.5-19 ms | 21-24 ms |

So a longer file does not widen the window in which the app has indexed a segment whose
file is not yet closed (the "no video, time keeps counting" case noted at
`pipeline_manager.py:546`), at least with one feed on an idle disk.

**Clock drift stays small.** `end_session_time_ns = last_pts + one offset` sampled at the
segment start (`pipeline_manager.py:2338-2342`) assumes the source and session clocks
advance together. On the recorded runs the offset moves by -1.3 to +4.3 ms per 30 s
(median magnitude 1.3 ms), so a 30 s segment ends up to about 0.13 frame off at the
far end. This adds a little to the jitter in section 3.2.

### 6.5 Variable-length segments

**Already happening.** The last segment of every game is cut short by `split-now` at Stop
(`pipeline_manager.py:555-615`): in session_022 `segment_00068.mkv` has 30 frames (1.04 s).
First segments differ by a frame, and a 28 fps feed gives 109-113 frames per segment.
Replay copes with all of these.

**Tested on GStreamer 1.28.1**, the app's chain shape (BGR to I420 bt601, `jpegenc
quality=85`, `splitmuxsink muxer-factory=matroskamux async-finalize=true`), 62 s of
720p30, one run:

| Segment | Length | Frames | How it was cut |
|---|---|---|---|
| 1 | 4.00 s | 120 | normal rotation |
| 2 | 4.00 s | 120 | normal rotation |
| 3 | 30.00 s | 900 | `max-size-time` set to 30 s **while PLAYING** at 10 s; the open segment (begun at 8 s) ran to 38 s |
| 4 | 1.93 s | 58 | `split-now` at 40 s |
| 5 | 10.07 s | 302 | `split-at-running-time(50 s)` scheduled at 39.8 s |
| 6 | 12.00 s | 360 | rest of the stream |

The frames add up to 1860 (62 s x 30): none lost, every file has its index.

**One caution.** `gst-inspect-1.0 splitmuxsink` documents `max-size-time` as "changeable
only in NULL or READY state", yet it took effect while PLAYING. Do not build on that.
The `split-now`, `split-after` and `split-at-running-time` signals are the supported
runtime controls, and the app already rebuilds the element at every Start
(`_rebuild_splitmuxsink_locked`, `pipeline_manager.py:1998`), which is the natural place
to apply a per-game length.

**Designs, none built:**

| Design | Gives | Costs |
|---|---|---|
| Per-game length, applied at the Start rebuild | Different length for e.g. practice vs match | Trivial; does not fix the live-edge lag |
| **Long cap (e.g. 30 s) plus `split-now` at Next Play, Timeout and Challenge** | Few seams normally, and the end of each play is replayable within about 0.1-0.3 s, the same mechanism the Stop path uses | The end of the reviewed play then sits exactly on a seam, so it meets the section 2 bugs where the referee looks. New code must not block the Qt thread (Stop sleeps 0.3 s, `:589`) and must keep the post-split segment (Stop discards it, `:601-603`) |
| Short segments near live, merged into long ones in the background (`ffmpeg -c copy`) | Low lag and few old files | Rows and index entries must be swapped, and Windows will not let a file be replaced while `SegmentDecoder` holds it open. Not tested |

If the goal is only fewer seam glitches, the section 5 fixes remove the cause for any
length; changing the length alone trades the glitch for a worse live edge.

### 6.6 What the recorded data says about the current length

Nothing in `C:\SportsReplay` uses any other length: of the 1181 complete segments in the
database none is longer than 4.10 s, 1165 are 3.0-4.2 s, and the 16 shorter ones are
game-ending or partial cuts. So the 8 s and 30 s figures above come from the arithmetic
and the scratch tests, not from app recordings.

### 6.7 Method, and what was not measured

- Scratch scripts (not in the repo) recorded synthetic video through a standalone
  GStreamer pipeline shaped like the app's record branch, for fixed 4 s, fixed 30 s and
  variable lengths; read the files back with `ffprobe` and `cv2`; and timed seeks with
  `CAP_PROP_POS_MSEC`. The crash test used `Process.kill()` on a live (real-time) pipeline.
  File-completion timing used `splitmuxsink-fragment-closed` bus messages against the
  `format-location` callback.
- The drift figures come from `pts_to_session_offset_ns` in `metadata.db`, read-only.
- **Not measured:** the real NDI chain, more than one feed, disk load, power loss (only a
  process kill), post-session export with 30 s files (`long_form_export`,
  `clips_json_export`; no 4 s constants were found in them), or the UI strings.

---

## 7. Recommended fix for the timing jitter

Date: 2026-11-11. Recommendation only; nothing is implemented. It builds on sections 2-6
and on three new experiments (7.2) run with scratch GStreamer pipelines outside the repo.

### 7.1 The root cause, stated precisely

Every segment's position in session time is **one reading of `time.monotonic_ns()`,
taken when the segment's first buffer happens to reach a Python pad probe**
(`_on_record_encoder_buffer_probe`, `pipeline_manager.py:2268-2300`; `SessionClock` is
`time.monotonic_ns()`, `session_clock.py:44`). That reading includes however late the
probe ran, which depends on CPU load. Separately, which frames belong to which segment
is decided by **when** the `format-location` callback swaps `_pending_segment`
(`:2181-2266`), not by the frames' timestamps. Those two choices produce the per-segment
offset jitter, the 2-frame lead and the blind-window frame loss of section 2.2.

The data needed to avoid both already exists inside GStreamer: every buffer carries an
exact timestamp on the pipeline's timeline, and `splitmuxsink` reports exactly which
buffer starts and ends each file.

### 7.2 New measurements

**E1 — GStreamer's clock and Python's clock are the same counter.** On this machine:

| | |
|---|---|
| `time.monotonic()` (Python 3.14.3) | `QueryPerformanceCounter()`, 100 ns resolution |
| `GstSystemClock` | `clock-type=monotonic`, 100 ns resolution |
| Offset between them | 0.0001 ms, unchanged after 10 s (zero drift); paired-read bracket 0.8 us |

So a buffer's `base_time + running_time` *is* a `time.monotonic_ns()` value, whenever
the pipeline runs on the system clock. Converting a frame timestamp to session time needs
no sampling at all. (On Python 3.12 and earlier, `time.monotonic()` on Windows was
`GetTickCount64()` with ~15.6 ms resolution; `perf_counter_ns()` is QPC on all versions.)

**E2 — the error inside today's sample.** A live 720p30 pipeline shaped like the record
branch (`queue ! videoconvert ! I420 ! jpegenc`), measuring how late a Python probe on the
encoder's src pad sees each buffer relative to that buffer's own timestamp. That lateness
is exactly what today's per-segment offset absorbs:

| Load | Median | Sigma | p95 | Max |
|---|---|---|---|---|
| Idle | 7.8 ms | 1.4 ms | 9.8 ms | 12.0 ms |
| 8 parallel 720p JPEG encoders (CPU) | 22.3 ms | 9.2 ms | 35.9 ms | 44.3 ms |
| 6 Python threads at ~20% duty (GIL) | 5.7 ms | 1.5 ms | 9.3 ms | 12.1 ms |
| Both | 23.4 ms | 8.6 ms | 40.8 ms | 52.9 ms |

The jitter at a seam is the *difference* of two such samples, so its sigma is about
sqrt(2) x sigma: about 1.9 ms idle and 13 ms under the CPU load. That is consistent with
the 1.4 ms (one feed) and 13.1 ms (two feeds) measured in the recordings (section 3.2).
**CPU contention, not the GIL, drives it**, which is why it grows with feed count and fps.

**E3 — splitmuxsink already reports exact bounds.** Live 720p30, 4 s fragments, with the
app's probe-and-swap bookkeeping reproduced (including a SQLite insert + commit inside
the callback, as `_finalize_pending_segment_locked` does), compared with the frames in
each file (`ffprobe`):

| Source of the fragment bounds | Error vs the file (idle) | Error vs the file (8-encoder load) |
|---|---|---|
| Today's design: probe + swap in `format-location` | head +2 frames (+1 on the first fragment), tail +2 frames | same, plus one fragment at head +3 / count -1 (a lost frame) |
| `format-location-full`'s first sample (PTS) | +0.085 ms on every fragment | -0.118 ms on every fragment |
| `splitmuxsink-fragment-opened` message, `running-time` | +0.085 ms | -0.118 ms |
| `splitmuxsink-fragment-closed` message, `running-time` vs *end* of the last frame | -0.25 ms | -0.45 ms |

The residuals are the mkv's 1 ms timestamp rounding, not errors. So the experiment
reproduces the recorded symptoms (the constant 2-frame lead and the occasional lost frame
under load) **outside the app**, and shows GStreamer's own reports are exact under the
same load. `fragment-closed` gives the exclusive end (end of the last frame), so
consecutive fragments meet exactly. One exception: a fragment closed by EOS (Stop)
reported the *start* of its last frame, so the final segment needs last PTS + one frame
duration.

**E4 — which clock the NDI pipelines run on (verified 2026-11-11).** `gst-inspect-1.0
ndisrc` says the element *can* provide a clock, so this needed a real sender. Test: the
app's own `NDIReceiver` chain (`timestamp-mode=receive-time-vs-timestamp`, target
1920x1080 at 30 from `app_settings.toml`) plus a record branch shaped like the app's,
receiving NDI 6 Tools Test Patterns (`DAVIDI7 (Test Pattern)`, UYVY 1920x1080, declared
30/1) on this PC, 60 s, letting GStreamer pick the clock as the app does:

| | Result |
|---|---|
| Pipeline clock | **`GstSystemClock`** (the singleton), `clock-type=monotonic`, 100 ns, no master |
| `ndisrc.provide_clock()` in this mode | none |
| Pipeline clock minus `time.monotonic_ns()` | 0.0001 ms at start and after 59.6 s; drift 0.001 ppm; paired-read bracket 1.6 us |

So with the app's timestamp mode the NDI pipelines run on the system clock, which is the
same counter as `SessionClock` (E1). Step 3 of 7.4 is not needed. If a feed's
`ndi_timestamp_mode` is ever set to another value (for example `clocked`), repeat the
check, since `ndisrc` may then provide its own clock.

The same run exposed a second hazard. The Test Patterns "Crosshatch" stream declares
30/1 but delivered **one frame per second** (57 frames in 60 s; `videorate` in=57,
out=1798, duplicate=1740). `videorate` can only emit the duplicates once the next real
frame arrives, so they reached the encoder in bursts, **0.4-4.3 s late** (median 758 ms).
Any static or slow NDI source (a still image, a paused player) would therefore throw
today's per-segment offset sample off by up to seconds. The step 1 fix is not affected,
because it uses the frames' timestamps, not their arrival. This run is not usable for
live-rate timing; repeat with a moving source (a moving Test Patterns pattern, VLC
playing a video, or a camera). Done in E5.

**E5 — a moving 29.97 fps source (2026-11-11).** Same chain and record branch as E4,
with `splitmuxsink-fragment-opened` / `-closed` messages captured as well. The source
was VLC on this PC playing a 29.97 fps video over NDI (`DAVIDI7 (VLC)`, UYVY 1920x1080
bt709, declared 30000/1001). The target was 1920x1080 at `target_fps = 29.97` (caps
2997/100) and the run lasted 120 s (31 fragments). Because sender and receiver share one
machine, there is no network in this path.

| | Result |
|---|---|
| Pipeline clock | `GstSystemClock` again; minus `time.monotonic_ns()` 0.0001 ms, drift 0.000 ppm over 119.7 s |
| Source rate, fitted to the NDI timestamps of 3,463 frames | 29.970037 fps: 0.2 ppm from 30000/1001, 1.2 ppm from the 2997/100 target; no missing frames (no timestamp gap over 1.5 frames) |
| NDI frame arrival interval (at `videorate`'s sink) | median 33.29 ms, sigma 2.6 ms, p1-p99 27.6-39.5 ms, max 57.1 ms |
| NDI timestamp step (`receive-time-vs-timestamp`) | median 33.34 ms, sigma 0.93 ms, p1-p99 31.4-35.5 ms, range 24.0-43.4 ms |
| `videorate` after start-up | 0 drops, 0 duplicates (all 121 duplicates were at start-up, below) |
| Today's sample: encoder-src lateness at each segment's first frame, steady state (29 segments) | median 31.1 ms, sigma 3.3 ms (1.4 ms without one 47.6 ms outlier); seam-to-seam change sigma 4.6 ms; **1 of 28 seams moved by more than half a frame** (17.1 ms) |
| Proposed: `fragment-opened` running-time vs `format-location-full`'s first sample | 0.0000 ms on all 31 fragments |
| Proposed: next fragment's first sample minus `fragment-closed` running-time | **0.0000 ms on all 29 seams**: no hole, no overlap |
| Proposed: `fragment-closed` minus (last frame + 1/29.97 s) | 0 ± 0.05 ms (the files' 100 us timestamp step) |
| File first timestamp vs first sample, `timecodescale` 100 us | at most 0.049 ms; 3,240 of 3,600 frames carry sub-millisecond timestamps; `cv2` seeks work |
| Segment length | 119 frames = 3,970.6 ms (a 120th frame would end at 4,004 ms, past `max-size-time`) |
| Cost | process 0.77 cores; 26.8 MB per segment = 6.75 MB/s, about 24 GB per hour for one 1080p feed |

What it shows:

- **Clock:** confirmed with a moving source. Step 3 of 7.4 is not needed.
- **Today's design** (one feed, 1080p, idle machine): 1 seam in 28 got an offset error
  larger than half a frame, the same 3.6% as the one-feed recordings (section 3.2). The
  lateness is about 31 ms at 1080p against 7.8 ms at 720p (E2), because the 1080p
  conversion and encode take longer. So today's session times run about one frame
  behind capture.
- **The proposed bounds are exact on a live source at a non-integer rate:** consecutive
  fragments meet to the nanosecond, and `fragment-closed` is the exclusive end. The E3
  exception repeated: the last fragment, closed by EOS, reported the *start* of its last
  frame (120,086.75 ms; the file's last frame is at 120,086.8 ms).
- **Source jitter does not reach the files.** `videorate` puts every output frame on its
  own exact 2997/100 grid. Consecutive NDI timestamps differ from the frame period by at
  most about 10 ms (steps of 24.0-43.4 ms). That was never enough to push a frame past
  the half-frame point (16.7 ms) where `videorate` drops or duplicates. Arrival
  jitter (sigma 2.6 ms) is about 3 times larger than timestamp jitter (sigma 0.93 ms), so
  `receive-time-vs-timestamp` smooths most of it.
- **`target_fps` matched to the source works.** At 29.97, the 1.2 ppm left over (the
  2997/100 rounding, 8.2 item 2) is one dropped or duplicated frame in about 7.7 hours.
  With `target_fps = 30` the same source would get a duplicate about every 33 s. A camera
  with its own oscillator will be some ppm off this PC's clock whatever the setting, so
  expect an occasional single-frame slip there; the `videorate` telemetry shows it.
- **Start-up burst (scratch pipeline only).** `ndisrc` took about 4 s from PLAYING to its
  first frame. `videorate` (default `skip-to-first=false`) then emitted 121 copies of that
  frame at once to fill back to running time 0. The record queue peaked at 120 buffers
  (about 712 MiB of 1080p BGR) and the first two fragments reached the encoder 4.1 s and
  1.7 s late. This does not reach the app's recordings: a new pipeline starts with the
  record valve closed, and recording is only enabled once video is flowing
  (`IMPLEMENTATION.md` §5.8). The preview path still gets the burst, and its leaky
  queues drop it.

### 7.3 How this is done outside this app

- **GStreamer's synchronisation model:** a buffer's clock time is `base_time +
  running_time`, and running time comes from the PTS through the pad's segment.
  Pipelines that must line up share one clock and one base time
  (`gst_pipeline_use_clock`, `gst_element_set_base_time`, with `start-time` set to
  NONE). That is the standard way to keep several capture pipelines on one timeline.
- **splitmuxsink is built to be indexed this way:** `format-location-full` hands over the
  first buffer of each new file, and the `splitmuxsink-fragment-opened` /
  `-closed` element messages carry each file's running-time bounds. GStreamer's own
  `hlssink2` builds its playlist segment durations from those messages (from GStreamer's
  source; not re-checked here).
- **Segmented streaming formats never sample a wall clock per segment.** HLS `EXTINF`
  durations and MPEG-DASH `SegmentTimeline` entries (`t`, `d`, with `t[n+1] = t[n] +
  d[n]`) come from media timestamps, so segments are contiguous by construction.
- **Broadcast replay servers index by frame count or timecode against a common
  reference** (genlock, PTP), not by when software noticed a frame. NDI carries a
  sender timecode and timestamp per frame, and `ndisrc` can use them (`timestamp-mode`
  `timecode` / `timestamp`); cross-camera alignment then depends on the senders' clock
  sync. The app moved to `receive-time-vs-timestamp` for the low-fps problem
  (`NDI_LOW_FPS_INVESTIGATION.md`), which is compatible with everything below.

### 7.4 Recommendation, in order

**Step 0 — check the clock. Done for the NDI Test Patterns sender (7.2 E4) and a moving
29.97 fps VLC sender (7.2 E5): the system clock, identical to `time.monotonic_ns()`;
step 3 is not needed.** Still worth keeping
as a one-line log at PLAYING (`pipeline.get_clock()`'s type, and the paired-read offset
to `time.monotonic_ns()` at start and after a minute), and confirming once on a POV
camera. Also pin Python 3.13 or later (or move `SessionClock` to `perf_counter_ns`) so
session time never falls back to 15.6 ms ticks.

**Step 1 — derive segment times from GStreamer's timeline, not from a probe sample.**
This is the fix.

1. **Start** of each segment = running time of the first sample passed to
   `format-location-full` (connect it instead of `format-location`; the filename logic
   stays the same). `splitmuxsink-fragment-opened` gives the same value.
2. **End** (exclusive) = `running-time` of `splitmuxsink-fragment-closed`. For the
   segment closed at Stop, use last PTS + one frame duration (E3 exception).
3. **Session time** = running time + `(base_time - session_start_monotonic_ns)`: **one
   constant per pipeline generation**, computed once at PLAYING and again after a
   rebuild (new base time), not once per segment. With the system clock (E1) this is
   exact. `SessionClock.rebase` keeps working, because it only moves
   `session_start_monotonic_ns`.
4. **Frame membership by timestamp, not timing.** If the probe is kept for frame counts,
   attribute each buffer by comparing its PTS with the known start of the next
   fragment. Better, take the count from the closed file in the validator, which
   already opens every closed segment.
5. **Insert the row and index entry when `fragment-closed` arrives** (the bus thread
   already receives it for validation, `IMPLEMENTATION.md` §7.2) instead of inside
   `format-location`. The file is complete at that point, closing the "indexed before
   the trailer is written" window (`pipeline_manager.py:546`). The SQLite commit also
   leaves the streaming thread, and with it the blind window.

Result: consecutive segments of a feed meet exactly (`start[N+1] == end[N]`), and the
bookkeeping error is zero by construction, whatever the CPU load, feed count or frame
rate. That removes the 2-frame lead, the blind-window loss and the per-segment offset
jitter (section 2.2), and the scaling of section 3 with them. The ~8-23 ms
encode-and-probe latency in E2 disappears too, so a segment's session time means the
frame's capture time rather than when the encoder finished it.

**Step 2 — make replay use the exact bounds** (section 5 rows 2-4).
`nearest_frame_location` treats segments as half-open `[start, end)`, so no hole exists
to freeze on. `SegmentDecoder` positions by container timestamp rather than
`offset x declared fps`, which also fixes the 28 fps feed of 2.3. `step_frames` steps
by frame index across segments. These are small once step 1 makes the bounds right.

**Step 3 — only if step 0 shows ndisrc's clock (it did not, 7.2 E4).** Either have every pipeline
`use_clock(Gst.SystemClock.obtain())`, which then also allows a shared base time
(step 4), or keep ndisrc's clock and map it to `time.monotonic_ns()` with paired clock
reads at PLAYING, re-checked every few seconds and fitted for drift. Paired clock reads
are accurate to about 1 us (E1), unlike buffer-arrival samples. The first option must be
re-tested against the low-fps problem in `NDI_LOW_FPS_INVESTIGATION.md`, because
`ndisrc` computes `receive-time-vs-timestamp` PTS against the pipeline clock.

**Step 4 — optional: one timeline for all feeds.** With a shared clock and base time,
every feed's running time is directly comparable, and `split-at-running-time` can cut
all feeds at the same session times (section 6.5 tested it). Seams then line up across
feeds instead of forming a zone (section 3.3 item 3). This is worth doing only after
steps 1-2, when seams no longer glitch and only the next-file-open hitch is left.

**Schema.** `start/end_session_time_ns` and `pts_to_session_offset_ns` already exist, but
an *exclusive* end changes the meaning of `end_*`. Mark new rows (a column added in
`MetadataDb._initialize_schema`, per `CLAUDE.md`), so old inclusive-end rows keep
resolving the old way.

### 7.5 What this does and does not fix

| Problem | After steps 1-2 |
|---|---|
| Per-segment offset jitter (1.4 ms one feed, 13 ms two feeds, worse with load) | Gone: no per-segment sample |
| 2-frame probe lead; replay ~2 frames behind true session time | Gone: bounds come from splitmuxsink |
| Blind-window frame loss (3.6% / 40% of seams) | Gone: no swap timing, no DB work in the streaming path |
| One-frame hole between segments; repeated / skipped frames at seams | Gone with half-open intervals |
| Past-end reads (first segment, 28 fps feed) | Gone with exact durations and timestamp positioning |
| Growth with 6-8 feeds and 60 fps (section 3) | The bookkeeping part is gone; CPU capacity limits (section 3.2) remain |
| Real timing irregularity of the source (network, NDI receive) | Not affected: it is in the PTS, and those are the true frame times |
| Constant latency differences between cameras | Not affected; calibrate per camera if cross-camera sync matters |

### 7.6 How to verify it

1. Unit tests (`unittest`) that feed recorded PTS sequences and fragment messages into
   the bookkeeping and check `start[N+1] == end[N]`.
2. On a new recording, the section 2.2 file audit should read head 0 / tail 0 / count 0
   on every segment, and every DB gap should be exactly one frame.
3. The section 1 step simulation over every seam and 34 click phases should show 0
   repeated and 0 skipped frames.
4. Two-, four- and eight-feed runs under load (section 4) should show zero offset change
   between consecutive segments of a feed.
5. A real-camera run for step 0 and, if needed, step 3.

### 7.7 Alternatives considered and rejected

| Alternative | Why not |
|---|---|
| Smooth the per-segment offset (median of recent segments) | Hides some jitter, lags real changes, and leaves the 2-frame lead and lost frames |
| Raise thread priority or pin CPU affinity for probe threads | Shrinks the lateness but never removes it; the cause is sampling at arrival |
| Read first/last timestamps back from each closed mkv | Exact, but extra I/O and a delay before replay; useful as the validator's check, not as the source |
| Move the probe after splitmuxsink's internal queue | With `async-finalize` the muxer is created per fragment; fragile, and still timing-based |
| Switch `ndisrc` to sender `timecode` / `timestamp` mode | Depends on sender clocks; coarse timecodes were the low-fps cause; does not fix the bookkeeping |

### 7.8 How often a repeat or skip is still expected after the fix

Date: 2026-11-11. Question asked: with the section 7 changes, how likely is a glitch,
and how often? **Nothing here is built yet**, so the "after" figures are predictions
from the 7.2 measurements, not results from the app. Section 7.6 is how to confirm them.

**Short answer:** stepping across a seam should become clean every time. What can still
show a repeated or skipped frame is a real duplicate or gap *inside the recording*, made
by `videorate` before anything reaches the files. Each one is a single frame, it can sit
anywhere in a segment, and it looks like one repeated click (or one two-frame jump), not
the "repeat 2-3, then skip" pattern of section 1. With your current one-feed setup that
is about once every 7.7 hours. With real cameras it is an estimated once every 10-55
minutes per camera, still to be measured.

**Stepping, today against after steps 1-2:**

| | Today | After the fix |
|---|---|---|
| Stepping inside a segment | Clean (168 of 168 runs, section 1) | Clean |
| Crossing a seam (one about every 120 clicks at 30 fps) | **About 65% of crossings glitch**: same frame for 1-3 clicks, then 0-2 frames skipped (section 1) | **Expected 0%**: segments meet exactly, and stepping goes by frame index, not time |
| Segment timing error | One feed: offset sigma 1.4-3.3 ms, 1 seam in 28 off by more than half a frame (7.2 E5), 3.6% of seams lose a frame (3.2). Two feeds: sigma 13 ms, 40% of seams lose a frame (3.2) | 0.0000 ms at all 29 seams with a moving 29.97 fps source (7.2 E5); under 0.5 ms (the file's 1 ms rounding) with 8 extra encoders loading the CPU (7.2 E3) |
| Replay lags true session time | About 2 frames (probe lead), plus about 31 ms of encode latency at 1080p | None: times are the frames' capture timestamps |

**What can still repeat or skip a frame after the fix:**

| Cause | How often, per feed | Basis |
|---|---|---|
| Source and target rates differ slightly: VLC on this PC, `target_fps = 29.97` | 1 frame in about 7.7 hours | Measured, 7.2 E5: 1.2 ppm apart (mostly the 2997/100 rounding, 8.2 item 2) |
| A camera's clock against this PC's clock | About 1 frame every 11-55 minutes if they are 10-50 ppm apart, so a 0.3-1.5% chance inside a 10-second review | **Estimate.** 10-50 ppm is a typical spread for quartz clocks, not measured here; the camera rate as seen by this PC is what counts |
| `target_fps` not matching the camera (30 against 29.97, or 60 against 59.94) | 1 duplicate every 33 s (30 fps) or 16.7 s (60 fps); the reverse mismatch drops instead | Arithmetic; avoided by matching `target_fps` (8.2 item 2) |
| Frames lost by the network or the NDI receiver | Unknown. None in E5, but sender and receiver were on the same PC | Not measured over a real network |
| More feeds, size or rate than the PC can encode | Constant, once the record queue backs up | Limits in 8.3: about 4 feeds at 1080p30, 8 at 720p30 on this PC |
| Timestamp spread at a 60 fps target | Possible, rare | Not measured; the half-frame margin drops from 16.7 ms to 8.3 ms (8.2 item 4) |
| A still or paused source | Repeats for as long as the source is still | Correct behaviour: the picture really is not changing (7.2 E4) |

The rate-mismatch rows exist today too; the seam glitch hides them. The ingest
`videorate` counters already in telemetry (`rate_dropped_per_sec`,
`rate_duplicated_per_sec`) show the real rate for each camera.

**Not covered by the fix:**

- **Recordings made before the fix** keep today's behaviour. Only new rows carry the
  exclusive-end marker (7.4, "Schema"), so older rows resolve the old way.
- **A feed that drops out and reconnects mid-game** (`IMPLEMENTATION.md` §5.8) leaves a
  real gap in the footage, which shows as a jump.
- **Constant latency differences between cameras** are an offset between tiles, not
  jitter (7.5).

To replace the estimates with measured numbers, run the E5 check against a real camera
over the network, and for the time of a match, and read the `videorate` counters.

---

## 8. 1080p60 NDI cameras: effect on the fix, and what to set in `app_settings.toml`

Date: 2026-11-11. Question asked: how does a 1920x1080 at 60 fps NDI feed change the
section 7 recommendation, and should `app_settings.toml` be changed to match? Advice
only; nothing in `app/` or `app_settings.toml` was changed.

Two things need to be kept apart:

- **What the camera sends**: here, 1080p60 over NDI.
- **What the app records**: `target_frame_width`, `target_frame_height` and `target_fps`
  in `[app]` (today 1280, 720, 30). `ndi_receiver.py:209-263` scales and rate-converts the
  camera to that target ahead of the `tee`, so the preview, the recording, the step size
  (`frame_period_ns`, `application_coordinator.py:159-166`) and the seam structure all
  follow the target, not the camera.

### 8.1 Short answer

- **The section 7 fix does not change**, and it matters more at 60 fps. Today's timing
  errors are milliseconds (section 7.2), and a 16.7 ms frame holds twice as many frames
  of them as a 33.3 ms one (section 3.4, case B). The fix makes them zero at any
  resolution or rate.
- **Four additions for 60 fps** (8.2).
- **Do not set the target to 1920x1080 at 60 yet.** On this PC the record stage cannot
  keep even one 1080p60 feed going with headroom (8.3), and the clock check of section
  7.4 step 0 does not need the change. Keep 1280x720 at 30 for now.

### 8.2 Additions to the recommendation for 60 fps

1. **Finer file timestamps.** `matroskamux` stores timestamps in steps of its
   `timecodescale` property, default 1,000,000 ns (1 ms). A 60 fps frame lasts 16.667 ms,
   so file timestamps alternate 16 / 17 ms. Setting `timecodescale` to 100,000 (100 us)
   through `splitmuxsink`'s `muxer-properties` keeps them exact to 0.1 ms. It matters only
   for section 7.4 step 2, where the decoder positions by container timestamp; the
   bookkeeping itself uses GStreamer's nanosecond values. **Verified 2026-11-11** in the
   E4 run (`muxer-properties = "properties,timecodescale=(gint64)100000"`): every file's
   first timestamp matched `format-location-full`'s first sample to 0.0000 ms, 80 of 120
   frames per file carried sub-millisecond timestamps (the other 40 fall on whole
   milliseconds at 30 fps), and `cv2` seeks worked. E5 repeated it at 29.97 fps: first
   timestamps within 0.049 ms (the 100 us step), 3,240 of 3,600 frames sub-millisecond.
   Not yet applied in the app: it is a
   one-property change in `_build_splitmuxsink_element` (`pipeline_manager.py:1971-1996`).
2. **Match `target_fps` to the camera's exact rate.** Many "60 fps" sources are 59.94
   (60000/1001). With a 59.94 camera and `target_fps = 60`, `videorate` inserts a
   duplicate frame about every 16.7 s (a 0.06 fps mismatch), which looks like a stuck
   frame when stepping but has nothing to do with seams. The reverse mismatch drops a
   frame at the same interval. `target_fps = 59.94` becomes `2997/50` through
   `Fraction(...).limit_denominator(1000)` (`ndi_receiver.py:255`), off from 60000/1001 by
   one frame in about 4.6 hours. The ingest `videorate` drop / duplicate rates are
   already in telemetry (`rate_dropped_per_sec`, `rate_duplicated_per_sec`) to confirm
   what the cameras send. The same holds at 30 fps: in E5 (7.2) a 29.97 source with
   `target_fps = 29.97` gave 0 drops and 0 duplicates over 115 s, where `target_fps = 30`
   would have duplicated a frame about every 33 s.
3. **Drop and scale before converting.** The NDI chain is `videoconvert ! videoscale !
   videorate ! capsfilter` (`ndi_receiver.py:320-322`), so every full-size frame is
   converted to BGR at the camera's rate before anything is dropped or shrunk. Measured,
   1080p60 UYVY input to a 1280x720 at 30 BGR target, one thread:

   | Element order | Input fps one thread can take | One 60 fps camera uses |
   |---|---|---|
   | Today: convert, scale, rate | 71 | 84% of a thread |
   | Rate, scale, convert | 103 | 58% of a thread |

   Both figures include the synthetic source's own cost, so the real saving in
   conversion work is larger. This applies at today's 720p30 target, as soon as the
   cameras send 1080p60, and it adds up across 6-8 cameras.
4. **Run the step 0 clock check with a 1080p60 sender** where possible (Test Patterns set
   to 1080p60, or a 1080p60 clip in VLC). Which clock the pipeline uses will not depend on
   it, but the timestamp cadence at 60 fps might. The first run (7.2 E4) used a Test
   Patterns stream that delivered 1 frame per second. The second (7.2 E5) used a moving
   1080p 29.97 source: NDI timestamp step sigma 0.93 ms, steps 24.0-43.4 ms, and no drops
   or duplicates. At 60 fps the half-frame margin halves to 8.3 ms, which a spread like
   that might occasionally exceed, so a 60 fps cadence run is still needed.

### 8.3 What this PC can record

Maximum sustained fps for one feed, idle machine, software `jpegenc` (`qsvjpegenc` fails
to link here; see the note at the top of this doc), 1200 synthetic frames per run:

| | Ingest stage (UYVY to BGR) | Record stage (BGR to I420 bt601 + `jpegenc quality=85`) |
|---|---|---|
| 1280x720 | 177 fps | 135 fps |
| 1920x1080 | 82 fps | **61 fps** |

The two stages run on different threads (the `ndisrc` streaming thread and the record
queue's thread, see the threading notes in `IMPLEMENTATION.md` §4.1), so a feed is limited
by the slower one. Combined with the many-feed encode totals in section 3.2 (about 440 fps
at 720p and 180 fps at 1080p across all feeds):

| Target in `[app]` | Feasible on this PC (encode only) |
|---|---|
| 1280x720 at 30 (current) | Up to 8 feeds |
| 1280x720 at 60 | About 4 feeds; 6 is tight (81% of the total); 8 is over |
| 1920x1080 at 30 | About 4 feeds; 6 is at the limit |
| 1920x1080 at 60 | **Not even 1 feed with headroom** (60 needed of 61 possible); 2 or more cannot keep up |

Other costs of a 1920x1080 at 60 target:

| | 1080p30 | 1080p60 |
|---|---|---|
| Disk per feed | 7.0 MB/s (measured, session_010: 28.1 MB per 4 s; 6.75 MB/s in 7.2 E5, about 24 GB per hour) | about 14 MB/s; 8 feeds about 112 MB/s (budget default 200 MB/s) |
| Record-queue memory if the encoder stalls (section 6.3) | up to 712 MiB per feed (120 buffers) | up to 1.48 GiB per feed (256 buffers) |
| Replay, 8 feeds (section 3.2) | Stepping fine; about 16.5 ms decode per click | Stepping fine; 60 fps *playback* exceeds what the UI thread can decode and draw |

### 8.4 Recommendation on the settings

- **Now:** leave `[app]` at 1280x720 at 30. The section 7 step 0 clock check is unaffected
  by the target and does not need a change.
- **If 60 fps frame-by-frame review matters more than resolution:** 1280x720 at 60 is the
  realistic middle on this class of PC, for up to about 4 feeds. Set `target_fps` to the
  cameras' exact rate (8.2 item 2).
- **1920x1080 at 60 for 6-8 cameras needs all of:** working hardware JPEG encoding (fix
  the `qsvjpegenc` caps negotiation) or a much bigger CPU; the ingest reorder (8.2 item 3);
  the section 7 fix; replay decode and drawing off the Qt thread (section 5, last row);
  and capacity measured on the production machine, not this laptop.

### 8.5 Method, and what was not measured

- Stage capacities: `gst-launch-1.0` with `videotestsrc pattern=ball`, 1200 frames,
  non-live, timed end to end, one process per measurement. The ingest-order comparison used
  the same source with UYVY 1920x1080 at 60 into a BGR 1280x720 at 30 capsfilter. Scratch
  scripts, not in the repo.
- Disk figures come from `size_bytes` in `metadata.db` (read-only); the 60 fps figure is
  scaled, not measured.
- **Not measured:** NDI's own decode of a 1080p60 stream (it adds to the ingest stage),
  real camera content (JPEG size and encode time depend on it), several real feeds at
  once, `qsvjpegenc` once it links, and the production hardware.
