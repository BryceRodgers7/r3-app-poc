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
python -m unittest tests.test_segment_replay_store           # one module
python -m unittest tests.test_segment_replay_store.ReplayStoreResolveTests.test_resolve_to_first_segment   # one case
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
       │     ├─ shared SegmentIndex (in-memory) + RecordingSegmentReplayStore
       │     └─ for each enabled feed:
       │           Source (NDI / synthetic)
       │           PreviewOutput
       │           PipelineManager (owns the GStreamer graph + tee fan-out
       │                            + splitmuxsink-driven recording branch
       │                            that writes one MKV per segment +
       │                            inserts segment rows into MetadataDb +
       │                            SegmentIndex)
       │           FeedRuntime (bundles the above)
       │     └─ RecordingManager (just owns the global RecordingState machine)
       ├─ Two MultiFeedOutputRenderer instances (operator, program)
       ├─ Two PlaybackController instances built inside the coordinator
       │     (operator: full transport; program: live_only=True)
       └─ Two MainWindow instances (operator shows controls; program does not)
```

Key invariants worth knowing before editing:

- **Per-feed everything.** `FeedRegistry` → `FeedRuntime` → its own `PipelineManager` / `PreviewOutput`. Do not reintroduce a single global source or shared preview surface.
- **Two output channels.** Operator and program windows each have their own `PlaybackController`, `MultiFeedOutputRenderer`, and `MainWindow`. Program is `live_only`; only the operator has pause / replay / slow / jump-to-live. Long recording continues regardless of what either window is showing.
- **Recording is segmented and native.** Each feed's `PipelineManager` runs a `tee → valve → videoconvert → jpegenc → splitmuxsink` branch (slice 4.A). Segment files land under `recording/{feed_id}/segment_NNNNN.mkv`; segment metadata (PTS span, frame count, file size) is persisted to a SQLite `segments` table and indexed in-memory by `SegmentIndex` (slice 4.B). Slice 4.D removed the legacy `Recorder` / `MuxedMediaWriter` / `ReplayBuffer` / `ReplayStoreManager` rolling-frame stack — don't reintroduce them.
- **Startup recovery (slice 4.E)** runs in `ApplicationCoordinator.initialize` before a new session is created. `app/storage/session_recovery.py` marks unfinished `session.json` files as `dirty`, validates prior `recording/<feed_id>/segment_*.mkv` files via `cv2.VideoCapture`, quarantines corrupt files to `<session>/quarantine/<feed_id>/`, and inserts `dirty` SQLite rows for valid in-progress files that lost their finalize step. The §11.4 "Resume / End and finalize / Discard" prompt is still deferred — 4.E only writes the data that prompt will react to.
- **Replay query goes through `RecordingSegmentReplayStore`** (slice 4.C). It enforces §10.4 / §6.6 (replay only while `recording_state == RECORDING`; only `state == "complete"` segments are returned) and resolves a target PTS to a `(segment_file, offset)` location. Slice 4.C.tail wired this into the operator: `PlaybackController` runs in PTS-ns time and renders decoded segment frames via `SegmentDecoder` (`app/media/segment_decoder.py`, an MJPEG `cv2.VideoCapture` wrapper) through the existing `OutputRenderer.show_frame` path. Multi-feed synchronized replay still needs Phase 5's `SessionClock` — for now only the primary feed shows rewound video.
- **Long game recording is opt-in.** Sessions are created on startup, but recording on disk only starts when the operator presses "Start game recording" (`ApplicationCoordinator.toggle_long_session_recording`).
- **`PlaybackController` still leans on the primary feed** (first enabled feed) for some replay paths — `_primary_runtime`, `_primary_feed_id`. True symmetric multi-feed replay is not yet implemented.
- **Frames that flow through Python** (BGR `MediaFrame` for the synthetic source) are the transitional path — the production target is for these to stay inside GStreamer.

### Layer map

- `app/config/` — `AppSettings` dataclass + TOML loader (legacy `[source]` and new `[[feeds]]`).
- `app/core/` — coordinator, registry, playback controller, app state, signals, dataclasses (`models.py` defines `MediaFrame`, `AudioChunk`, `SessionPaths`, `Segment`, `PlaybackMode`).
- `app/media/` — sources (`source_interface`, `source_factory`, `ndi_receiver`, `test_source`), pipeline (`pipeline_manager` — owns ingest tee + splitmuxsink-driven recording), `recording_manager` (just the `RecordingState` machine), output (`output_renderer`, `preview_output`), telemetry/overlay helpers.
- `app/storage/` — filesystem layout (`file_manager`), SQLite metadata (`metadata_db`, with the `segments` table), session lifecycle (`session_manager`), in-memory segment index (`segment_index`), replay query layer (`segment_replay_store`), startup crash recovery (`session_recovery`).
- `app/ui/` — `MainWindow` (re-used for both operator and program with flags), `controls_widget`, `multi_feed_video_panel`, `status_bar_widget`, `video_widget`.
- `tests/` — fast unit tests using stdlib `unittest`. They mock or stub GStreamer where needed; do not require real cameras to run.

## Working in this codebase

- Do not bypass the per-feed seams. Code that touches recording or ingest should go through `FeedRuntime`, `RecordingManager`, or each feed's `PipelineManager`. Replay queries go through `RecordingSegmentReplayStore`.
- The `PreviewOutput` / `OutputRenderer` split is intentional: a feed's `PreviewOutput` is its own ingest sink, while operator/program windows render via `MultiFeedOutputRenderer`. Do not collapse them.
- The synthetic source in `app/media/test_source.py` is the only non-NDI path the app builds. It is the dev fallback for camera-less environments — don't remove it as part of unrelated work.
- `MediaFrame` payloads are `numpy` BGR images (OpenCV ordering convention). When adding overlays, use `app/media/frame_overlay.py`.
- For UI changes, run `python main.py` and exercise the relevant transport (start/stop recording, rewind 10s, slow 1/2x, slow 1/4x, jump to live) — type checks won't catch playback regressions.

## Architecture doc vs current code (gaps and contradictions in `docs/r3_app_architecture.md`)

The doc declares itself "authoritative" but several parts contradict the code as it stands today, and a few are internally inconsistent. Worth flagging when working from it:

**Contradictions with current code (mostly resolved by Phase 4)**
- §2.4 / §5 / §20 used to forbid JPEG-per-frame replay buffers — slice 4.D removed `app/media/replay_buffer.py` and the rolling JPEG/segment path entirely. The synthetic dev source still pushes frames from Python through an `appsrc` (production NDI ingest goes native in Phase 3.A).
- §3 / §5.4 / §6.3 require ProRes/DNxHR/MJPEG segments in `.mov`/`.mxf`/`.mkv`/`.ts` for the active recording. Slice 4.A landed MJPEG-in-MKV via `splitmuxsink`; ProRes/DNxHR remain deferred (their encoders ship in `gst-plugins-bad`, which isn't always available on UCRT64).
- §15 / §10.4 say replay must be unavailable when recording is not active and only completed recording segments are eligible. `RecordingSegmentReplayStore` now enforces both rules; segment-file replay rendering itself lands in slice 4.C.tail.
- §3 mentions a single "operator playback controller"; the current app correctly runs **two** (`operator_controller` and `program_controller`). The doc's §15.1 and the runtime ownership block in §3.2 are slightly inconsistent on this — code is right, doc text is loose.

**Internal inconsistencies in the doc**
- Section numbering is broken. After §16 ("Testing and Validation Strategy") the subsections are numbered §17.1/§17.2/§17.3/§17.4, then a new top-level §17 ("AI Coding Assistant Implementation Rules") appears with subsections §18.1/§18.2/§18.3/§18.4, and the next top-level becomes §18 with §17.x subsections nowhere. The numbering needs a pass.
- §21 references a path `docs/architecture/production-replay-architecture.md` that does not exist in this repo — the actual file is `docs/r3_app_architecture.md`.
- §1.1 and §3.2 are slightly inconsistent on whether replay is purely from the active recording or also from "instant-replay shortcut" buffers; §3.2 ultimately says "Replay playback reads from the recording store rather than from a feed pipeline branch" — this is the consistent position and the rest of the doc should be read through it.
- §19 ("Recommended Implementation Defaults") lists `recording segment duration: 4 seconds` and `instant replay shortcuts: 10, 30, 60, 120 seconds`. The current code's `recording_segment_duration_seconds = 4.0` matches the segment cadence; the rewind-shortcut presets are a 4.C.tail concern.

**Gaps (real but unspecified)**
- No statement on how the per-feed `SessionClock` / PTS map is initialized when a feed joins late versus when a feed reconnects mid-recording. §8.4 says "feed joins/leaves should be represented in the timeline" but does not say where that representation lives (segment metadata? feed timeline table? both?).
- No spec for what happens to the in-memory replay index across an app restart of an active session. §6.4 says "rebuild in-memory indexes on startup" but §10.4 implies replay is unavailable outside `recording_state == RECORDING` — so the rebuild semantics for a crash-and-resume are undefined.
- No spec for how operator clip markers (§6.7) interact with multi-feed segment boundaries when a play crosses a segment edge on one feed but not another.
- No retention/cleanup policy for `processed/` MP4 deliverables or `quarantine/` segments — only a "do not delete recording media" rule (§6.6).
- No definition of "finalized session" used by the post-session processor in §3.2 / §18 Phase 8 — the state machine in §10 doesn't include a `FINALIZED` session state.

If you want, I can either prepare a patch to fix the numbering / dead path / contradictions in the doc, or open issues for the gaps. Most of the contradictions are already implicit in the doc's "this is the target, not the current code" framing — they only need to be made explicit at the top.
