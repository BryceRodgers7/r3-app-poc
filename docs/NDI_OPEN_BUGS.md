# Open NDI bugs — findings and where to pick up

Date: 2026-11-01, updated 2026-11-07
Hardware under test: Zowietek POV 4K cameras (`POVZoom-47800`, `POVZoom-55014`),
NDI over wired Ethernet to the dev PC, GStreamer 1.28.2 on MSYS2 UCRT64. The
first round used one camera; the second used two, at 1280x720@60.

> **Status (2026-11-07):** the cause of bug 1 is found and fixes for it, for the
> recording-recovery bugs it exposes, and for choppy replay are on branch
> `fix/begin-game-freeze-recovery-replay`. They are unit-tested but **not yet
> verified in the real app**. Do that first: run the fresh-launch harness
> ([`ndi_test_harness/README.md`](ndi_test_harness/README.md)) on that branch and
> on `main`, and compare the stall rate. Bug 2 (exit hang) is unchanged.

Four-feed numbers in the older notes come from one camera configured as four
`[[feeds]]`. "Original code" means commit `0a894f1`, before any of the UI work.

---

## 1. A feed freezes at Begin Game, then its footage goes unindexed

### What happens

When recording starts, one or more feeds stop dead. Preview and recording both
stop, while other feeds carry on. About 8 seconds later the health check declares
the feed lost and rebuilds it. The picture comes back, so it looks recovered, but
**for the rest of that game the feed's segments never reach the database**, and
**the next game that feed records nothing at all** (1.3).

How often (two cameras, `app.log` from 2026-11-07): 8 of 26 feed starts across 13
game starts. Every one was a game-1 start after launch.

Timeline from one run (session 049, `ndi_1`):

| Time | Event |
|---|---|
| 19:08:24.245 | `Recording started for feed_id=ndi_1`. Its `source_fps` stops within about a second |
| 19:08:24.2 | `qrec` sits at `1/256`: the record queue is **not** backing up |
| 19:08:32.219 | `LIVE -> DISCONNECTED`, once `source_fps` (a 5 s average) has sat at 0 |
| 19:08:33.223 | `rebuild feed=ndi_1 starting`; 30-60 fps again 0.5 s later |
| 19:08:33.708 | It first logs `splitmuxsink opening segment index=0` |

### 1.1 The cause of the freeze: a circular wait

Found with native stack dumps taken from the running app while a feed was frozen
(`ndi_test_harness/stack.py`, two dumps 1.5 s apart, identical both times):

| Thread | Where it is stuck |
|---|---|
| `ndisrc` streaming thread | Inside a **serialized downstream query** (ALLOCATION) forwarded by the `tee` into the record `queue`, waiting for that queue to drain |
| `jpegenc` / record queue thread | Inside `splitmuxsink`'s video input, asleep on a condition variable |
| `splitmuxsink` output threads | Asleep in the output-side probe, waiting to be released |
| `matroskamux` aggregator thread | Waiting for data |

`splitmuxsink` 1.28 gathers input in GOPs. Its reference (video) stream waits in
`check_completed_gop` until **every** stream, including an audio stream that has
just been added and has no data yet, has delivered past the end of the GOP
(`gstsplitmuxsink.c`, lines 2873-2889 in 1.28.2). The audio can only come from the
`ndisrc` thread, because `ndisrcdemux` pushes audio and video from the same
thread; and that thread is blocked behind the video. Nothing breaks the wait
except tearing the pipeline down, which is what the 8 s rebuild does.

Why Begin Game: the record valve used to be opened by `connect_source`, so the
`splitmuxsink` was already mid-file when Begin Game requested its `audio_%u` pad
(the first Start took the "flag false" path, which late-builds the audio branch
onto the running element). Games 2+ build a fresh `splitmuxsink` with both pads
before any data flows, and did not stall.

Evidence for it, from the real app (harness, 2 cameras, 1280x720@60):

| Scenario | Stalled feed starts |
|---|---|
| Game 1 after a fresh launch (`launch_once.py`), the clean trials | **9 of 14** |
| Games 2-14 in one session (`cycle.py`) | **0 of 28** |

The headless GStreamer repro (real camera, no UI) **never** reproduced it: 0 of 42
recorded trials, with and without an injected upstream RECONFIGURE. What
triggers the serialized query in the real app is still **unknown**; the preview
windows are the likely source (resize, layout change, d3d11 reconfigure) but that
is not shown.

### 1.2 The fix (on the branch)

- `connect_source` no longer opens the record valve; `_build_pipeline` marks the
  new `splitmuxsink` as needing a fresh rebuild, so the **first Start takes the
  same path as every later game** (`_rebuild_splitmuxsink_locked`).
- `splitmuxsink` is built with `async-handling`. A closed record valve means it
  never prerolls, and without the property the pipeline's PAUSED to PLAYING change
  stays pending and an async preview sink (d3d11videosink is) gets no frames:
  measured 0 frames in 6 s, ~58 fps with it. This is what the old comment in
  `connect_source` ("closing it can block preroll") was warning about, so do not
  simply close the valve without it.
- `_rebuild_splitmuxsink_locked` no longer requests a second `audio_%u` pad when it
  has just built the audio chain. That would leave an empty audio stream, which
  stalls the video exactly as above. The first Start now reaches this path with no
  chain built, so it matters.

See [`GSTREAMER_INVARIANTS.md` §12](GSTREAMER_INVARIANTS.md).

A side effect worth knowing: segment 0 now opens at the click. Before, the
record branch was encoding all session into a temp "unrouted" folder (29-31 MB
files, 7.5 MB/s per feed) and Begin Game only redirected the *next* rotation, so
the first 0-4 s of every game went to the temp folder and were discarded.

### 1.3 The consequences, and their fixes (on the branch)

**Footage never indexed after a rebuild.** `FeedRuntime.rebuild` runs
`stop_all()`, `connect_source()`, `start_preview()` and nothing re-armed recording:
`stop_all` clears `_recording_running`, and the encoder and record probes return
early when it is False. The rebuilt `splitmuxsink` kept writing files that no probe
counted, and `_finalize_pending_segment_locked` dropped the empty segments at
DEBUG. Confirmed from today's log: all 8 frozen feeds opened segment files for the
rest of the game and indexed **none**; every healthy feed indexed `opened - 1`.

Fix: `stop_all` remembers recording was armed and indexes the partial segment;
`connect_source` starts a worker that re-arms recording through the normal
`enable_file_recording` once video is flowing and the audio decision has settled,
continuing the segment counter (`IMPLEMENTATION.md` §5.8).

**The next game records nothing.** `_teardown_pipeline` never cleared
`_audio_record_encoder`, so after a rebuild it pointed at the dead pipeline's
opusenc. The next Start's `splitmuxsink` rebuild tried to link it and raised
`WRONG_HIERARCHY` ("the next game will record zero segments"). This happened in 3
of 3 cases in the log history where a game-1 freeze was followed by a game 2
(2026-10-29 `ndi_2` and `ndi_5`, 2026-11-07 `ndi_5`), contradicting the earlier
note here that the next Begin Game "appears to clear it". Also, a rebuilt feed's
remaining game-1 footage had no audio branch at all.

Fix: teardown drops every per-pipeline reference. A Start pressed while a feed is
mid-rebuild is now remembered instead of dropped.

**Challenge shows the feed as disconnected.** Not a disconnect; the feed is live.
The referee replays from segment rows, and a feed with none is skipped
(`location is None` in `_render_at_session_time_ns`), so its tile keeps its
placeholder ("Awaiting video...", or the stale "Unable to connect to source.",
which is only ever set at startup). Fixed by the rows existing; the stale
placeholder text is still in section 3.

### 1.4 How to verify

Not yet done. On the branch and on `main`, with the fresh-launch harness:

```
bash ndi_test_harness/run_launches.sh      # see its README: one launch = one game 1
```

Expect a stall rate near zero on the branch against roughly half on `main`. Then
check, per feed, that segment files and `metadata.db` rows agree after a stall
(`STOP_ON_STALL`-style: let the rebuilt feed record 20 s), and that the *next* game
records for that feed. File sizes prove nothing; only rows do. Also confirm the
live preview is not stalled at idle (the `async-handling` risk): all tiles should
show picture before Begin Game.

---

## 2. Process can hang forever at exit

The window closes, `exit 0` is logged, and the process never ends. 2 of 12 NDI
runs. None in 3 exits on the original code, which is too few to clear it.

Native stack of the stuck process, taken with `ndi_test_harness/stack.py`:

```
NtWaitForSingleObject
WaitForSingleObjectEx
Processing.NDI.Lib.x64.dll   (15 frames, no symbols)
LdrShutdownProcess
RtlExitUserProcess
ExitProcess
Py_Exit
```

It is one thread, inside NDI's DLL teardown, waiting on a thread Windows has already
ended. `taskkill` fails and `Stop-Process` doesn't remove it: the process has
`HasExited = True` but stays listed with its ~450 MB until a reboot. Ending the
parent that spawned it does not release it either.

A lead, not proven: closing the windows destroys the native surfaces while pipelines
are running, so each feed logs `Output window was closed`, then `feed_lost`, and
`reconnect_supervisor` schedules a reconnect during shutdown. That also happens on
the original code. Stopping pipelines and receivers before the windows close would
address both. Calling `TerminateProcess` on ourselves after `exec()` returns avoids
the hang, because it skips DLL teardown, and all the harness drivers do this. It is
a workaround rather than a fix, as it would skip any cleanup that runs at exit.

**Trap, hit again on 2026-11-07:** call it with a typed 64-bit pseudo-handle,
`k32.TerminateProcess(wintypes.HANDLE(-1), code)`. Passing
`GetCurrentProcess()` through ctypes as a plain int can silently fail, and the
process then exits normally into the hang. `os._exit()` also runs the DLL detach and
hangs the same way. A one-line standalone script that opens an `ndisrc` can
produce these.

---

## 3. Smaller things

| Item | Detail |
|---|---|
| Choppy 1x replay (**fixed on the branch**, section 3.1) | Two feeds replayed at about 8 frames/s |
| Black tiles entering replay | At four feeds, tiles sit black for up to about 270 ms when a challenge opens, then fill one at a time. `set_video_surface_visible(live=False)` flips to the QLabel layer before the first decoded frame arrives. Worse on the original code |
| One-frame layout glitch on hide/show | A tile being shown can render tiny in its corner for one frame while neighbours are mid-layout. Rare: about 1 in 24 transitions on both versions. Native windows move before Qt finishes the layout pass |
| Maximize/restore dark frame | One or two frames of the window mid-resize, pictures intact. Flagged more on the new layout (7 of 12 against 3 of 12), though the score counts dark clothing and the new layout has more letterbox, so that isn't a clean comparison |
| Clicks block during replay | About 180 ms at four feeds, 55 ms at one, measured before the replay fix in 3.1. Each seek renders every feed on the UI thread. Likely much better now; re-measure |
| Segment boundary hitch in replay | Opening the next segment's file costs ~85 ms and happens inside the tick every 4 s per feed. Prefetching it in the decoder is not done |
| Stale "Unable to connect to source." | Set once in `PlaybackController.initialize` and never cleared; shows on any tile that has no replay frame. Clear it when a feed connects |
| False error at startup | `recording_branch_saturated` (ERROR) can fire while feeds connect, before any recording. The queue drains in about 3 s and nothing is lost. May no longer happen now the record valve is closed at startup |
| `audio_missing` warning timing | The check now runs from the ingest probe, since the record branch's probe no longer sees buffers before Start |
| Pre-existing test failures | `test_long_form_export` `MainCliEncodingTests` (2 tests), `test_returns_only_dirty_sessions`, `test_disk_log_emits_after_set_path`, and on Python 3.12 `test_sampler_totals_reach_snapshot`. The first two fail on the original code too |

### 3.1 Choppy 1x replay

Replay runs on the Qt thread, not the GPU path live uses. A 40 ms timer (25 ticks/s,
against 60 fps footage) drove a decode and a Qt draw of every feed. Measured on a
real 720p60 MJPEG segment, idle machine, offscreen Qt:

| Step | Before | After |
|---|---|---|
| Decode, `set(POS_MSEC)` + `read()` every tick | ~49 ms/feed | ~9 ms at 25 Hz, ~4.4 ms at 60 Hz |
| Display (`cvtColor`, `QImage.copy`, full-size `QPixmap`, Qt smooth-scale) | 6-10 ms/feed | cv2 resize first, then tile-sized work |
| Whole tick, two feeds | ~118 ms (about 8 replay frames/s) | ~17-26 ms |

Why the seek was so slow: OpenCV's FFmpeg backend steps back about 16 frames and
decodes forward to the target, so one seek costs ~11 decodes (4.5 ms each). Fix on
the branch: read or skip forward sequentially and seek only for a real jump;
decode feeds in parallel; pace the timer from the measured tick cost in whole
60 fps frame steps. Two feeds now replay at about 30 frames/s, one feed can run at
60. Not done: moving decode and drawing off the Qt thread, which is what 60 fps
with several feeds would need. `IMPLEMENTATION.md` §6.6.

---

## 4. Zoom, pan and the seek bar: what wasn't exercised

Tested with real mouse input on the real pipeline (59 of 59). `IMPLEMENTATION.md`
§11.5.3 and §11.6 have the design. Not exercised for real:

- Zoom surviving an actual reconnect. Only unit-tested
  (`test_rebuilt_sink_gets_the_view_back`).
- The seek bar at the very end of a just-finished play, where the last segment
  hasn't finalized and the handle should snap back.
- The largest zoom surface, about 9288x5224 (4x of a maximized single tile), ran
  only in a bare test. The full app reached 4636x2552. VRAM use wasn't measured,
  and a weaker production GPU could behave differently.
- The real camera, zoomed, during a feed freeze.
- The replay display change (3.1) was checked against the zoom/pan unit tests and
  pixel assertions, not with the real mouse.

---

## 5. Testing against the real app

Scripts are in [`ndi_test_harness/`](ndi_test_harness/); its README has the commands.

Traps that cost real time:

- **`cycle.py` cannot reproduce bug 1.** It repeats Begin/End Game in one session,
  so only its first cycle is a game 1; the rest take the benign game-2+ path
  (0 of 28 stalls). Use `launch_once.py`, one fresh launch per game 1.
- **Wipe the harness data folder between launches.** A launch that ends without
  closing the session leaves it dirty, and the next launch stops on the
  resume/discard dialog and waits for a human. A driver that ignores this keeps
  opening windows.
- **Stopping a background task does not stop a loop it started.** Check with a
  process listing (`launch_once`, `run_launches`, `main.py`) before assuming the
  desktop is quiet.
- **Don't run two NDI pipelines on the same cameras at once** (the real app plus a
  headless test): it taints both. Rates shift and so does CPU.
- **A headless repro must use an async preview sink.** `fakesink async=False` hides
  the closed-valve preroll stall of 1.2; the real `d3d11videosink` is async.
- **Native stacks find what logs can't.** `stack.py` on the running app, while the
  feed is frozen, identified bug 1 in one run after many log-only attempts.
  Symbols are only the exported ones, so names inside GStreamer plugins are
  approximate: read the thread shapes, not the function names.
- **Never `QTest.qWait` when driving the real app.** It holds the GIL for the whole
  wait (one background-thread tick per second, against about 660 with a
  `QEventLoop`). That stalls every Python pad probe, which fakes NDI connection
  failures, near-zero `source_fps` and a jammed record queue. One run showed 96
  false failures this way.
- **`QWidget.grab()` can't see NDI video.** It's a native child window. Read the
  screen instead: GDI `BitBlt` of the desktop DC includes the d3d11 surfaces.
- **QTest input skips native window routing.** Whether input reaches Qt over a sink
  can only be shown with real Win32 input, which moves the real pointer.
- **Don't chain `QApplication.activeModalWidget().button(...)`.** The temporary
  wrapper dies and raises "already deleted", leaving a real dialog on screen. Hold
  the modal in a local.
- **`d3d11videosink` reset:** `set_render_rectangle(-1, -1, -1, -1)` blanks the
  picture. See `IMPLEMENTATION.md` §11.5.3.
- **For "did I cause this?", run the original code.** `git archive <commit> app
  main.py tests | tar -x -C <dir>`, then point `R3_REPO` at it. Check the commit
  first: `HEAD` moves.
