# Why use the MSYS2 UCRT64 terminal for this app?

This project is a **Windows** desktop app. The user interface and most Python dependencies install cleanly from PyPI, but the **media pipeline** is built around **GStreamer** loaded through **PyGObject** (`gi.repository.Gst`, `gi.repository.GstVideo`). On Windows, that stack is a **native** one: you need a consistent set of **GLib, GObject, GStreamer, and plugins** that match the **same C runtime and ABI** as the Python process loading them.

The **UCRT64** environment in [MSYS2](https://www.msys2.org/) is the practical way to get that: it ships **mingw-w64 UCRT** builds of Python, GStreamer, GObject introspection, and related libraries that are **built and linked together**. Running and developing the app from the **UCRT64** shell ensures:

1. **PATH and DLL discovery**  
   The same prefix (`…/msys64/ucrt64/…`) is used for `python`, `gstreamer-1.0-*.dll`, plugin directories, and typelibs. PowerShell or a “random” `python` on `PATH` often points at a different Python, so `import gi` or GStreamer plugin loading fails with missing DLLs, wrong plugins, or cryptic `gi` / Gst errors.

2. **UCRT (Universal C Runtime)**  
   The UCRT64 toolchain targets the UCRT, which is what current MSYS2 **MinGW-w64** packages for this family expect. Mixing a **MSVC**-linked python.org CPython with **MinGW**-built GStreamer/GLib is fragile; UCRT64 keeps **one** coherent stack.

3. **What the app actually imports**  
   The pipeline in `app/media/pipeline_manager.py` requires GStreamer via PyGObject (see the `_ensure_gstreamer_loaded` path). Optional install extras in `pyproject.toml` list `PyGObject`; on Windows, the **hard** part is not the wheel alone—it is the **native** GStreamer + GObject + plugin layout next to the interpreter. MSYS2’s UCRT64 packages address that; a plain venv of stock CPython often does not.

4. **NDI and other plugins**  
   Features such as NDI (e.g. `ndisrc`) depend on **GStreamer plug-ins** being discoverable. That is much easier to reason about when `GST_PLUGIN_PATH`, the plugin directory, and the Python you run are all under the same MSYS2 UCRT64 tree you maintain.

## Recommended workflow

- Install [MSYS2](https://www.msys2.org/) and use the **“MSYS2 MinGW UCRT 64-bit”** / UCRT64 environment (or open `ucrt64.exe` so your shell is the UCRT64 one).
- Use the **UCRT64** `python3` to create the project venv, e.g. from the repo root:  
  `python3 -m venv .venv`  
  For GStreamer and `gi` provided by MSYS2 system packages, a venv with access to those packages is often used, e.g.  
  `python3 -m venv --system-site-packages .venv`  
  so PyPI packages (e.g. PySide6) live in the venv while `gi`/GStreamer stay consistent with the UCRT64 install.
- Activate the venv and install the project, including the **media** extra so `PyGObject` is available to the `pip` resolver:  
  `python -m pip install -e ".[media]"`  
  (The native GStreamer + GObject stack still comes from MSYS2; the extra only pulls the Python bindings.)
- **Always** run `python main.py` from a shell where the UCRT64 `python` is first on `PATH` (typically the UCRT64 MSYS2 terminal, after activation).

## What this document is not

It is not a full MSYS2 administration guide. Package names and updates change; refer to the current MSYS2 docs for “UCRT64”, “GStreamer”, and “Python” if you need exact `pacman` package lists.

**Summary:** For this repository, the UCRT64 terminal is the supported way to run and develop because the **media path depends on GStreamer + PyGObject and native MinGW UCRT64 binaries** that MSYS2 UCRT64 provides as a set. Other terminals and Python builds are likely to work for **non-GStreamer** experiments only, and will not match how the app’s GStreamer graph is expected to be developed and debugged on Windows.
