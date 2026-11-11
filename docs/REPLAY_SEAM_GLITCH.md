# Replay glitch at segment seams — findings, and how it scales to 6-8 feeds and 60 fps

Date: 2026-11-10. Status: **investigated, not fixed.** Nothing under `app/` was
changed. Read this before touching segment bookkeeping in `PipelineManager`,
`RecordingSegmentReplayStore.nearest_frame_location`, `SegmentDecoder`, or
`PlaybackController.step_frames`.

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
