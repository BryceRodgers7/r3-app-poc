# NDI test harness

Scratch-quality drivers that run the real app against a real NDI camera. They are
reference tooling, not product code, and `C:/git/r3-app` is hard-coded in the
`drive_*` scripts. Findings and test traps are in [`../NDI_OPEN_BUGS.md`](../NDI_OPEN_BUGS.md).

| Script | What it does | Takes the mouse? |
|---|---|---|
| `launch_once.py` | One fresh launch, one Begin Game (a game 1), stall check, native stack dumps if a feed stalls | No |
| `run_launches.sh` | Runs `launch_once.py` N times, wiping the data folder between launches | No |
| `cycle.py` | Repeats Begin/End Game in one session. **Cannot reproduce the Begin Game freeze** (only its first cycle is a game 1); useful for games 2+ | No |
| `drive_ndi.py` | Full transport run, reading pictures from the screen | No |
| `drive_zoom.py` | Zoom, pan, seek bar with real Win32 input | **Yes** |
| `compare.py` | Same transitions on either code version | No |
| `reflow.py` | Hide/show flicker on either code version | No |
| `drive_synthetic.py` | Full transport run on synthetic feeds, no camera | No |
| `stack.py` | Native stacks of a stuck process: `python -I stack.py <pid>` | No |

`drive_zoom.py`, `compare.py` and `reflow.py` load helpers out of `drive_ndi.py`,
so keep them together. Windows open on screen and stay on top during a run.

## Setup

Run from an empty folder holding its own `app_settings.toml`, so your real config
and `C:\SportsReplay` stay untouched. Only UCRT64 Python can run the app.

```toml
[app]
base_data_dir = "C:/temp/r3-harness/data"
target_frame_width = 1920
target_frame_height = 1080
target_fps = 60.0
force_python_push_preview = false

[recording]
enabled = true
segment_duration_seconds = 4.0
codec = "mjpeg"
container = "mkv"
audio_enabled = true

[[feeds]]
feed_id = "ndi_1"
display_name = "NDI 1"
kind = "ndi"
ndi_name = "POVZoom-47800 (POVZoom-47800)"
```

Repeat the `[[feeds]]` block with `ndi_2` and so on to run one camera as several
feeds. `drive_synthetic.py` needs `kind = "synthetic"` feeds named `cam_1` to `cam_4`.

```bash
export PATH="/c/msys64/ucrt64/bin:$PATH" PYTHONUTF8=1 PYTHONIOENCODING=utf-8
python=C:/msys64/ucrt64/bin/python.exe
```

## Reproduce the Begin Game freeze

The freeze happens at the first Begin Game after a launch (bug 1 in
[`../NDI_OPEN_BUGS.md`](../NDI_OPEN_BUGS.md)). Each trial therefore has to be a
fresh launch:

```bash
cd C:/temp/r3-harness            # holds app_settings.toml (base_data_dir under r3-harness)
R3_REPO=C:/git/r3-app/r3-app-poc DATA_DIR=C:/temp/r3-harness/data LAUNCHES=12 \n    bash <harness>/run_launches.sh
```

It prints one `RESULT` line per launch and, for any stalled feed, leaves two
native stack dumps in `stacks/`. Run it on the branch under test and on `main`
and compare. Don't run anything else on the same cameras at the same time. The
script wipes `DATA_DIR` before each launch and refuses unless the path contains
`r3-harness`.

`cycle.py` and `compare.py` take the code version from `R3_REPO`:

```bash
# Count freezes over 14 Begin Games.
CYCLES=14 R3_REPO=C:/git/r3-app $python <harness>/cycle.py > run.log 2>&1
grep -E '^(cycle |RESULT)' run.log

# Stop at the first freeze and let the rebuilt feed record, then compare
# files on disk against rows in data/metadata.db, per feed.
STOP_ON_STALL=1 CYCLES=22 R3_REPO=C:/git/r3-app $python <harness>/cycle.py > run.log 2>&1
```

Compare against the code before the UI work:

```bash
mkdir base && git -C C:/git/r3-app archive 0a894f1 app main.py tests | tar -x -C base
R3_REPO="$PWD/base" $python <harness>/cycle.py > run.log 2>&1
```

Runs leave files in `%TEMP%/r3-app-unrouted-segments`. Delete `base_data_dir` and
that folder afterwards.

## Replay performance tools (read-only)

Used to measure replay and live-pipeline cost (see
[`../REPLAY_RANDOM_ACCESS_DESIGN.md`](../REPLAY_RANDOM_ACCESS_DESIGN.md)). None of
them changes the app or its files.

- `sample_app.ps1 -AppPid N -VlcPid N -Seconds 30` samples a running app: process and
  per-thread CPU, system CPU, GPU engine use, and the last telemetry lines.
- `replay_accept_check.py [--seconds 60] [--end HH:MM:SS]` reads `app.log` and prints
  PASS/FAIL for the replay acceptance test: replay shows >= 20 frames/s per feed (from
  the app's `replay frames ... shown_fps=` lines; older logs: tick interval <= 50 ms),
  recording queue < 20/256, `source_fps` >= 29.5 on every feed, replay actually
  playing, 2 feeds present.
- `decoder_gap_bench.py SEGMENT.mkv` times `SegmentDecoder.decode()` against a real
  segment as the frame gap per call grows (flat ~9.5 ms at 1080p with direct JPEG decode).
- `replay_tick_sim.py A.mkv B.mkv` simulates two-feed replay ticks, OpenCV path vs direct.
- `replay_workers_sim.py A.mkv B.mkv [seconds] [busy_processes]` runs free-running replay with real
  decoders, tiles and worker threads and a Qt event loop (no app needed): frames shown/dropped per
  feed, worker decode+scale time, and how busy the UI thread is. `busy_processes` imitates the live
  pipeline competing for cores.

## NV12 / hardware JPEG tools (read-only, Windows)

`nv12_bench.py` compares the ingest chains the app can run (see
[`../IMPLEMENTATION.md`](../IMPLEMENTATION.md) section 5.1 and
[`../GSTREAMER_INVARIANTS.md`](../GSTREAMER_INVARIANTS.md) sections 1 and 13). It starts its own
NDI sender (`R3BENCH`) and builds `gst-launch` chains shaped like the app's (ingest, two d3d11
preview sinks, JPEG record branch); it does not touch the app. Windows open on screen briefly.
Set `R3_BENCH_SEGMENT` to a recorded 1080p `.mkv` to send a real picture instead of colour bars.

```bash
export PATH="/c/msys64/ucrt64/bin:$PATH"
python nv12_bench.py bench bgr,nv12,nv12_qsv --seconds 20           # CPU cores for one feed
python nv12_bench.py bench bgr,nv12,nv12_qsv --feeds 2              # two feeds at once
python nv12_bench.py bench bgr,nv12,nv12_qsv --size 1280x720        # 1080p source into a 720p target
python nv12_bench.py color bgr,nv12,nv12_qsv                        # decoded JPEG colours, real frame
python nv12_bench.py bars  bgr,nv12,nv12_qsv                        # saturated bars expose a wrong matrix
```

Run it on a candidate production PC before trusting the numbers: `nv12_qsv` there is the check
that `qsvjpegenc` links and that `splitmuxsink` rotates (a `record fps` of ~30 with files that
never rotate was the failure mode, so also launch the real app once and look for several
`segment_*.mkv` per feed). The real-app A/B used in the results is `launch_once.py`-style: a
config in an empty folder with two `[[feeds]]` pointing at `<host> (R3BENCH)`, Begin Game, then
read `app.log` telemetry (`enc=` shows the encoder actually chosen).
