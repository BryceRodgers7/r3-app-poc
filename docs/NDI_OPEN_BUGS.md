# Open NDI bugs — findings and where to pick up

Date: 2026-11-01
Hardware under test: one Zowietek POV 4K camera (`POVZoom-47800 (POVZoom-47800)`),
1920x1080 UYVY 30 fps plus stereo 48 kHz, wired Ethernet to the dev PC. GStreamer
1.28 on MSYS2 UCRT64.

> **Status:** nothing here is fixed. Bug 1 costs game footage and should come
> first. Its second half has a likely cause from reading the code, but no fix
> has been tried. Its first half has no known cause.

Four-feed numbers come from the one camera configured as four `[[feeds]]`, not
four cameras. "Original code" means commit `0a894f1`, before any of the UI work.

---

## 1. A feed freezes at Begin Game, then its footage goes unindexed

### What happens

When recording starts, one or more feeds stop dead. Preview and recording both
stop, while other feeds carry on. About 8 seconds later the health check declares
the feed lost and rebuilds it. The picture comes back, so it looks recovered, but
**for the rest of that game the feed's segments never reach the database.**

- Live video looks fine again, and files keep being written at normal size.
- Replay has nothing for that feed. Its referee tile reads "Unable to connect to
  source." for every challenge.
- `recording_fps` reads 0 for that feed although files are being written.
- Nothing tells the operator footage is at risk beyond the brief `feed_lost`.

Timeline from one run (`ndi_3`):

| Time | Event |
|---|---|
| 02:27:06.910 | `Recording started for feed_id=ndi_3`. Frames stop here, and it never logs `splitmuxsink opening segment index=0` when the other feeds do |
| 02:27:14.051 | `LIVE -> DISCONNECTED`, once `source_fps` (a 5 s average) has sat at 0 |
| 02:27:15.052 | Reconnect attempt 1, `rebuild feed=ndi_3 starting` |
| 02:27:15.329 | `RECONNECTING -> LIVE`, 30 fps again |

### How often

| Setup | Code | Result |
|---|---|---|
| One feed | With UI work | 2 of 14 Begin Games froze |
| Four feeds | Original | 2 of 36 feed starts (both on one Begin Game of 9) |
| Four feeds | With UI work | 3 of 36 feed starts (all on one Begin Game of 9) |

One feed was not measured on the original code. Several feeds can freeze on the
same Begin Game.

### Footage evidence

Segment files on disk against rows in `metadata.db`:

| Run | Frozen feed | Healthy feeds |
|---|---|---|
| With UI work, full drive | 30 files, **0 rows** | `ndi_1`: 31 files, 30 rows |
| Original code | 11 files, **5 rows** (the 5 predate the freeze) | 13/11, 13/11, 14/12 |

A healthy feed runs about 1-2 files ahead of its rows. The frozen feed stops gaining
rows at the rebuild.

The next Begin Game appears to clear it: in a run that kept cycling after a freeze,
`recording_fps` was back to 60 on all feeds on the following Begin Game. Whether
that game's rows were complete was not checked.

Not checked: **whether post-session export finds these files.** It reads the same
index, so assume it doesn't until shown otherwise.

### Likely cause of the missing rows (from reading the code, not yet proven)

`FeedRuntime.rebuild` (`app/media/feed_runtime.py:82`) runs `stop_all()`,
`connect_source()`, `start_preview()`. It does nothing about recording.

1. `PipelineManager.stop_all` (`pipeline_manager.py:645`) sets
   `_recording_running = False`.
2. Nothing sets it back, because `enable_file_recording` is not called on a rebuild.
3. `_on_record_encoder_buffer_probe` returns early when `_recording_running` is
   False, so `pending["first_pts_ns"]` stays None.
4. `_finalize_pending_segment_locked` treats that as an empty segment and drops it,
   logging only at DEBUG. That is the missing rows.
5. `_on_record_branch_buffer_probe` has the same gate, which is the 0 `recording_fps`.

Check first: **why files keep being written at all** when recording is meant to be
off after a rebuild. `_recording_session_paths` survives `stop_all`, and the record
valve state on the rebuilt pipeline wasn't traced.

A fix has to do more than flip the flag. The rebuilt `splitmuxsink` restarts its
fragment numbering, PTS start over so the pts-to-session offset needs re-anchoring,
and the session clock mapping must stay consistent. See `IMPLEMENTATION.md` §5.5.
The silent drop in step 4 should also become a warning: a segment with bytes on
disk and no buffers counted is never "empty".

### Cause of the freeze itself: unknown

Frames stop the moment recording is enabled, on some feeds only, and only sometimes,
which suggests a race in the record-branch start. Compare a frozen feed's log lines
against a healthy one around `Audio record branch wired into splitmuxsink` and
`armed stale-PTS audio drop`. Ingest and preview share a tee, so a blocked record
branch can stall both. `GST_DEBUG` on the tee and record queue during a cycle run
is the obvious next step. Since the freeze is what triggers the rebuild, fixing it
also stops the footage loss in practice, but the rebuild path needs fixing anyway
for real camera dropouts mid-game.

### How to verify a fix

Run `ndi_test_harness/cycle.py` with `STOP_ON_STALL=1`, or a long cycle with no
freezes. Then compare files against rows per feed. A single run can't show this: it
needs dozens of Begin Games. File sizes prove nothing, only rows do.

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
ended. `taskkill` fails and `Stop-Process` doesn't remove it: it stays listed,
holding open files such as `app.log`, until a reboot.

A lead, not proven: closing the windows destroys the native surfaces while pipelines
are running, so each feed logs `Output window was closed`, then `feed_lost`, and
`reconnect_supervisor` schedules a reconnect during shutdown. That also happens on
the original code. Stopping pipelines and receivers before the windows close would
address both. Calling `TerminateProcess` on ourselves after `exec()` returns avoids
the hang, because it skips DLL teardown, and all the harness drivers do this. It is
a workaround rather than a fix, as it would skip any cleanup that runs at exit.

---

## 3. Smaller things

| Item | Detail |
|---|---|
| Black tiles entering replay | At four feeds, tiles sit black for up to about 270 ms when a challenge opens, then fill one at a time. `set_video_surface_visible(live=False)` flips to the QLabel layer before the first decoded frame arrives. Worse on the original code |
| One-frame layout glitch on hide/show | A tile being shown can render tiny in its corner for one frame while neighbours are mid-layout. Rare: about 1 in 24 transitions on both versions. Native windows move before Qt finishes the layout pass |
| Maximize/restore dark frame | One or two frames of the window mid-resize, pictures intact. Flagged more on the new layout (7 of 12 against 3 of 12), though the score counts dark clothing and the new layout has more letterbox, so that isn't a clean comparison |
| Clicks block during replay | About 180 ms at four feeds, 55 ms at one. Each seek renders every feed on the UI thread. Seek bar drags get about 4 picture updates a second at four feeds |
| False error at startup | `recording_branch_saturated` (ERROR) can fire while feeds connect, before any recording. The queue drains in about 3 s and nothing is lost |
| Stale status | "Unable to connect to source." can stay in the referee rail after feeds are live |
| Pre-existing test failures | `test_long_form_export` `MainCliEncodingTests` (2 tests), `test_returns_only_dirty_sessions`, `test_disk_log_emits_after_set_path`, and on Python 3.12 `test_sampler_totals_reach_snapshot`. The first two fail on the original code too |

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

---

## 5. Testing against the real app

Scripts are in [`ndi_test_harness/`](ndi_test_harness/); its README has the commands.

Traps that cost real time:

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
