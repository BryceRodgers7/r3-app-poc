# Setting up a new PC (dev or production)

Step-by-step install for a brand-new Windows 11 machine. The same steps serve
a development PC and a production PC; the differences are collected in
[§8](#8-production-pc-differences).

The app is a Python / PySide6 program that drives GStreamer through PyGObject.
On Windows that only works reliably when Python, PyGObject, Qt, GStreamer and
every plugin come from one matched set, which is what **MSYS2 UCRT64** provides.
Background: [UCRT64_DEVELOPMENT.md](UCRT64_DEVELOPMENT.md). NDI detail:
[NDI_SETUP.md](NDI_SETUP.md).

> Package list below was taken from the reference dev machine's `pacman -Q`.
> It has not been replayed on a truly clean PC; if a step fails, fix it and
> update this doc.

## 1. Prerequisites

- Windows 10/11, 64-bit, with admin rights for the installs.
- **Intel GPU (recommended)** for hardware JPEG recording (`qsvjpegenc`). With no
  Intel GPU the app falls back to software `jpegenc` (higher CPU). See §8.
- Git, plus network access to the repo.
- A fast disk with plenty of free space for recordings (default folder
  `C:\SportsReplay`).
- Keep the **install path free of spaces** where you can (`C:\msys64`,
  `C:\git\...`).

## 2. Install MSYS2

1. Download the installer from <https://www.msys2.org/> and install to the
   default `C:\msys64`.
2. Open **"MSYS2 UCRT64"** from the Start menu. It must be the UCRT64 one; the
   prompt says `UCRT64`, not `MSYS` or `MINGW64`.
3. Update the system. Run it twice, because the first run may close the window:
   ```bash
   pacman -Syu
   ```

## 3. Install Python, Qt, OpenCV and GStreamer (UCRT64 packages)

In the UCRT64 shell:

```bash
pacman -S --needed \
  mingw-w64-ucrt-x86_64-python \
  mingw-w64-ucrt-x86_64-python-pip \
  mingw-w64-ucrt-x86_64-python-setuptools \
  mingw-w64-ucrt-x86_64-python-numpy \
  mingw-w64-ucrt-x86_64-python-opencv \
  mingw-w64-ucrt-x86_64-python-gobject \
  mingw-w64-ucrt-x86_64-pyside6 \
  mingw-w64-ucrt-x86_64-gstreamer \
  mingw-w64-ucrt-x86_64-gst-plugins-base \
  mingw-w64-ucrt-x86_64-gst-plugins-good \
  mingw-w64-ucrt-x86_64-gst-plugins-bad \
  mingw-w64-ucrt-x86_64-gst-plugins-ugly \
  mingw-w64-ucrt-x86_64-gst-plugins-rs \
  mingw-w64-ucrt-x86_64-libva \
  mingw-w64-ucrt-x86_64-ffmpeg
```

Why these come from pacman and not PyPI: PyPI has no wheels for MinGW/UCRT
Python, so PySide6, NumPy and OpenCV must be the MSYS2 builds.

| Package | Used for |
| --- | --- |
| `gstreamer`, `gst-plugins-*` | pipelines; `-bad` carries `d3d11videosink` and the `qsv` plugin |
| `gst-plugins-rs` | the NDI source element, `ndisrc` |
| `python-gobject` | `import gi` (GStreamer from Python) |
| `pyside6`, `python-opencv`, `python-numpy` | UI, image handling |
| `libva` | Intel hardware media (QuickSync) support |
| `ffmpeg` | post-session MP4 export |

## 4. Install the NDI runtime

1. Install **NDI Tools** (free) from <https://ndi.video/tools/>. It installs
   `Processing.NDI.Lib.x64.dll` and sets `NDI_RUNTIME_DIR_V6`. It also gives you
   **Test Pattern** (a fake camera) and **Studio Monitor**.
2. Open a **new** shell so the environment variable is visible.
3. Confirm the plugin loads (PowerShell or UCRT64):
   ```powershell
   C:\msys64\ucrt64\bin\gst-inspect-1.0.exe ndisrc
   ```
   You should see "NewTek NDI Source". If it says the NDI library is missing,
   run `setx NDI_RUNTIME_DIR_V6 "C:\Program Files\NDI\NDI 6 Tools\Runtime"` and
   open a new shell.

## 5. Get the code and build the venv

In the UCRT64 shell (not PowerShell, not a python.org Python):

```bash
mkdir -p /c/git/r3-app-demo && cd /c/git/r3-app-demo
git clone <repo-url> r3-app-poc
cd r3-app-poc

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -e ".[media]"
```

`--system-site-packages` is required: it is how the venv sees `gi`, PySide6,
OpenCV and GStreamer from MSYS2.

Check that the venv is built on the UCRT64 Python:

```powershell
Get-Content .\.venv\pyvenv.cfg
# home = C:/msys64/ucrt64/bin   and   include-system-site-packages = true
```

If `pip install` tries to download or compile PySide6, OpenCV or PyGObject,
stop it. Those should already be satisfied by pacman. Re-check §3, or as a
last resort run `python -m pip install -e . --no-deps`.

Quick import check:

```bash
python -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); print(Gst.version_string()); import PySide6, cv2, numpy; print('ok')"
```

## 6. Configure the app

`app_settings.toml` is **git-ignored**, so it does not come with the clone.
Create it for each PC:

```bash
cp app_settings.toml.example app_settings.toml
```

Edit it:

- `[app] base_data_dir`: where sessions and recordings go (default
  `C:\SportsReplay`; the app creates it).
- `[[feeds]]`: one row per camera. At least one needs `enabled = true`.
  For NDI, `ndi_name` must be the **full** `HOSTNAME (Source Name)` string,
  e.g. `"CAMPC1 (Camera 1)"`. Find sender names in NDI Studio Monitor.
- For a camera-less dev PC, uncomment the `synthetic` feed row and disable the
  NDI rows. Also set `[recording] audio_enabled = false`; the synthetic source
  has no audio.
- `[media] hardware_acceleration = "auto"` is fine. It uses the Intel hardware
  JPEG encoder if present and falls back to software otherwise.
- `[media] working_pixel_format = "NV12"` is the default; use `"BGR"` only if
  NV12 misbehaves on some GPU or driver.

Camera names and order are not set here. The **pre-match dialog** at startup
asks for them and saves `camera_layout.toml` in `base_data_dir`. See
[PREMATCH.md](PREMATCH.md).

## 7. First run and verification

```bash
source .venv/bin/activate        # every new shell
python main.py
```

Always launch from a shell where the UCRT64 Python is the first `python`.

1. **Pre-match dialog** appears; name the cameras and continue.
2. Two windows open: referee and operator.
3. Live video shows in each tile. (For a smoke test, start NDI Tools **Test
   Pattern** and point an `ndi_name` at it.)
4. Exercise the transport: Start game, wait ~10 s, Rewind, Slow-mo, Jump to
   Live, Replay Play, Stop game. Recordings land under
   `base_data_dir\sessions\session_N`.
5. Run the tests: `python -m unittest discover -s tests`.

Healthy NDI start: no "ndisrc is not installed" line, and an ingest telemetry
line in the log once the pipeline reaches PLAYING.

## 8. Production PC differences

- Use only `kind = "ndi"` feeds; remove or comment out the synthetic row.
- Set `[app] app_mode = "production"` (surfaces a banner if anything regresses to
  the Python-push path).
- Raise the target resolution/FPS to the real feed (e.g. `target_frame_width =
  1920`, `target_frame_height = 1080`, `target_fps = 30.0`). Sizes must be even
  for NV12.
- Set `[recording] disk_budget_mb_s` to match the disk (200 ≈ SATA SSD; higher
  for NVMe, lower for spinning disk or network share). A startup check warns
  if estimated throughput exceeds it.
- Put `base_data_dir` on the fast recording disk.
- Install the **current Intel graphics driver** so QuickSync works, then confirm:
  ```powershell
  C:\msys64\ucrt64\bin\gst-inspect-1.0.exe qsvjpegenc
  ```
  If it is missing, the app still works on software `jpegenc` but uses more CPU.
  Hardware JPEG has not yet been tried on the production PC; test with the real
  feeds before relying on it.
- Network: cameras and the PC on the same LAN/VLAN. Allow NDI discovery
  (mDNS/UDP 5353) and streams through Windows Firewall; set the network profile
  to Private.
- Windows power settings: no sleep, no USB/disk power saving, High performance
  plan. Disable notifications and updates during an event.
- After a game, export MP4s with the post-session processor (needs ffmpeg,
  already installed in §3); see the README.
- Keep your own copy of `app_settings.toml` (git-ignored); it is the one file a
  re-clone loses.

## 9. Updating an existing install

```bash
cd /c/git/r3-app-demo/r3-app-poc
git pull
source .venv/bin/activate
python -m pip install -e ".[media]"
```

Update MSYS2 packages occasionally with `pacman -Syu`, but not right before an
event. Updating GStreamer can change behaviour; re-run the §7 checks after.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: gi` / DLL load errors | wrong Python on `PATH`, or venv without `--system-site-packages` | run from the UCRT64 shell; recreate the venv per §5 |
| `GStreamer plugin 'ndisrc' is not installed.` | `gst-plugins-rs` missing | `pacman -S mingw-w64-ucrt-x86_64-gst-plugins-rs` |
| `gst-inspect-1.0 ndisrc` says the NDI library is missing | NDI Tools not installed or env var not set | §4 |
| UI stuck on "Waiting for the selected source" | `ndi_name` is not the full `HOST (Name)`, or firewall/LAN | NDI_SETUP.md |
| High CPU, no `qsvjpegenc` | no Intel GPU/driver | install driver, or accept software `jpegenc` |
| Preview window glitches (extra window, freezes) | `d3d11videosink` problem on this GPU | `force_python_push_preview = true` as a stopgap |
| Synthetic feed records nothing / stalls | `audio_enabled = true` with no audio source | set `audio_enabled = false` |
