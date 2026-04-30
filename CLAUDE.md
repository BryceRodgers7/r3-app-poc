# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working
with code in this repository.

## What this project is

Windows desktop proof-of-concept for live multi-feed sports video replay.
PySide6 UI on top of a GStreamer-centered media path (preview, file
recording, replay-from-segments). Production ingest is **NDI-only**
(`kind = "ndi"`); a synthetic test source (`kind = "synthetic"`) is the
dev fallback for camera-less work. Production NDI ingest is fully native
(Phase 3.A.2 + 3.A.3 retry — d3d11videosink preview + native source
chain); the synthetic source intentionally stays on the python_push path.
Two top-level windows (operator + program) drive two independent
`PlaybackController` instances over a shared graph of per-feed
`FeedRuntime`s.

## Where to find things

The design and implementation reference lives in the [`docs/`](docs/)
tree. Start at [`docs/README.md`](docs/README.md) — it's a goal-driven
index ("I want to understand the system before proposing an
extension" / "I'm refactoring the pipeline" / etc.) that points at:

- [`docs/r3_app_architecture.md`](docs/r3_app_architecture.md) —
  declared **target** production architecture. It is aspirational; the
  current code does not yet conform to it. § numbers used elsewhere
  refer to this file.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) — **current
  implementation** reference: state machines, threading model,
  recording / replay / recovery lifecycle, schema evolution, layer
  map. **This is the doc to read when extending current behavior.**
- [`docs/GSTREAMER_INVARIANTS.md`](docs/GSTREAMER_INVARIANTS.md) —
  GStreamer construction rules. Read before refactoring
  `pipeline_manager.py`.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — current code's object graph
  as a status snapshot. Older framing; substantial overlap with
  `IMPLEMENTATION.md`.

When asked to design or refactor toward production, treat the target
spec as authoritative and `IMPLEMENTATION.md` + the source code as the
gap. When asked to fix or extend current behavior, read
`IMPLEMENTATION.md` and the actual code first — the target spec
describes intent, not implementation.

## Run / install / test

The Windows GStreamer + `gi` (PyGObject) stack must match the Python
interpreter — see [docs/UCRT64_DEVELOPMENT.md](docs/UCRT64_DEVELOPMENT.md)
for the MSYS2 UCRT64 setup. NDI feeds additionally require
`gst-plugins-rs` and the NewTek NDI runtime — see
[docs/NDI_SETUP.md](docs/NDI_SETUP.md).

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

Optional `app_settings.toml` in the working directory configures feeds
and paths. With `[[feeds]]` rows present, the legacy `[source]` block
is ignored and at least one feed must have `enabled = true`. See
`app_settings.toml.example` and `app/config/settings.py`.

## Working in this codebase

- **Don't bypass the per-feed seams.** Code that touches recording or
  ingest goes through `FeedRuntime`, `RecordingManager`, or each feed's
  `PipelineManager`. Replay queries go through
  `RecordingSegmentReplayStore`. See `IMPLEMENTATION.md` §2 for the
  full list of seams.
- **The `PreviewOutput` / `OutputRenderer` split is intentional** — a
  feed's `PreviewOutput` is its own ingest sink, while operator and
  program windows render via `MultiFeedOutputRenderer`. Don't
  collapse them.
- **The synthetic source in `app/media/test_source.py` is the only
  non-NDI path the app builds.** It is the dev fallback for
  camera-less environments — don't remove it as part of unrelated work.
- **`MediaFrame` payloads are `numpy` BGR images** (OpenCV ordering
  convention). When adding overlays, use `app/media/frame_overlay.py`.
- **For UI changes, run `python main.py`** and exercise the relevant
  transport (start/stop recording, rewind 10s, slow 1/2x, slow 1/4x,
  jump to live, Replay Play). Type checks won't catch playback
  regressions.
- **When in doubt about an invariant**, check `IMPLEMENTATION.md`
  before reading code. Most non-obvious behaviors are documented
  there with file:line refs.

## Codebase conventions

- Tests use stdlib `unittest`, not pytest. Match the existing style
  in `tests/`.
- Logging via `LOGGER = logging.getLogger(__name__)` per module. No
  print statements in production code.
- New SQLite columns/tables: extend `MetadataDb._initialize_schema`,
  not a separate migration framework. The Phase 7.F migration in
  `_migrate_segments_unique_constraint_locked` is the model for any
  destructive migration.
- New health-event categories: add them where they're emitted, no
  central registry. The `HealthSeverity` enum is in
  `app/core/health_events.py`.
- New state machines: build via the generic `StateMachine[E]` in
  `app/core/state_machine.py`. The framework rejects illegal
  transitions and emits an `invalid_transition` health event so
  bugs surface in the JSONL log.
