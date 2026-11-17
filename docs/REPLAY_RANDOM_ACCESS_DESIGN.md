# Replay frame access: random-access JPEG decode (design plan)

Status: **Phases 1 and 2 implemented** on branch `feature/replay-random-access`
(sections 4.1-4.4; results in sections 11 and 12). Phase 3 (reduced-size decode)
is not started. The real-app acceptance test (section 8) failed after Phase 1
and has not been re-run since Phase 2. Written 2026-11-15 from live measurements
on the dev laptop (i7-1165G7, 4 cores / 8 threads, Iris Xe).

## 1. Problem

With replay playing, the app cannot keep up at 1080p:

| Setup | Replay playing | Live pipeline |
|---|---|---|
| 1 feed, 720p | holds 33 ms ticks, app 2.5 cores | clean |
| 1 feed, 1080p | tick interval 33 -> 633 ms, tick cost up to 377 ms | `source_fps` 24-29, recording queue 42-84/256 |
| 2 feeds, 1080p | tick interval 500-800 ms, tick cost ~408 ms | `source_fps` 0, recording queue stuck at 120/256, feeds reconnect every ~10 s |

Acceptance target (section 8) is 2 feeds at 1080p with replay playing.

### Root cause found so far

`SegmentDecoder` gets a frame out of a recorded segment with OpenCV
(`cv2.VideoCapture`), which can only reach frame N by decoding:

- gap <= `_MAX_SKIP_FRAMES` (8): `grab()` each skipped frame. `grab()`
  fully decodes it. Cost ~8.5 ms per frame advanced at 1080p.
- gap > 8, or backwards: seek. OpenCV's FFmpeg seek steps back ~16
  frames and decodes forward. Measured **~125 ms wall, ~190-230 ms CPU**
  for one displayed frame at 1080p, flat in the gap.

At 30 fps, 8 frames is ~266 ms. Once a tick takes longer than that, every
tick seeks. `_adapt_replay_interval` lengthens the tick when ticks run
long, which keeps the decoder on the seek path: more CPU to show fewer
frames. Under load the same decode runs ~3x slower.

### Measurements (idle machine, completed 1080p segment, 119 frames, 27.6 MB)

`SegmentDecoder.decode()` median ms per call by frame gap:

| gap | 1 | 2 | 4 | 6 | 8 | 9 | 10 | 12 | 16 | 20 | 30 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ms | 11 | 20 | 34 | 50 | 68 | 71 | 124 | 124 | 129 | 117 | 123 |

Raw OpenCV: `read()` 9.8 ms wall / ~19-21 ms CPU; `grab()` 8.3 ms.

Decoding one frame straight from its JPEG bytes (`cv2.imdecode`):

| decode | wall per frame | order |
|---|---|---|
| 1920x1080 | 7.5 ms (~15.6 ms CPU) | in order |
| 1920x1080 | 7.9 ms (~15.6 ms CPU) | random |
| 960x540 (`IMREAD_REDUCED_COLOR_2`) | 4.5-4.9 ms | either |
| 480x270 (`IMREAD_REDUCED_COLOR_4`) | ~3 ms | either |

Position does not matter. The CPU column for the reduced sizes was not
reliable, so only wall time is quoted for them.

The Qt main thread was not the bottleneck: 0.07 cores during replay
(oldest thread, per-thread CPU sampling). py-spy could not attach to the
MSYS2 Python 3.14 build, so no Python-level profile exists.

## 2. Goal and non-goals

Goal: the cost of showing one replay frame is ~8 ms of decode regardless
of which frame is wanted, so forward play, rewind, scrub, step and
slow-mo all cost the same per displayed frame, and replay can never fall
into the seek path.

Non-goals (separate work, see the memory note):

- Native NV12 pipeline and the hardware JPEG fix (live side).
- Reconnect-supervisor and recording-queue changes.
- Hardware decode. None is available for JPEG on this machine
  (`jpegdec` only; no `d3d11jpegdec`/`nvjpegdec`; `avdec_mjpeg`/gst-libav
  is not installed).
- Changing the recording format. It stays MJPEG in MKV.

## 3. Current code (reference)

- `app/core/playback_controller.py`
  - `_on_replay_timer_tick` (~l.1132): turns the monotonic clock into a
    session time and calls `_render_at_session_time_ns` (~l.1185), then
    `_adapt_replay_interval` (~l.1255) with the render's wall time.
  - `_render_at_session_time_ns`: per feed, `nearest_frame_location()`,
    then `_decode_jobs` (~l.1233). One feed decodes inline on the Qt
    thread; two or more feeds use a `ThreadPoolExecutor` named
    `replay-decode` and wait for all results. Frames are shown on the Qt
    thread in feed order.
  - The same `_render_at_session_time_ns` serves step, jump, scrub and
    slow-mo (called from ~l.259, 315, 367, 455, 580, 677).
  - Pacing constants: `_REPLAY_TARGET_DUTY` 0.6, `_REPLAY_BUSY_DUTY` 0.8,
    `_REPLAY_IDLE_DUTY` 0.4, cost window 15.
- `app/media/segment_decoder.py`: `SegmentDecoder.decode(location)`
  (~l.141) -> `_read_frame` (~l.220) -> `_read_frame_by_timestamps`
  (~l.253, used when the file's block timestamps are known) ->
  `_walk_to` / `_seek_to_frame`; non-timestamp path uses
  `_advance_sequentially` and `_seek_capture`. `_MAX_SKIP_FRAMES = 8`
  (~l.72). Exact frame indices come from `location.frame_index`
  (docs/REPLAY_SEAM_GLITCH.md).
- `app/media/mkv_timestamps.py`: walks the EBML structure and returns each
  video block's timestamp. `_read_block_header` (~l.173) already reads the
  block header and then skips the data.
- `app/storage/segment_replay_store.py`: replay only resolves segments
  whose state is `complete` (the in-progress segment is never read), so
  every file replay opens is final.
- `app/ui/video_widget.py` `display_frame` (~l.422): already cheap. It
  keeps the BGR frame and scales to tile size with `cv2.resize` before
  converting to a pixmap. Zoomed views use the full-size image
  (`_full_image`), so zoom needs full-resolution frames.

## 4. Design

The decoder keeps its interface (`decode(location) -> MediaFrame | None`)
and all its index logic. Only "how do I get the pixels of frame N" changes.

### 4.1 Frame index for each segment file

Extend `mkv_timestamps.py` with a function that returns, per video frame,
`(timestamp_ns, data_offset, data_size)` instead of only the timestamp.
`read_video_timestamps_ms` becomes a thin wrapper so existing callers and
tests keep working.

Layout verified on a real segment: `A3 <size vint> <track vint> <2-byte
relative timecode> <flags> <JPEG bytes: FF D8 ...>`. Flags `0x80` =
keyframe, no lacing. The data starts `len(track vint) + 3` bytes after the
element's payload start; its size is the block size minus that header.

The index is built once per file when it is first opened (the file's block
headers are already read for timestamps, so this adds no extra pass).

Eligibility - use the direct path only when all hold, else fall back to
the existing cv2 path unchanged:

- Video track CodecID is `V_MS/VFW/FOURCC` with fourcc `MJPG` (what
  `matroskamux` writes for JPEG; `V_MJPEG` does not appear in these files),
  or, as a cheaper equivalent check, the first block's data begins with
  `FF D8`.
- No lacing (flags & 0x06 == 0).
- Block count matches timestamp count and offsets are inside the file.

### 4.2 Decoding a frame

`FrameReader` (new, in `segment_decoder.py` or its own module):

1. Hold the file open for the segment (like `_OpenSegment` does today).
2. For frame N: `seek(offset)`, `read(size)` (~230 KB at 1080p), wrap in
   `numpy.frombuffer`, `cv2.imdecode(buf, IMREAD_COLOR)`.
3. Return BGR, the same type `MediaFrame` carries today.

Keep a tiny LRU (3 frames) per feed so repeated requests for the same
frame (paused, or two ticks inside one frame) decode nothing, as today's
`last_image` does.

### 4.3 Where it plugs in

Inside `SegmentDecoder._read_frame`:

- If the segment has a frame index and is eligible -> frame N =
  `location.frame_index` if present, else `_nearest_index(timestamps,
  offset_ms)` (existing code), clamped to `[0, count-1]`. Read via
  `FrameReader`. No `grab`, no seek, no `primed`, no end-of-file
  backtracking (`_read_last_frame`, `_MAX_END_BACKTRACK_FRAMES`): the
  index knows the true frame count.
- Otherwise -> existing code path, untouched.

If `imdecode` returns `None` or raises, fall back to the cv2 path for that
frame and stop using the direct path for that file (log once, like
`_failed_paths`).

### 4.4 Phase 2: latest-wins decode, Qt thread only paints

Phase 1 makes each decode ~8 ms, so most of the damage is gone. Phase 2
removes the remaining coupling between the tick and decode time:

- One worker per feed. The tick writes the newest wanted `(location)` into
  a one-slot mailbox, replacing any pending request, and returns.
- The worker decodes the newest request and hands the frame to the Qt
  thread with a queued signal; the Qt thread calls
  `output_renderer.show_frame`.
- At most one decode in flight per feed. A request replaced before it
  starts counts as a dropped frame.
- `_adapt_replay_interval` is replaced: keep the timer at the frame period
  (33 ms) and report displayed fps and drops. The "slow the timer when
  ticks are expensive" ratchet goes away because the tick is now cheap.
- Operations that need the frame immediately (step, jump, scrub) call the
  same path and wait for that one result, so single-step stays exact.

### 4.5 Phase 3 (optional): reduced-size decode

For tiles much smaller than the frame, pass a hint (`max_dimension` from
the tile size) and decode with `IMREAD_REDUCED_COLOR_2` / `_4` (4.5 / 3 ms).
Must be disabled while a tile is zoomed (`_full_image` needs full
resolution) and when a frame is requested for stepping at full detail.
Defer until Phase 1-2 are measured; the interaction with the 8x zoom work
makes this the riskiest part.

## 5. Behaviour to preserve

- Exact frame at a given `frame_index` (docs/REPLAY_SEAM_GLITCH.md): the
  direct path uses the same index.
- Request beyond the last frame returns the last frame; repeated request
  for the shown frame decodes nothing.
- Frozen-tile (`is_freeze`) behaviour is decided by the store, not the
  decoder; unchanged.
- Fallback for old recordings, synthetic-source files, anything the index
  cannot describe.

## 6. Pixel parity (measured, needs a visual check)

Compared `cv2.VideoCapture` frames with `imdecode` of the same JPEG for all
119 frames of one segment: shapes identical, **mean absolute difference
0.76 of 255 per frame, worst single pixel 34**, with some pixel difference
above 8 on every frame. This is chroma-upsampling/rounding difference
between FFmpeg's and libjpeg's decoders, not a different frame. It should
be invisible at tile size, but switching from the cv2 path to the direct
path should be checked by eye on real footage (flat colours, text, edges)
before it becomes the default. If it shows, an alternative is decoding with
`IMREAD_COLOR` plus matching the upsampling, which costs time; measure
first.

## 7. Risks and open questions

| Risk | Mitigation |
|---|---|
| Direct decode differs slightly from OpenCV's | Section 6: visual check; fallback flag |
| Recordings with lacing, BlockGroups, other codecs | Eligibility checks, automatic fallback, existing tests keep covering the cv2 path |
| Seam exactness regressions | Reuse `location.frame_index`; run `test_replay_exact_bounds.py` and `test_replay_current_play.py`; add seam tests for the new path |
| Latest-wins drops frames during step/scrub | Operations that need an exact frame wait for that frame; only free-running play drops |
| Thread safety (decoder state, file handle) | One worker owns one decoder; close on replay end (`playback_controller` ~l.910) |
| Memory | 1080p BGR frame ~6 MB; LRU of 3 = ~18 MB per feed |
| Some load remains: ~15.6 ms CPU per displayed 1080p frame, i.e. ~0.5 core per feed at 30 fps | Drops under load instead of seeking; Phase 3; the live-side work (NV12) is still needed |
| Display path (resize + colour conversion + pixmap) not yet measured per frame | Measure in Phase 1 |

Open: how `Slow-mo` and reverse play call the decoder today (they use the
same render function, so they should just work); whether a settings flag is
wanted (suggest `[replay] direct_jpeg_decode`, default on, automatic
fallback). Not yet decided.

## 8. Test plan

Unit (stdlib `unittest`, matching `tests/`):

- `tests/test_mkv_timestamps.py`: new index function returns offsets and
  sizes; decoding each `(offset, size)` slice gives a JPEG; wrapper still
  returns the old timestamps; truncated file, lacing and BlockGroup cases
  return None/fallback.
- `tests/test_segment_decoder.py`: with a stub capture that counts `grab`,
  `read` and `set` calls, the direct path makes none of them for any gap
  (this proves "no seek"); fallback when ineligible or when `imdecode`
  fails; clamping to the last frame; same-frame request decodes once.
- `tests/test_replay_pacing.py`: updated for Phase 2 (timer stays at the
  frame period; drops counted; no interval ratchet).
- Existing: `test_replay_exact_bounds.py`, `test_replay_current_play.py`,
  `test_replay_safety_invariants.py`, `test_playback_controller.py` must
  pass unchanged.

Measurement (rerun the same tools after each phase):

1. Decoder gap benchmark: ms per call must be flat (~8-12 ms at 1080p, idle
   machine) for gaps 1 to 30 with no step at gap 9.
2. Real-app sampler: app cores, per-thread CPU, telemetry, during replay.
3. **Acceptance** (2 feeds, 1080p, replay playing, 60 s window):
   - replay tick interval stays <= 50 ms,
   - recording queue stays < 20/256 on every feed,
   - `source_fps` median >= 29.5 on every feed,
   - no `feed_lost` events in the window.

   The acceptance script reads `app.log` over a window and prints PASS or
   FAIL per criterion. The sampler, decoder benchmark and acceptance
   scripts are currently in the session scratchpad and should be moved to
   `docs/ndi_test_harness`.

## 9. Expected effect (estimates, not measurements)

- Cost of a displayed 1080p frame: ~125 ms wall / ~200 ms CPU (seek path)
  -> ~8 ms wall / ~15.6 ms CPU, independent of position.
- Replay at 30 fps: ~0.5 core per feed (was ~0.6 sequential and up to
  ~0.7 on the seek path while showing only 3-4 fps). At 15 fps displayed,
  ~0.25 core per feed.
- The tick ratchet (33 -> 633 ms) disappears because tick cost no longer
  grows with load in the same way.
- This alone may not be enough for 2 feeds at 1080p on this laptop: a
  healthy live pipeline is ~2.6 cores for 2 feeds and replay adds ~1 core,
  against 4 physical cores. The NV12 work is still needed. Re-measure after
  Phase 1 before deciding on Phase 3.

## 10. Order of work

1. Frame index in `mkv_timestamps.py` + tests (small, isolated).
2. `FrameReader` and the direct path in `SegmentDecoder` + tests + fallback.
3. Re-run the gap benchmark, parity check on real footage, and the sampler
   with replay playing. **Decision point**: is acceptance met?
4. If not, Phase 2 (latest-wins worker, remove the interval ratchet).
5. Re-measure; then decide on Phase 3 and on the NV12 work.

Rough size: steps 1-2 are small and local; step 4 touches the
playback controller and its pacing tests, so it is the larger change.

## 11. Phase 1 results (implemented 2026-11-15)

What changed:

- `app/media/mkv_timestamps.py`: `read_video_frame_index()` returns each video
  frame's timestamp, byte offset and size (`MkvFrameIndex`);
  `read_video_timestamps_ms()` is now a thin wrapper.
- `app/media/segment_decoder.py`: `SegmentDecoder` opens a segment for direct
  access when the index is usable (JPEG frames, no lacing, first frame starts
  `FF D8 FF`), reads the wanted frame's bytes and decodes them with
  `cv2.imdecode`. No OpenCV capture is opened for such files (it is opened
  lazily only on fallback, which also saves the ~85 ms file open at each
  segment boundary). Any file that does not qualify, and any frame that fails
  to decode (the file then stays on OpenCV, with a warning), uses the unchanged
  OpenCV path. A 3-frame cache per open segment.
- `[replay] direct_jpeg_decode` (default `true`) in `app_settings.toml`;
  `false` forces OpenCV. Wired from `AppSettings` to the referee controller's
  decoder factory in `application_coordinator.py`.
- Tests: `tests/mkv_builder.py` (tiny mkv writer), new cases in
  `test_mkv_timestamps.py`, `test_segment_decoder.py` (incl. "no OpenCV capture
  is ever opened for any gap"), `test_app_settings.py`;
  `test_segment_close_gating.py` now checks both paths against GStreamer-recorded
  segments (OpenCV path bit-exact, direct path exact against its own JPEG decode
  and within 2/255 of OpenCV's). Full suite: 1595 tests, only the 4 failures that
  also fail on `main`.

Measured on this laptop, real 1080p segments, machine idle:

| `SegmentDecoder.decode()` by frame gap | 1 | 4 | 8 | 9 | 10 | 16 | 30 |
|---|---|---|---|---|---|---|---|
| before (ms) | 11 | 34 | 68 | 71 | 124 | 129 | 123 |
| after (ms)  | 10 | 10 | 9.6 | 9.5 | 9.5 | 9.6 | 9.5 |

Two feeds, simulated replay ticks (`docs/ndi_test_harness/replay_tick_sim.py`,
pool decode as in `_decode_jobs`):

| tick interval | tick cost before -> after (median) | CPU before -> after |
|---|---|---|
| 33 ms (1x) | 11 -> 18 ms | 1.44 -> 1.03 cores |
| 67 ms | 29 -> 21 ms | 0.93 -> 0.53 |
| 133 ms | 45 -> 23 ms | 0.64 -> 0.32 |
| 283 ms | 79 -> 21 ms | 0.56 -> 0.14 |
| 633 ms | 126 -> 23 ms | 0.39 -> 0.07 |

Same test with 6 of the 8 hardware threads kept busy by other processes: at a
33 ms tick the worst tick was 232 ms before and 32 ms after, CPU 1.38 -> 0.86
cores.

Things to know:

- At the healthy 33 ms cadence on an **idle** machine the median tick is a bit
  slower than before (18 vs 11 ms for two feeds): OpenCV's FFmpeg decode spreads
  one frame over several threads, which shortens wall time but costs more CPU
  (24 vs ~17 ms CPU per frame). Both are well under 33 ms, and the CPU is lower,
  which is what matters when the live pipeline competes for cores.
- Pixel parity on real footage (6 segments, 102 frames): mean absolute difference
  0.76/255 against OpenCV, worst pixel 37, 0.07% of pixels differ by more than 8,
  mostly on saturated edges (the scoreboard). Visually identical side by side.
- Not yet measured: the real app with 2 feeds at 1080p and replay playing
  (acceptance test, section 8), and the display path's per-frame cost.

## 12. Phase 2 (implemented 2026-11-15): decode and scale on a worker thread

Phase 1's real-app run showed the decode was no longer the problem (tick cost
408 -> 55-80 ms) but a tick still cost 50-80 ms: the UI thread did the
resize (7.5 ms), colour conversion (2.4 ms) and pixmap build (3.5 ms) for every
feed under load, and the timer slowed itself to keep ticks at ~60% duty (100-150
ms intervals, ~7-10 fps). Phase 2 moves that work off the UI thread:

- `app/media/replay_frame_worker.py`: `ReplayFrameWorker`, one daemon thread per
  feed, started on first use. `submit(request, epoch)` returns at once and replaces
  any request still waiting (counted as dropped); the worker keeps only the
  newest finished result and wakes the UI thread once per burst; `take()` hands it
  over; `cancel()` / `stop()`.
- `PlaybackController`: the timer tick calls `_submit_replay_frames` (frame-location
  lookup per feed + `submit`). A queued Qt signal (`_FramesReadyBridge`) runs
  `_deliver_replay_frames` on the UI thread, which `show_frame`s the newest finished
  frame per feed. Clicks keep the synchronous `_render_at_session_time_ns` path.
  `_replay_epoch` (bumped by every click render and when the clock stops) makes
  frames finished for an older picture get dropped; frames that arrive outside
  REPLAY mode are dropped too.
- `_adapt_replay_interval` and its constants are gone. The timer ticks once per
  frame of footage (`_replay_interval_ms`, 33 ms at 29.97 fps, clamped 16-50 ms).
  Every 5 s of replay the app logs `replay frames feed=... shown_fps=...
  dropped_fps=... decode_ms=...`.
- `VideoWidget.prepare_for_display(frame)` (reached through
  `OutputRenderer.prepare_frame`) scales the frame to the tile on the worker and
  sets `MediaFrame.fitted` (`FittedImage`); `display_frame` uses it when it still
  fits the tile (same fitted size, not zoomed) and otherwise scales on the UI
  thread as before. Qt now reads the BGR pixels directly (`Format_BGR888`), so the
  `cv2.cvtColor` BGR->RGB pass is gone from both the tile and the zoom paths.
- `SegmentDecoder.decode` / `close` are serialized with a lock (a worker and a
  click can reach the same decoder; a `close` in the middle of a read would
  otherwise mark a good file as failed).

Measured offline (idle machine, real 1080p frame): UI thread per displayed frame
1.8 -> 1.1 ms into a 960x540 tile and 2.6 -> 0.3 ms into 640x360, with the scale
(0.6 / 2.2 ms) done on the worker. The gain is larger under load, where the same
UI-thread work took ~13 ms per feed. The acceptance check
(`docs/ndi_test_harness/replay_accept_check.py`) now judges replay by the logged
displayed fps per feed (>= 20/s) instead of the tick interval.

Not changed: the live pipeline (recording queue stalling the tee, reconnect
flapping, NV12), reduced-size JPEG decode (Phase 3), zoomed tiles (still scaled
on the UI thread).
