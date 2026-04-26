# NDI setup on Windows (MSYS2 UCRT64)

This doc captures exactly what has to be present on a fresh Windows machine to
make the `ndi_1` feed in `app_settings.toml` light up in the app. It is meant to
be reproducible across development machines — follow it top to bottom.

The application talks to NDI through GStreamer's Rust-based `ndisrc` element.
That element is part of `gst-plugins-rs` and has two external requirements:

1. It must be installed into the **same** GStreamer tree the app loads from
   (MSYS2 **UCRT64**, matching `docs/UCRT64_DEVELOPMENT.md`).
2. The **NewTek NDI runtime** must be discoverable by the plugin at load time.

If either is missing, `NDIReceiver.connect_source()` in
`app/media/ndi_receiver.py` either rejects the feed with `"GStreamer plugin
'ndisrc' is not installed."` or the pipeline reaches PLAYING but never locks
onto a sender, leaving the UI on "Waiting for the selected source".

## Prerequisites

Before starting, confirm that:

- MSYS2 is installed at `C:\msys64` (or adjust paths below to match).
- The project virtual environment is built against the **UCRT64 Python**, per
  `docs/UCRT64_DEVELOPMENT.md`. Check with:

  ```powershell
  Get-Content .\.venv\pyvenv.cfg
  ```

  The `home` line should read `C:/msys64/ucrt64/bin` and the `executable` line
  should point at `C:/msys64/ucrt64/bin/python3.exe`. If it does not, recreate
  the venv from the UCRT64 shell first — installing the plugin into UCRT64 will
  not help a venv that loads a different Python/GStreamer.
- **NDI Tools** (or the NDI SDK) is installed. The free NewTek / Vizrt NDI
  Tools bundle is sufficient and also provides `Test Pattern` and
  `Studio Monitor` for end-to-end verification. Download:
  <https://ndi.video/tools/> (pick the "NDI Tools" installer for Windows).

## Install the GStreamer NDI plugin

The `ndisrc` element ships with the Rust GStreamer plugin bundle. Install it
into UCRT64 via `pacman`. From any Windows shell:

```powershell
C:\msys64\usr\bin\pacman.exe -S --noconfirm mingw-w64-ucrt-x86_64-gst-plugins-rs
```

Or, from an MSYS2 UCRT64 terminal:

```bash
pacman -S mingw-w64-ucrt-x86_64-gst-plugins-rs
```

The package pulls a handful of dependencies (GStreamer devtools, GUI-related libs, etc.); the exact set and size change with MSYS2 updates — confirm with `pacman -Si mingw-w64-ucrt-x86_64-gst-plugins-rs` if you need current numbers. After install,
the NDI plugin lives at:

```
C:\msys64\ucrt64\lib\gstreamer-1.0\libgstndi.dll
```

### Verify the plugin is registered

Run `gst-inspect-1.0` from the **UCRT64** GStreamer tree (the one the app
actually loads):

```powershell
C:\msys64\ucrt64\bin\gst-inspect-1.0.exe ndisrc
```

You should see something like:

```
Factory Details:
  Long-name   NewTek NDI Source
  ...
Plugin Details:
  Filename    C:\msys64\ucrt64\lib\gstreamer-1.0\libgstndi.dll
  Version     0.15.0-<build hash>
  ...
Element Properties:
  ndi-name            : NDI stream name of the sender
  receiver-ndi-name   : NDI stream name of this receiver
  url-address         : URL/address and port of the sender, e.g. 127.0.0.1:5961
```

If `gst-inspect-1.0` prints `No such element or plugin 'ndisrc'`, the plugin
was not installed into the UCRT64 tree — re-run the `pacman` step above and
confirm you did not accidentally install into `mingw64` or `clang64` instead.

If it prints `Blacklisted` or complains about `Processing.NDI.Lib.x64.dll`, the
plugin loaded but cannot find the NDI runtime — see the next section.

## NewTek NDI runtime

The plugin dynamically loads `Processing.NDI.Lib.x64.dll` at startup. The NDI
Tools installer places it under:

```
C:\Program Files\NDI\NDI 6 Tools\Runtime\
```

and sets the environment variable `NDI_RUNTIME_DIR_V6` pointing at that folder
(or `_V5` / `_V4` for older SDK installs). The Rust `ndisrc` plugin checks
those env vars and also the default Program Files location, so a stock NDI
Tools install typically needs no extra configuration.

If your `gst-inspect-1.0 ndisrc` fails to load the plugin with an NDI runtime
error, set the variable explicitly, for example:

```powershell
setx NDI_RUNTIME_DIR_V6 "C:\Program Files\NDI\NDI 6 Tools\Runtime"
```

Open a new shell after `setx` so the variable is picked up.

## Sender side: start an NDI source

For a smoke test without any capture hardware, use **NDI Tools -> Test
Pattern** on the same (or any) machine on the LAN. Once it is running, open
**NDI Tools -> Studio Monitor** and confirm the sender shows up and plays.

Studio Monitor usually lists sources by just their short name (e.g.
`Test Pattern`), but on the wire every NDI source is advertised as
`HOSTNAME (Source Name)`. That full string is what `ndisrc` needs.

To find your hostname:

```powershell
$env:COMPUTERNAME
```

For example, on this reference machine the resulting NDI name is
`DAVIDI7 (Test Pattern)`.

### Standalone pipeline check (optional but recommended)

Before launching the app, validate the plugin end-to-end with `gst-launch-1.0`:

```powershell
C:\msys64\ucrt64\bin\gst-launch-1.0.exe -v `
  ndisrc "ndi-name=<HOSTNAME> (Test Pattern)" `
  ! decodebin ! videoconvert ! autovideosink
```

You should see a window open with the NDI test pattern. If the pipeline
reaches PLAYING but nothing shows up, you almost certainly have the short name
in `ndi-name` instead of the full `HOSTNAME (Source Name)` form.

## App configuration

In `app_settings.toml`, the NDI feed's `ndi_name` must match the full
on-the-wire name exactly, including the parentheses and the space:

```toml
[[feeds]]
feed_id = "ndi_1"
display_name = "NDI Program"
kind = "ndi"
ndi_name = "DAVIDI7 (Test Pattern)"
enabled = true
```

Replace `DAVIDI7` with the sender machine's hostname. If the sender is running
on the same box as the app, that is `$env:COMPUTERNAME`. Keep the USB feed rows
(`cam_a`, `cam_b`) with `enabled = false` unless you also want to run them.

## Launching the app

From a PowerShell session where the UCRT64-backed venv is active:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

Expected log signals of a healthy NDI start:

- No `GStreamer plugin 'ndisrc' is not installed.` line.
- A single `Ingest telemetry: …` line from `app.media.ndi_receiver` early in
  startup once the pipeline reaches PLAYING.
- Live frames in the operator video area a second or two later.

## Quick troubleshooting matrix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `GStreamer plugin 'ndisrc' is not installed.` at startup | `gst-plugins-rs` missing from UCRT64 | Run the `pacman -S mingw-w64-ucrt-x86_64-gst-plugins-rs` step. |
| `ndisrc` missing but only when running `python`, present in a MSYS2 terminal | The venv is not built against the UCRT64 Python | Recreate the venv from a UCRT64 shell (see `docs/UCRT64_DEVELOPMENT.md`). |
| Pipeline starts, UI stays on "Waiting for the selected source" | `ndi_name` is only the short name; sender not discovered | Set `ndi_name = "<HOSTNAME> (Test Pattern)"` with the actual hostname. |
| `gst-inspect-1.0 ndisrc` prints a DLL load error | NDI runtime not reachable | Install NDI Tools or set `NDI_RUNTIME_DIR_V6` to its runtime folder. |
| Studio Monitor sees the source but the app does not | Firewall or network interface mismatch | Ensure both processes run on the same LAN; allow mDNS/UDP for the sender process. |
