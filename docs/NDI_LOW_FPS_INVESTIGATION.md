# NDI low frame rate (~12 fps) — investigation and recommendation

Date: 2026-10-25
Hardware under test: one Zowietek POV 4K camera (`POVZOOM-47800 (POVZoom-47800)`),
wired 1 Gb Ethernet to the dev PC (Intel I219-LM), GStreamer 1.28.2 on MSYS2 UCRT64.

> **Status (2026-10-27):** recommendations 1 and 4 are implemented on branch
> `ndi-timestamp-mode`, pending the section 6 validation with the camera.
> `ndisrc` now gets `timestamp-mode=receive-time-vs-timestamp` by default, with
> a per-feed `ndi_timestamp_mode` override on `[[feeds]]` (and `[source]`).
> `source_fps` is counted ahead of `videorate`, and the 1Hz telemetry line
> gained `vrate_drop_per_s` / `vrate_dup_per_s`. Recommendations 2 and 3 are
> not done. The "no config-file equivalent" notes below describe the code before
> that branch.

No application code or settings were changed during this investigation. Every
result below comes from reading the code, today's `C:\SportsReplay\logs\app.log`
and recordings, and running standalone `gst-launch-1.0` pipelines against the
live camera.

---

## 1. Summary

- The telemetry showed `source_fps` / `preview_fps` of 9-16 fps in **every**
  configuration tried (camera 1080p30 and 1080p60; app 720x480@60, 1920x1080@30,
  1920x1080@60). Network, cables, switch, CPU, GPU and the camera's frame rate
  are **not** the cause.
- **Root cause:** `ndisrc` is left at its default `timestamp-mode=auto`, which
  resolves to `receive-time-vs-timecode`. With this camera that mode produces
  buffer timestamps (PTS) that arrive in clumps — about 15 frames stamped only
  ~3 ms apart, then a ~470 ms jump — even though the frames themselves arrive
  evenly (~33 ms apart). The `videorate` element that follows is timestamp-driven:
  it **drops** the frames inside each clump and **duplicates** a frame to fill each
  gap.
- Result with the default settings, measured over ~25 s at 30 fps:
  `videorate` dropped **475** of ~620 input frames and inserted **595**
  duplicates. Only about 23% of the frames leaving the ingest chain are fresh.
  The same `videorate` feeds both the preview and recording branches, so
  **the recorded segments contain mostly repeated frames** (85% of frames in a
  sampled 720x480@60 segment and 76% in a sampled 1920x1080@30 segment were
  byte-size-identical to the previous frame, i.e. about 8-9 distinct frames per
  second).
- The preview branch then makes the symptom worse: each duplicate burst overflows
  the 4-buffer leaky preview queues (head queue plus one queue per window), which
  silently discard most of what is left. That is what the telemetry shows as
  ~12 fps.
- **Fix:** set `timestamp-mode=receive-time-vs-timestamp` on the `ndisrc`
  element. In the end-to-end test, with the app's queue policy unchanged, this
  took the preview from 9.2 / 12.4 / 12.8 fps to 29.9 / 59.6 / 59.6 fps for the
  three app configurations. This is a **code change** (section 6); there is no
  config-file equivalent today.

---

## 2. What was ruled out

| Suspect | Evidence | Verdict |
|---|---|---|
| Camera / NDI frame rate | `gst-launch` with `ndisrc ! ndisrcdemux ! fpsdisplaysink` received **30.19 fps average over 41 s, 0 dropped**, caps `UYVY 1920x1080 30/1`. | Not the cause |
| Cabling / switch / link speed | PC link negotiated 1 Gbps; sustained full-rate receive above. | Not the cause |
| Windows "Public" network profile | Full-rate NDI receive works on the Public profile. | Not the cause |
| NDI HX decode load | Raw `ndisrc` into a leaky queue ran at 30.27 fps. | Not the cause |
| CPU conversion (`videoconvert` UYVY->BGR, BGR->BGRA) | App-style chain at 1080p30 into `fakesink`: ~30-32 fps. | Not the cause |
| GPU / `d3d11videosink` | Two `d3d11videosink` windows at 1080p fed through large queues: 30.6 fps. | Not the cause |
| Python / GIL in the pad probe | The same ~12 fps reproduces in pure `gst-launch` with no app code. | Not the cause |
| Old `run.log` (May 13) held 30.00 fps | A different sender (an NVIDIA Quadro screen-capture source on this PC) ran through the same pipeline at a steady 30.00 fps. Presumably its timecodes are well-formed (not verified). | Points at the camera/timestamp interaction, not the app pipeline in general |

The earlier suspicions about NDI HX decode cost and the Public firewall profile
were wrong and should be disregarded.

---

## 3. Evidence

### 3.1 The telemetry label is misleading

`source_fps` and `preview_fps` are always identical because both are ticked in one
pad probe on the **preview branch's** `videoconvert.src`
(`app/media/pipeline_manager.py:1311-1317`, handler at `:1328-1342`). That point is
after the `tee`, after a leaky 4-buffer queue and the preview valve. The
`FeedMetrics.tick_source` docstring ("fires at the head of the per-feed pipeline",
`app/core/telemetry.py:154`) is therefore inaccurate for native pipelines.

The recording branch is counted separately. In the same sessions it read the full
configured rate:

| Session (from `app.log`) | Config (inferred from disk-budget estimate and segment size) | Preview `source_fps` (median) | Recording rate (segment `frames`/`duration_ms`) |
|---|---|---|---|
| 11:33, 12:09 | 720x480@60 (5.9 MB/s est.) | 14.8-15.6 | 60.2 fps |
| 12:22 | 1920x1080@30 (17.8 MB/s est.) | 12.8 | 30.3 fps |
| 13:27 | 720x480@30 (3.0 MB/s est.) | 12.0 | n/a |

The recorder getting the full rate while the preview gets 12-15 is exactly what
duplicate-filled output followed by leaky preview queues looks like.

### 3.2 Camera frames arrive evenly; the default timestamps do not

PTS of the frames leaving `ndisrcdemux` (20 s trace, camera at 30 fps):

| `timestamp-mode` | Input frames | Deltas < 10 ms | Gaps > 100 ms | `videorate` drop | `videorate` dup | Fresh frames kept |
|---|---|---|---|---|---|---|
| `auto` (default) | 619 | **579** | **39** (~470 ms) | **475** | **595** | **23%** |
| `receive-time-vs-timecode` (what `auto` picks) | 620 | 580 | 39 | 486 | 606 | 22% |
| `receive-time` | 617 | 7 | 0 | 79 | 205 | 87% |
| **`receive-time-vs-timestamp`** | 614 | 1 | 0 | 43 | 171 | **93%** |
| `timestamp` | 616 | 0 | 0 | 0 | 918* | 100% |
| `clocked` | 617 | 537 | 79 | 417 | 1078 | 32% |

\* `timestamp` mode yields perfectly even 33 ms spacing, but the sender's absolute
timestamps make `videorate` back-fill from 0 at start-up (a one-off burst of
duplicates). It would need `videorate skip-to-first=true`; `receive-time-vs-timestamp`
avoids the issue. `timecode` mode produced no frames in one attempt (not
investigated).

Because pure `receive-time` shows evenly spaced arrivals and `auto` shows clumps,
the clumping comes from the timecode term. Inference: this camera's NDI timecode
field advances in coarse steps (~15 frames, ~0.5 s). We did not read the raw
timecode values directly; this is deduced from the behaviour of the timestamp
modes.

### 3.3 The preview queues amplify it

With the default clumpy timestamps and a leaky queue of N buffers in front of the
sink (1080p30, `fakesink`):

| Queue in front of the sink | fps |
|---|---|
| none | ~34 |
| non-leaky, 4 buffers | ~34 |
| **leaky, 4 buffers (app default)** | **~11** |
| leaky, 8 buffers | ~18.6 |
| leaky, 16 buffers | ~30 |
| leaky, 64 buffers | ~32 |

The app has two such queues in series per feed: the head preview queue
(configurable, `[media.queue_policy.preview]`, default 4 buffers / 200 ms,
`app/config/settings.py:42-46`, applied at `pipeline_manager.py:1035-1058`) and a
per-window queue that is **hard-coded** to leaky / 4 buffers / 200 ms
(`pipeline_manager.py:1265-1270`). Raising only the head queue to 64 buffers still
left each window at ~11.6 fps.

Enlarging the queues only stops the *preview* from discarding the duplicates; it
does not bring back the fresh frames `videorate` already threw away.

### 3.4 End-to-end confirmation

Full preview topology rebuilt in `gst-launch` (`ndisrc -> demux -> videoconvert ->
videoscale -> videorate -> capsfilter -> tee -> 4-buf leaky queue -> valve ->
videoconvert -> tee -> 2 x [4-buf leaky queue -> d3d11videosink]`), 40 s per run,
camera at 1080p30:

| App target size/rate | `timestamp-mode=auto` | `timestamp-mode=receive-time-vs-timestamp` |
|---|---|---|
| 1920x1080@30 | **9.2 fps** | **29.9 fps** |
| 720x480@60 | **12.4 fps** | **59.6 fps** |
| 1920x1080@60 | **12.8 fps** | **59.6 fps** |

The unchanged-queue, timestamp-mode-only result is the key one: no queue changes
are needed for the preview rate to reach the configured target. (When the target
exceeds the camera's rate, `videorate` duplicates frames to reach it; the "60" rows
above are the camera's 30 fps stream padded to 60.)

### 3.5 Existing recordings

Frame sizes of MJPEG frames in two recorded segments (identical frames encode to
identical sizes):

| Segment | Frames | Same size as previous frame | Approx. distinct frames/s |
|---|---|---|---|
| `session_015/.../segment_00005.mkv` (720x480@60) | 240 | 203 (85%) | ~9 |
| `session_019/.../segment_00002.mkv` (1920x1080@30; short, 52 frames) | 52 | 39 (76%) | ~7.5 |

Recordings made so far with the default timestamp mode will replay as repeated
frames (stutter) regardless of the configured rate.

---

## 4. Why the app is exposed

`app/media/ndi_receiver.py` builds the native chain as
`ndisrc -> ndisrcdemux -> videoconvert -> videoscale -> videorate -> capsfilter`
(`build_native_chain`, ~lines 178-236). Only `ndi-name` is set on `ndisrc`
(`ndi_receiver.py:222`), so `timestamp-mode` stays at `auto`. `videorate`
(`:193-195`) is always inserted and rate-converts using whatever PTS `ndisrc`
provides. Senders with sane timecodes (the May 13 Quadro/OBS-style source) work
fine; this camera does not.

---

## 5. Recommendations (in priority order)

1. **Set `ndisrc` `timestamp-mode` to `receive-time-vs-timestamp`** (code change in
   `ndi_receiver.py` next to the `ndi-name` property, ~`:222`). Preferably make it a
   per-feed setting (for example `ndi_timestamp_mode` on a `[[feeds]]` entry,
   defaulting to the value above or to `auto` if other senders must be left
   unchanged) so senders can be tuned individually. This is the change that removes
   the root cause and is the only item that restores fresh frames in both the
   preview **and** the recording.
2. **Only insert `videorate` when it is needed.** If the sender's frame rate already
   equals the target rate, rate conversion is redundant and, with jittery
   timestamps, still drops/duplicates a few percent of frames (`receive-time-vs-timestamp`
   still shows drop 43 / dup 171 per ~25 s). Alternatively use `drop-only=true`
   when downscaling the rate.
3. **Make the per-window preview queues use the configurable preview policy**
   (`pipeline_manager.py:1265-1270` currently hard-codes leaky / 4 / 200 ms), and
   consider a default of 16-32 buffers (16 was enough in tests, 8 was not). This is
   defence-in-depth against bursty sources; it is not the main fix.
4. **Fix the telemetry so it can diagnose this next time.** Count frames at the
   ingest head (before the `tee`), keep the preview count separate, and surface
   `videorate` `drop` / `duplicate` counters (set `silent=false`, read the
   properties) as a health indication such as "x% of frames are duplicates". Correct
   the `FeedMetrics.tick_source` docstring.
5. **Camera side (optional):** check whether the camera's firmware or web UI offers
   a newer firmware or an NDI timecode setting; if the timecode is made monotonic and
   per-frame, `auto` may work as intended. This was not verified.

### Interim workaround (no code change)

Setting `[media.queue_policy.preview] max_buffers = 32`, `max_size_time_ms = 1000` in
`app_settings.toml` raises only the head preview queue. It will improve the
preview-fps reading but will **not** reach the target (window queues stay at 4) and
does **not** repair the duplicate-filled recordings. There is no config-file way to
set `timestamp-mode` today.

---

## 6. Validation plan after the change

1. Run `python main.py` with the camera at 1080p30 and the app at 1920x1080@30:
   telemetry `preview_fps` should read ~30. Repeat for 720x480@60 and 1080p60
   (expect ~60 only if the camera really sends 60; otherwise `videorate` pads).
2. Record 10-20 s and re-run the frame-size duplicate check in section 7.3; the
   same-size-as-previous ratio should fall to roughly the stated `videorate`
   duplicate share (a few percent to ~25%), not 76-85%.
3. Check audio/video sync in a recorded segment and in replay: the audio PTS comes
   from the same `ndisrc` timestamp mode, and the "stale-PTS audio drop" logic in
   `pipeline_manager.py` depends on audio PTS being sane.
4. Exercise Next Play, challenge, rewind 10 s, slow 1/2x and 1/4x, jump to live and
   Replay Play (per `CLAUDE.md`).
5. Re-test with any other NDI senders in use (OBS, NVIDIA screen capture, VLC) to
   make sure the new mode does not regress them.

---

## 7. How to reproduce

All commands use the MSYS2 GStreamer at `C:\msys64\ucrt64\bin`. Notes: when stdout
is redirected to a file, `gst-launch-1.0` buffers in ~4 KB blocks, so let it run
long enough to flush; `ndisrc` can hang at "Freeing pipeline" on Ctrl+C, so use
`taskkill /F /PID <pid>` afterwards. Always give `ndisrc` `timeout=30000
connect-timeout=30000` (the first video frame can take several seconds), and always
consume the `audio` pad of `ndisrcdemux`, otherwise `ndisrc` stops with `not-linked`.

### 7.1 Is the camera delivering full rate?

```
gst-launch-1.0.exe -v ndisrc ndi-name="POVZOOM-47800 (POVZoom-47800)" timeout=30000 connect-timeout=30000 ! ndisrcdemux name=d d.video ! queue ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false d.audio ! queue ! fakesink sync=false
```

Expect ~30 `current:` / `average:` fps.

### 7.2 Show the timestamp clumping and `videorate` drop/dup counts

```
gst-launch-1.0.exe -v ndisrc ndi-name="..." timeout=30000 connect-timeout=30000 timestamp-mode=auto ! ndisrcdemux name=d d.video ! identity silent=false name=idin ! videoconvert ! videorate silent=false ! video/x-raw,format=BGR,framerate=30/1 ! fakesink sync=false d.audio ! queue ! fakesink sync=false
```

Look at the `pts:` values on `idin` (consecutive deltas of ~3 ms with ~470 ms jumps =
clumping) and the final `videorate0: drop = ...` / `duplicate = ...` notifications.
Repeat with `timestamp-mode=receive-time-vs-timestamp` for comparison.

### 7.3 Duplicate-frame check on a recording

```
gst-launch-1.0.exe -v filesrc location=C:/SportsReplay/sessions/<session>/recording/<game>/ndi_1/segment_00005.mkv ! matroskademux name=m m.video_0 ! identity silent=false name=idrec ! fakesink sync=false
```

Parse the `(N bytes` value of each `idrec` line; consecutive equal sizes indicate
repeated frames.

### 7.4 The full preview topology (end to end)

```
gst-launch-1.0.exe -v ndisrc ndi-name="..." timeout=30000 connect-timeout=30000 timestamp-mode=receive-time-vs-timestamp ! ndisrcdemux name=d d.video ! videoconvert ! videoscale ! videorate ! video/x-raw,format=BGR,width=1920,height=1080,framerate=30/1 ! tee name=t t. ! queue leaky=2 max-size-buffers=4 max-size-time=200000000 max-size-bytes=0 ! valve drop=false ! videoconvert ! tee name=w w. ! queue leaky=2 max-size-buffers=4 max-size-time=200000000 max-size-bytes=0 ! fpsdisplaysink video-sink=d3d11videosink text-overlay=false sync=false w. ! queue leaky=2 max-size-buffers=4 max-size-time=200000000 max-size-bytes=0 ! fpsdisplaysink video-sink=d3d11videosink text-overlay=false sync=false d.audio ! queue ! fakesink sync=false
```

Swap `timestamp-mode=auto` in to see the ~9-13 fps result.

---

## 8. Open items / caveats

- The camera was set to 1080p30 during these tests (it advertised `30/1`). The user
  reported the same 12-15 fps with the camera set to 1080p60; the mechanism is
  independent of the frame rate but the 1080p60 camera case was not re-measured here.
- The camera's raw NDI timecode values were not read directly; the clumping is
  inferred from PTS behaviour in the different timestamp modes.
- Not yet tested: whether `receive-time-vs-timestamp` is safe for the recording
  pipeline's audio/video timestamps and the replay clock inside the full app (see the
  validation plan), nor other senders in the user's setup.
- The splitmuxsink warning "format-location without an active recording session"
  seen in the logs is unrelated to this issue.
