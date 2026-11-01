# NDI test harness

Scratch-quality drivers that run the real app against a real NDI camera. They are
reference tooling, not product code, and `C:/git/r3-app` is hard-coded in the
`drive_*` scripts. Findings and test traps are in [`../NDI_OPEN_BUGS.md`](../NDI_OPEN_BUGS.md).

| Script | What it does | Takes the mouse? |
|---|---|---|
| `cycle.py` | Repeats Begin/End Game and counts feeds that freeze | No |
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
