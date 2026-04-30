# Sports Replay POC

Windows desktop proof of concept for live sports replay using **Python** and **PySide6**, with a **GStreamer**-centered media path (preview, file recording, and rolling replay). Production ingest is **NDI-only**; a synthetic test source exists for camera-less development. See *Temporary vs intended to remain* below for which paths still go through Python.

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

## Post-session MP4 export (Phase 8)

After the recording app has been **shut down** and the session is **finalized**, run the post-session processor to produce one long-form MP4 per game per feed. Source MKV segments stay on disk untouched.

Dependencies:

- **ffmpeg** must be installed and either on PATH or passed via `--ffmpeg-path`. On MSYS2 UCRT64:
  `pacman -S mingw-w64-ucrt-x86_64-ffmpeg`
  Default install location is `C:\msys64\ucrt64\bin\ffmpeg.exe`.

Usage:

```
python -m app.tools.post_session_processor <session_path> [options]
```

Examples (Windows / Git Bash):

```
# Default — print plan, encode each (game, feed) to <session>/processed/<game>/<feed>.mp4
python -m app.tools.post_session_processor C:/SportsReplay/sessions/session_118 \
    --ffmpeg-path C:/msys64/ucrt64/bin/ffmpeg.exe

# Plan only, no encoding
python -m app.tools.post_session_processor C:/SportsReplay/sessions/session_118 --dry-run
```

Or, to skip `--ffmpeg-path` on every run, add the bin dir to PATH for your shell:

```
export PATH="/c/msys64/ucrt64/bin:$PATH"
python -m app.tools.post_session_processor C:/SportsReplay/sessions/session_118
```

Behavior:

- **Refuses to run** on sessions in any state other than `finalized`. Use the recording app's recovery dialog to finalize a `dirty` / `stopped` session first.
- **File-locks** the session directory via `<session>/.processing.lock` so concurrent processors fail-fast. If the previous run crashed, delete the lock file manually and retry.
- **Encodes** to H.264 (libx264, `-preset medium -crf 23`) + AAC (128 kbps), MP4 container.
- **Output**: `<session_path>/processed/<game_NNN>/<feed_id>.mp4` per (game, feed). Existing outputs are overwritten (`-y`).
- **Idempotent re-runs (Phase 8.C)**: every export attempt persists a row into the `export_artifacts` SQLite table. A subsequent run skips any `(kind, game, feed)` triple that already has a `success` row — no wasted encode time. Failed attempts are retried automatically on re-run; pass `--force` to ignore prior successes too.
- **Exit codes:** `0` — all artifacts encoded (or all skipped, or `--dry-run`); `1` — one or more artifacts failed; `2` — pre-flight failure (validation, missing DB, missing ffmpeg, lock held).

Options:

- `--dry-run` — print the export plan and exit without encoding.
- `--force` — re-encode every artifact even when a prior `success` row exists.
- `--ffmpeg-path PATH` — override ffmpeg binary location (otherwise `shutil.which("ffmpeg")`).
- `--metadata-db PATH` — override DB location (default `<session_path>/../../metadata.db`, i.e. `<base_data_dir>/metadata.db`).
- `-v` / `--verbose` — DEBUG-level logging.

Phase 8 ships long-form export today. Short-play MP4s (per-clip extracts using operator clip markers) are deferred until the §6.7 clip-marker system lands. See [docs/r3_app_architecture.md](docs/r3_app_architecture.md) Phase 8 sequencing notes for details.

- **`kind = "ndi"`**: use the NDI receiver (GStreamer) for that feed. Production deployments use this exclusively.
- **`kind = "synthetic"`**: deterministic synthetic test pattern from [app/media/test_source.py](app/media/test_source.py). Dev-only fallback for machines without NDI hardware.

USB / OpenCV / GStreamer-camera ingest was removed in Phase 2.5; `kind = "auto"` now produces a startup error.

## Current vertical slice

- **Two windows:** an **operator** window (live multiview, transport controls) and a **program** window (live multiview only), each with its own `PlaybackController` and `MultiFeedOutputRenderer`.
- A **new session** is created on startup; live preview runs for enabled feeds. **Long "game" recording to disk is not started automatically** — use **Start game recording** in the operator UI. Each press of Start allocates a fresh `game_NNN` subdir; segment files land at `{base_data_dir}/sessions/{session_id}/recording/<game_NNN>/<feed_id>/segment_NNNNN.mkv` (default `base_data_dir` is `C:\SportsReplay`, overridable in TOML).
- **Replay reads from the recorded segments** via `RecordingSegmentReplayStore` and per-feed `SegmentDecoder` (slice 4.C / 4.D). Replay is only available while recording is active; transport returns to the live state when recording stops.
- **Pause** freezes the viewed frame (operator output when not in program-live-only mode). The "behind live" counter advances at wall-clock rate (1s per real second) while paused or in replay.
- **Rewind 10s** switches the view to buffered content while ingest continues; repeated clicks accumulate. **Jump to live** returns to the newest frame.
- **Slow 1/2x** and **Slow 1/4x** (operator) adjust replay playback rate only — they do not auto-rewind. Ingest and disk recording are independent of the viewed rate.
- **Stop game recording** finalizes the in-flight segment (matroskamux trailer is forced via `splitmuxsink.split-now`) and clears all transport overlays back to fresh-startup state. The next Start creates a new `game_NNN` folder and a fresh splitmuxsink instance — no risk of appending to the previous game's last file. **Caveat:** the split-on-stop sequence leaves one short, trailer-less "dud" file at the tail of each game's folder that is not openable in media players. The startup recovery scan quarantines or marks it dirty on the next launch.

## Temporary vs intended to remain

**Temporary or transitional for this milestone**

- **Synthetic test source** in [app/media/test_source.py](app/media/test_source.py) — deterministic frame generator for dev environments without NDI hardware. Stays on the `python_push` path by design; not productionized.
- **NumPy / OpenCV BGR** `MediaFrame` payloads and pushing frames from Python into GStreamer in [app/media/pipeline_manager.py](app/media/pipeline_manager.py) — the graph is still described as transitional until Phase 3 lands native NDI ingest for production NDI feeds.
- The trailer-less "dud" file produced at the end of each game (see *Current vertical slice*) is an artifact of the current `split-now`-on-Stop strategy and may be eliminated by a cleaner finalize path later.

**Intended to remain (stable seams)**

- Source abstraction in [app/media/source_interface.py](app/media/source_interface.py)
- Per-feed `FeedRegistry`, `FeedRuntime`, and media coordination in [app/media/pipeline_manager.py](app/media/pipeline_manager.py)
- Native **splitmuxsink-driven** segmented recording owned by each feed's `PipelineManager`; segment metadata persisted via [app/storage/metadata_db.py](app/storage/metadata_db.py) and indexed in-memory by [app/storage/segment_index.py](app/storage/segment_index.py)
- Replay query layer in [app/storage/segment_replay_store.py](app/storage/segment_replay_store.py) and replay decoding in [app/media/segment_decoder.py](app/media/segment_decoder.py)
- Per-output view-state / transport in [app/core/playback_controller.py](app/core/playback_controller.py), anchored to [app/core/session_clock.py](app/core/session_clock.py) for smooth wall-clock-resolution overlay timestamps
- Crash recovery + dirty-session prompt in [app/storage/session_recovery.py](app/storage/session_recovery.py) and [app/ui/recovery_dialog.py](app/ui/recovery_dialog.py)
