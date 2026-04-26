# Sports Replay POC

Windows desktop proof of concept for live sports replay using **Python** and **PySide6**, with a **GStreamer**-centered media path (preview, file recording, and rolling replay). Some ingest options still use **OpenCV**-based or transitional Python frame delivery; see *Temporary vs intended to remain* below.

**Developing on Windows:** the media layer loads **GStreamer** through **PyGObject** (`gi`). That stack is easiest with a coherent MSYS2 **UCRT64** Python and GStreamer install. If you see import or DLL issues outside that environment, read [docs/UCRT64_DEVELOPMENT.md](docs/UCRT64_DEVELOPMENT.md).

**Enabling NDI sources:** the `ndi` feed kind is served by GStreamer’s `ndisrc` element from `gst-plugins-rs`, plus the NewTek NDI runtime. For install and `ndi_name` configuration (including the full `HOSTNAME (Source)` form), see [docs/NDI_SETUP.md](docs/NDI_SETUP.md).

Target vs current design (multi-feed, two windows, future playback model) is described in [ARCHITECTURE.md](ARCHITECTURE.md).

## Run

1. Create and activate a virtual environment:
   `python -m venv .venv`
   `.venv\Scripts\activate`
2. Install dependencies. For the GStreamer code paths, include the **media** extra so **PyGObject** is installed:
   `python -m pip install -e ".[media]"`
3. On Windows, run from a context where the **UCRT64** GStreamer + `gi` stack matches the interpreter (see [docs/UCRT64_DEVELOPMENT.md](docs/UCRT64_DEVELOPMENT.md)).
4. Launch the app:
   `python main.py`

**Optional** `app_settings.toml` in the working directory sets ingest and paths (see [app/config/settings.py](app/config/settings.py)). If the file defines `[[feeds]]`, at least one feed must have `enabled = true` and the legacy `[source]` section is ignored. With no `[[feeds]]` table, the app uses a single feed from `[source]`.

- **`kind = "auto"`** (per feed or legacy): try **GStreamer** camera capture first, then fall back to **OpenCV** webcam / synthetic test frames in [app/media/test_source.py](app/media/test_source.py).
- **`kind = "ndi"`**: use the NDI receiver (GStreamer) for that feed.

## Current vertical slice

- **Two windows:** an **operator** window (live multiview, transport controls) and a **program** window (live multiview only), each with its own `PlaybackController` and `MultiFeedOutputRenderer`.
- A **new session** is created on startup; rolling replay and preview run for enabled feeds. **Long “game” recording to disk is not started automatically** — use **Start game recording** in the operator UI. Files go under `{base_data_dir}/sessions/{session_id}/recording/{feed_id}/` (default `base_data_dir` is `C:\SportsReplay`, overridable in TOML).
- **Rolling replay** duration defaults to two minutes (`replay_buffer_seconds` in app settings) and is implemented as on-disk **JPEG** frame metadata, short **muxed** audio/video segments, and in-memory **indices** (see [app/media/replay_buffer.py](app/media/replay_buffer.py)) — not a purely in-RAM buffer.
- **Pause** freezes the viewed frame (operator output when not in program-live-only mode).
- **Rewind 10s** switches the view to buffered content while ingest continues; **Jump to live** returns to the newest frame.
- **Slow 1/2x** and **Slow 1/4x** (operator) adjust replay playback rate; ingest and disk recording are independent of the viewed rate.

## Temporary vs intended to remain

**Temporary or transitional for this milestone**

- **OpenCV** webcam capture and **synthetic** fallback, and the OpenCV `read_frame` path, in [app/media/test_source.py](app/media/test_source.py) (used when GStreamer camera capture does not connect, and for exercises of the `SourceInterface` contract).
- **NumPy / OpenCV BGR** `MediaFrame` payloads and pushing frames from Python into GStreamer in [app/media/pipeline_manager.py](app/media/pipeline_manager.py) — the graph is still described as transitional until native in-tree sources dominate.
- [app/media/replay_buffer.py](app/media/replay_buffer.py) — rolling replay storage is **JPEG- and file-backed** with a defined `ReplayStore` interface; a future system might replace the mix of thumbs + rolling segments with a different timeline store.

**Intended to remain (stable seams)**

- Source abstraction in [app/media/source_interface.py](app/media/source_interface.py)
- Per-feed `FeedRegistry`, `FeedRuntime`, and media coordination in [app/media/pipeline_manager.py](app/media/pipeline_manager.py)
- **GStreamer** muxed session recording in [app/media/recorder.py](app/media/recorder.py) via [app/media/muxed_writer.py](app/media/muxed_writer.py) (not OpenCV `VideoWriter`)
- Per-output view-state / transport in [app/core/playback_controller.py](app/core/playback_controller.py)
- Separation between preview, optional file recording, and rolling replay responsibilities
