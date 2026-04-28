# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Windows desktop proof-of-concept for live multi-feed sports video replay. PySide6 UI on top of a GStreamer-centered media path (preview, file recording, rolling replay). Production ingest is **NDI-only** (`kind = "ndi"`); a synthetic test source (`kind = "synthetic"`) is the dev fallback for camera-less work. Both still push frames through Python today — Phase 3.A is the slice that converts NDI to a native GStreamer source bin. Two top-level windows (operator + program) drive two independent `PlaybackController` instances over a shared graph of per-feed `FeedRuntime`s.

The repo is between proof-of-concept and production. Two architecture documents coexist:
- [ARCHITECTURE.md](ARCHITECTURE.md) — describes the **current** code's object graph and what is "still open."
- [docs/r3_app_architecture.md](docs/r3_app_architecture.md) — declared **authoritative target** production architecture. It is aspirational; the current code does not yet conform to it (see "Architecture doc vs current code" below).

When asked to design or refactor toward production, treat `docs/r3_app_architecture.md` as the spec and the `ARCHITECTURE.md` "Still open" list as the gap. When asked to fix or extend current behavior, read the actual code first — the production doc describes intent, not implementation.

## Run / install / test

The Windows GStreamer + `gi` (PyGObject) stack must match the Python interpreter — see [docs/UCRT64_DEVELOPMENT.md](docs/UCRT64_DEVELOPMENT.md) for the MSYS2 UCRT64 setup. NDI feeds additionally require `gst-plugins-rs` and the NewTek NDI runtime — see [docs/NDI_SETUP.md](docs/NDI_SETUP.md).

```
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[media]"     # the [media] extra installs PyGObject
python main.py
```

Tests use stdlib `unittest`:

```
python -m unittest discover -s tests
python -m unittest tests.test_recorder           # one module
python -m unittest tests.test_recorder.RecorderTests.test_specific_case   # one case
```

There is no lint/format config in the repo; don't invent one.

Optional `app_settings.toml` in the working directory configures feeds and paths. With `[[feeds]]` rows present, the legacy `[source]` block is ignored and at least one feed must have `enabled = true`. See `app_settings.toml.example` and `app/config/settings.py`.

## High-level architecture

```
main.py
  └─ build_application
       ├─ AppSettings.load()                       # app/config/settings.py
       ├─ FileManager + MetadataDb + SessionManager  # app/storage/*
       ├─ build_default_application_coordinator    # app/core/application_coordinator.py
       │     ├─ FeedRegistry                       # one FeedDefinition per [[feeds]] row
       │     └─ for each enabled feed:
       │           Source (NDI / synthetic)
       │           Recorder, ReplayBuffer (ReplayStore)
       │           PreviewOutput
       │           PipelineManager (owns the GStreamer graph + tee fan-out)
       │           FeedRuntime (bundles the above)
       │     ├─ RecordingManager (one Recorder per feed_id)
       │     └─ ReplayStoreManager (one ReplayStore per feed_id)
       ├─ Two MultiFeedOutputRenderer instances (operator, program)
       ├─ Two PlaybackController instances built inside the coordinator
       │     (operator: full transport; program: live_only=True)
       └─ Two MainWindow instances (operator shows controls; program does not)
```

Key invariants worth knowing before editing:

- **Per-feed everything.** `FeedRegistry` → `FeedRuntime` → its own `PipelineManager` / `Recorder` / `ReplayStore` / `PreviewOutput`. Do not reintroduce a single global source or shared preview surface.
- **Two output channels.** Operator and program windows each have their own `PlaybackController`, `MultiFeedOutputRenderer`, and `MainWindow`. Program is `live_only`; only the operator has pause / replay / slow / jump-to-live. Long recording continues regardless of what either window is showing.
- **Replay storage today is JPEG thumbs + short muxed audio/video segments + in-memory indexes** (`app/media/replay_buffer.py`), not native segmented encoded recording. The production doc forbids this; the current code still does it.
- **Long game recording is opt-in.** Sessions are created on startup and rolling replay starts immediately, but the long game recording on disk is started by the operator's "Start game recording" button (`ApplicationCoordinator.toggle_long_session_recording`). Files land under `{base_data_dir}/sessions/{session_id}/recording/{feed_id}/`.
- **`PlaybackController` still leans on the primary feed** (first enabled feed) for some replay paths — `_primary_runtime`, `_primary_feed_id`, `_replay_buffer = replay_store_manager.get(primary)`. True symmetric multi-feed replay is not yet implemented.
- **Slow motion** is implemented by adjusting playback rate (1.0 / 0.5 / 0.25) inside `PlaybackController` rather than via a separate `PlaybackSession` / timeline model.
- **Frames that flow through Python** (BGR `MediaFrame`, audio chunks `AudioChunk`) are the transitional path — the production target is for these to stay inside GStreamer.

### Layer map

- `app/config/` — `AppSettings` dataclass + TOML loader (legacy `[source]` and new `[[feeds]]`).
- `app/core/` — coordinator, registry, playback controller, app state, signals, dataclasses (`models.py` defines `MediaFrame`, `AudioChunk`, `SessionPaths`, `PlaybackMode`).
- `app/media/` — sources (`source_interface`, `source_factory`, `ndi_receiver`, `test_source`), pipeline (`pipeline_manager`), recording (`recorder`, `muxed_writer`, `recording_manager`), replay (`replay_buffer`, `replay_store_manager`), output (`output_renderer`, `preview_output`), telemetry/overlay helpers.
- `app/storage/` — filesystem layout (`file_manager`), SQLite metadata (`metadata_db`), session lifecycle (`session_manager`).
- `app/ui/` — `MainWindow` (re-used for both operator and program with flags), `controls_widget`, `multi_feed_video_panel`, `status_bar_widget`, `video_widget`.
- `tests/` — fast unit tests using stdlib `unittest`. They mock or stub GStreamer where needed; do not require real cameras to run.

## Working in this codebase

- Do not bypass the per-feed seams. Code that touches recording, replay storage, or ingest should go through `FeedRuntime`, `RecordingManager`, or `ReplayStoreManager`, not directly into one feed's internals.
- The `PreviewOutput` / `OutputRenderer` split is intentional: a feed's `PreviewOutput` is its own ingest sink, while operator/program windows render via `MultiFeedOutputRenderer`. Do not collapse them.
- The synthetic source in `app/media/test_source.py` is the only non-NDI path the app builds. It is the dev fallback for camera-less environments — don't remove it as part of unrelated work.
- `MediaFrame` payloads are `numpy` BGR images (OpenCV ordering convention). When adding overlays, use `app/media/frame_overlay.py`.
- For UI changes, run `python main.py` and exercise the relevant transport (start/stop recording, rewind 10s, slow 1/2x, slow 1/4x, jump to live) — type checks won't catch playback regressions.

## Architecture doc vs current code (gaps and contradictions in `docs/r3_app_architecture.md`)

The doc declares itself "authoritative" but several parts contradict the code as it stands today, and a few are internally inconsistent. Worth flagging when working from it:

**Contradictions with current code (doc forbids; code does)**
- §2.4 / §5 / §20 forbid JPEG-per-frame replay buffers, Python-pushed frames, and MP4 as an active recording container. Current code does all three: `app/media/replay_buffer.py` writes JPEG thumbs + short muxed segments, `app/media/pipeline_manager.py` pushes frames from Python into GStreamer, and `AppSettings.recording_filename` defaults to `session_recording.mp4` with `audio_container = "mp4"`. The doc is the target, not a description.
- §3 / §5.4 / §6.3 require ProRes/DNxHR/MJPEG segments in `.mov`/`.mxf`/`.mkv`/`.ts` for the active recording. Current `MuxedMediaWriter` is wired around an MP4 container.
- §15 / §10.4 say replay must be unavailable when recording is not active and only completed recording segments are eligible. Current behavior runs rolling replay independently of long game recording — replay works as soon as the session starts.
- §3 mentions a single "operator playback controller"; the current app correctly runs **two** (`operator_controller` and `program_controller`). The doc's section §15.1 and the runtime ownership block in §3.2 are slightly inconsistent on this — code is right, doc text is loose.

**Internal inconsistencies in the doc**
- Section numbering is broken. After §16 ("Testing and Validation Strategy") the subsections are numbered §17.1/§17.2/§17.3/§17.4, then a new top-level §17 ("AI Coding Assistant Implementation Rules") appears with subsections §18.1/§18.2/§18.3/§18.4, and the next top-level becomes §18 with §17.x subsections nowhere. The numbering needs a pass.
- §21 references a path `docs/architecture/production-replay-architecture.md` that does not exist in this repo — the actual file is `docs/r3_app_architecture.md`.
- §1.1 and §3.2 are slightly inconsistent on whether replay is purely from the active recording or also from "instant-replay shortcut" buffers; §3.2 ultimately says "Replay playback reads from the recording store rather than from a feed pipeline branch" — this is the consistent position and the rest of the doc should be read through it.
- §19 ("Recommended Implementation Defaults") lists `recording segment duration: 4 seconds` and `instant replay shortcuts: 10, 30, 60, 120 seconds`. The current code's `replay_buffer_seconds = 120` is a single rolling-window number with no notion of segment duration; the gap is real and intentional, but worth calling out as Phase-4 work.

**Gaps (real but unspecified)**
- No statement on how the per-feed `SessionClock` / PTS map is initialized when a feed joins late versus when a feed reconnects mid-recording. §8.4 says "feed joins/leaves should be represented in the timeline" but does not say where that representation lives (segment metadata? feed timeline table? both?).
- No spec for what happens to the in-memory replay index across an app restart of an active session. §6.4 says "rebuild in-memory indexes on startup" but §10.4 implies replay is unavailable outside `recording_state == RECORDING` — so the rebuild semantics for a crash-and-resume are undefined.
- No spec for how operator clip markers (§6.7) interact with multi-feed segment boundaries when a play crosses a segment edge on one feed but not another.
- No retention/cleanup policy for `processed/` MP4 deliverables or `quarantine/` segments — only a "do not delete recording media" rule (§6.6).
- No definition of "finalized session" used by the post-session processor in §3.2 / §18 Phase 8 — the state machine in §10 doesn't include a `FINALIZED` session state.

If you want, I can either prepare a patch to fix the numbering / dead path / contradictions in the doc, or open issues for the gaps. Most of the contradictions are already implicit in the doc's "this is the target, not the current code" framing — they only need to be made explicit at the top.
