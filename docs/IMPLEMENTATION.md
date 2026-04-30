# r3-app — Current Implementation Reference

This doc describes how the system **currently** works — the runtime
graph, the state machines, the threading model, the recording and
replay lifecycle, recovery semantics, and the load-bearing invariants
that aren't obvious from any single file.

**Companion docs:**

- [`r3_app_architecture.md`](r3_app_architecture.md) — target spec.
  This doc is the implementation; that doc is the goal. Cross-references
  use `§N.M` to point at sections of the target spec.
- [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md) — GStreamer
  construction rules. Read before refactoring `pipeline_manager.py`.
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — current code's object graph,
  written as a status snapshot. Substantial overlap with this doc;
  `ARCHITECTURE.md` is the older "design verdict" framing, this doc is
  the deep reference.

---

## Table of contents

1. [Application object graph](#1-application-object-graph)
2. [Per-feed seams (don't bypass)](#2-per-feed-seams-dont-bypass)
3. [State machines](#3-state-machines)
4. [Threading model](#4-threading-model)
5. [Recording lifecycle](#5-recording-lifecycle)
6. [Replay model](#6-replay-model)
7. [Recovery model](#7-recovery-model)
8. [Schema evolution](#8-schema-evolution)
9. [Storage layout](#9-storage-layout)
10. [Observability surfaces](#10-observability-surfaces)
11. [Operator UI invariants](#11-operator-ui-invariants)
12. [Layer map](#12-layer-map)

---

## 1. Application object graph

```
main.py
  └─ build_application
       ├─ AppSettings.load()                       # app/config/settings.py
       ├─ FileManager + MetadataDb + SessionManager  # app/storage/*
       ├─ build_default_application_coordinator    # app/core/application_coordinator.py
       │     ├─ FeedRegistry                       # one FeedDefinition per [[feeds]] row
       │     ├─ shared SegmentIndex (in-memory) + RecordingSegmentReplayStore
       │     ├─ SessionClock                       # one per app run, monotonic-anchored
       │     ├─ PlayManager                        # plays SQLite owner
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

`ApplicationCoordinator` is the single owner of every long-lived runtime
object except the two `MainWindow`s (which `main.py` builds so the
coordinator stays UI-agnostic).

### 1.1 Builder phase

`build_default_application_coordinator` runs **before** `initialize()`.
It builds the runtime graph but doesn't start any thread or open any
durable resource:

1. `FeedRegistry.build_default(settings)` — read `[[feeds]]` rows.
2. **Phase 7.A disk-budget assessment** — `assess_disk_budget(...)`
   runs and `_log_disk_budget_assessment` emits a single log line
   (`INFO`/`WARNING`/`ERROR` per verdict) **before any feed is
   constructed**, so the verdict appears at the top of the log file
   rather than buried under feed-startup noise. The corresponding
   **health event is deferred** to `initialize()` because the JSONL
   log isn't open yet.
3. Construct `RecordingManager`, `TelemetryHub`, `SegmentIndex`,
   `SessionClock`, `PlayManager`. Order matters here too:
   - `telemetry_hub.register_recording_state(...)` runs **before any
     feed registers a queue-depth sampler** so saturation-driven
     `RECORDING → RECORDING_ERROR` transitions have a target machine.
4. For each enabled feed: build source + `PipelineManager` +
   `FeedMetrics` + `FeedState` machine + `FeedRuntime`. Wire telemetry
   samplers, set metadata DB / segment index / session clock on each
   `PipelineManager`.

The coordinator instance is returned without anything started. The
two `PlaybackController`s are constructed in `__init__` (they need
the runtime list); their `initialize()` is deferred to step §1.2
below.

### 1.2 `initialize()` ordering

`ApplicationCoordinator.initialize(resume_session_id=None)` is
sequence-dependent. Reordering breaks specific things; the comments
below name what.

1. **Recovery scan happens before the coordinator is constructed.**
   `main._run_startup_recovery_flow` runs `mark_dirty_sessions` +
   `validate_session_segments` + `find_dirty_sessions` and shows the
   §11.4 dialog. By the time `initialize()` runs, every prior
   session's manifest is in `finalized` / `created` / `archived`
   state, and the operator's Resume choice (if any) is already
   threaded in via `resume_session_id`.
2. **Adopt-or-create the session.** Resume → `adopt_session(id)` (no
   directories created, no SQLite session row inserted, state machine
   loads at `DIRTY → CREATED`). Fresh → `start_new_session(label)`.
3. **`_populate_segment_index_from_resume(session_id)`** — only on
   the resume path. Without this, replay queries against the resumed
   session would only see segments produced *after* resume.
4. **`_setup_resume_continuation(session_paths)`** — only on the
   resume path. Detects the crashed game folder, computes earliest
   start + latest end across its segments, calls
   `session_clock.rebase(latest_end + 1ms)` so post-resume
   session-time values are strictly greater than pre-crash. Stashes
   `_ResumeContinuation`. Also runs
   `play_manager.auto_close_open_plays_for_session` here so the
   `UNIQUE(session_id, game_subdir, play_number)` check has a clean
   view before the next `start_game` runs.
5. **Open the health-events log** (`default_health_log().open(...)`).
   Opening earlier would risk recording into the previous session's
   log if it was still open; opening later would lose any startup
   events.
6. **`_emit_disk_budget_health_event()`** — emits the deferred
   Phase 7.A verdict event from §1.1. Goes into the now-open JSONL.
7. **Start every `FeedRuntime`.** Each connects its source and brings
   the GStreamer pipeline to PLAYING. Once started, each feed begins
   producing buffers and ticking metrics.
8. **Initialize both `PlaybackController`s.** Adds live-frame and
   live-overlay listeners; runs the first `refresh_recording_state()`
   so the overlays start at the right state.
9. **Start the `TelemetryHub`** — its periodic registrar (Qt-timer
   backed) begins firing the 1Hz log line + queue-depth sampling +
   health-event evaluation. Started last so all samplers are
   registered before the first tick.

The `_setup_resume_continuation` helper is **call-site-gated, not
internally gated** — it's only invoked from the resume branch. It
would be a defensive no-op on a fresh session (the "no `game_NNN/`
folder" bail-out fires), but the call-site discipline is what
prevents incorrect invocation.

---

## 2. Per-feed seams (don't bypass)

The graph is intentionally **per-feed**. Code that touches recording,
ingest, or replay must go through one of these seams:

- **`FeedRuntime`** — bundles `Source` + `PipelineManager` + `PreviewOutput`
  for one feed. Live-frame and live-overlay listeners attach here.
- **`PipelineManager`** (one per feed) — owns the GStreamer graph,
  the splitmuxsink recording branch, segment-row insertion, queue-depth
  sampling, audio dynamic wiring.
- **`RecordingManager`** — owns just the global `RecordingState`
  machine. After Phase 4.D it does not hold per-feed `Recorder`
  instances.
- **`RecordingSegmentReplayStore`** — the only contract the
  `PlaybackController`s use for replay eligibility / lookup.

Do not reintroduce a single global source, a single shared preview
surface, a per-feed `Recorder` class, or a per-feed `ReplayStore`
instance — the Phase 4.D removal of those was deliberate.

The `PreviewOutput` / `OutputRenderer` split is also intentional: a
feed's `PreviewOutput` is its own ingest sink, while operator and
program windows render via `MultiFeedOutputRenderer`. They are not
collapsible.

---

## 3. State machines

Five state machines coexist. Four are independent authoritative
sources; the fifth (`AppState`) is a derived view computed on demand.

| Machine | Owner | Persisted | Drives |
|---|---|---|---|
| `FeedState` (per feed) | `PipelineManager` (via `set_feed_state`) | no | diagnostics widget, telemetry health events, `AppState` aggregator |
| `RecordingState` (global) | `RecordingManager` | no | `enable/disable_file_recording`, `replay_available` AND-gate, status overlay |
| `ReplayState` (per operator controller) | `PlaybackController.replay_state` | no | transport-method gating, replay overlay |
| `SessionState` (global) | `SessionManager._active_session_state` | yes — `session.json` per session | recovery dirty-detection, post-session processor refusal logic |
| `AppState` (derived) | `compute_app_state()` | no | top-level UI status row, diagnostics |

### 3.1 FeedState

**Source:** `app/core/feed_state.py`. Spec: §10.2.

States: `DISABLED`, `CONNECTING`, `LIVE`, `DEGRADED`, `DISCONNECTED`,
`RECONNECTING`, `FAILED`.

Producers:
- Live pipeline `ERROR` bus message → `DISCONNECTED` (via
  `_poll_bus_for_messages` in `pipeline_manager.py`).
- Sustained `qos` dropped-buffer rate ≥ 1.0/sec → `DEGRADED` (via
  `TelemetryHub`'s feed-state evaluator).
- Sustained record-queue saturation ≥ 75% for ≥ 2 ticks → `DEGRADED`
  (slice 3.B).
- Sustained preview-queue saturation ≥ 75% for ≥ 2 ticks → `DEGRADED`
  (slice 3.B).
- Telemetry zero-fps streak (3 samples) → `DISCONNECTED` (fallback for
  sources that never emit a bus error).

Side effects: the `make_feed_state_machine` factory installs an
`on_enter` hook that emits `feed_lost` / `feed_recovered` health events
on enter to / exit from the `_BAD_STATES` set.

### 3.2 RecordingState

**Source:** `app/core/recording_state.py`. Spec: §10.3.

States: `NOT_RECORDING`, `STARTING_RECORDING`, `RECORDING`,
`STOPPING_RECORDING`, `FINALIZING`, `RECORDING_ERROR`.

Producers (all in `ApplicationCoordinator.toggle_long_session_recording`):
- Start press: `NOT_RECORDING → STARTING_RECORDING → RECORDING`.
- Stop press: `RECORDING → STOPPING_RECORDING → FINALIZING → NOT_RECORDING`.
- Sustained record-queue saturation: `RECORDING → RECORDING_ERROR`
  (slice 3.B).

Recovery: from `RECORDING_ERROR`, the only allowed transitions are back
to `NOT_RECORDING` or `STARTING_RECORDING`.

### 3.3 ReplayState

**Source:** `app/core/replay_state.py`. Spec: §10.4 (after the Gap 6.1
edit — the doc no longer lists the old `REPLAY_AVAILABLE` state).

States: `REPLAY_UNAVAILABLE_NOT_RECORDING`, `LIVE_WHILE_RECORDING`,
`SEEKING`, `REPLAYING`, `PAUSED`, `SLOW_MOTION`, `JUMPING_TO_LIVE`,
`REPLAY_DEGRADED`.

Producers: every `PlaybackController` transport method
(`pause_playback`, `rewind_10_seconds`, `replay_current_play`,
`set_playback_rate`, `jump_to_live`) and the
`_sync_replay_state_with_recording_locked` observer that mirrors
`RecordingState` start/stop.

Notable transition rules (enforced by the FSM, leaked into transport
method shape):

- The FSM **rejects direct** transitions out of `LIVE_WHILE_RECORDING`
  to `SLOW_MOTION` or `REPLAYING` — only `SEEKING`, `PAUSED`, and
  `REPLAY_DEGRADED` are valid targets. Transport methods that enter
  replay from LIVE bounce through `SEEKING → REPLAYING` first.
- `JUMPING_TO_LIVE` is the only state from which a return to
  `LIVE_WHILE_RECORDING` (recording active) or
  `REPLAY_UNAVAILABLE_NOT_RECORDING` (recording stopped) is allowed.
- A recording-stop mid-replay drives
  `(any active replay state) → JUMPING_TO_LIVE → REPLAY_UNAVAILABLE_NOT_RECORDING`.

The set `ACTIVE_REPLAY_STATES = {SEEKING, REPLAYING, PAUSED, SLOW_MOTION, JUMPING_TO_LIVE}`
is exported and used by `_sync_replay_state_with_recording_locked` to
detect "we were in a replay when recording stopped."

### 3.4 SessionState

**Source:** `app/core/session_state.py`. Spec: §10.6 (Gap 6.2 rewrite).

States: `CREATED`, `RECORDING`, `STOPPED`, `FINALIZED`, `DIRTY`,
`ARCHIVED`.

**This is the only durably persisted state machine.** Every
transition writes `<session>/session.json` atomically (`tmp + replace`).
The absence of `finalized_at` in that JSON is the marker the §11.4
recovery scan reads on next launch to detect crashed sessions.

Producers:
- `SessionManager.start_new_session` → `CREATED`.
- `ApplicationCoordinator.toggle_long_session_recording` → `RECORDING` /
  `STOPPED`.
- `SessionManager.close()` (typically at app shutdown) → `FINALIZED`.
- `SessionManager.adopt_session(session_id)` (Resume path) → loads at
  `DIRTY`, immediately drives `DIRTY → CREATED`.
- `session_recovery.mark_dirty_sessions` → `RECORDING/STOPPED → DIRTY`
  (writes the manifest directly, not through the state machine,
  because at scan time no live machine exists for prior sessions).

`STOPPED → FINALIZED` is **not** automatic per Stop press — between
games, `STOPPED` is the resting state. The transition only fires when
the session itself closes. Multiple `Start/Stop` cycles within one app
run all live in the same session.

### 3.5 AppState (derived)

**Source:** `app/core/application_state.py`. Spec: §10.1.

States (highest precedence first):
`SHUTTING_DOWN`, `ERROR`, `REPLAYING`, `PAUSED`, `SLOW_MOTION`,
`DEGRADED`, `RECORDING`, `PREVIEWING`, `STARTING`, `IDLE`.

`compute_app_state(feed_states, recording_state, replay_state,
shutting_down)` aggregates from the four authoritative machines. Not
itself a state machine — there's no transition table; the precedence
cascade is the rule.

`DEGRADED` is intentionally **below** the operator's active replay
states so that user-action context isn't buried under a side
indicator. The diagnostics widget surfaces all four sub-states
regardless, so the operator can still see what's degraded.

### 3.6 `StateMachine[E]` framework contracts

All four authoritative machines (3.1–3.4) are built on the generic
`StateMachine[E]` in `app/core/state_machine.py`. Three contracts the
framework enforces:

- **Same-state requests are silent no-ops.** `transition_to(target)`
  returns `False` and **does not fire `on_enter`** when `target ==
  current`. Side-effect-driven `on_enter` hooks therefore must not
  assume they run every time `transition_to(X)` is called with the
  current state X. They only run on a real change.
- **`on_enter` runs OUTSIDE the lock.** Mirrors the
  render-outside-lock pattern in `PlaybackController` (§4.3). Hooks
  may read state via the public `state` property, but recursive calls
  to `transition_to` from inside an `on_enter` hook would deadlock
  the internal `_lock`.
- **Invalid transitions surface as `invalid_transition` health
  events** with `metadata={"machine", "from", "to"}`. Bugs don't
  disappear silently into a `False` return value; they appear in
  `health_events.jsonl` for postmortem.
- **`StateMachine.force(target)` is the init-only escape hatch.**
  Bypasses the transitions table, no `on_enter` fires. Currently
  unused in production (`make_session_state_machine` accepts
  `initial_state` instead). Future code that needs to set state
  outside the validation rules should reuse `force()` rather than
  reinventing.

---

## 4. Threading model

### 4.1 Threads

Per app, at runtime:

- **Qt main thread** — UI, transport button presses, recording start/stop,
  Qt timer callbacks (`_on_replay_timer_tick`, `_on_overlay_timer_tick`),
  health-event emission from those.
- **GStreamer streaming thread (per pipeline)** — buffer probes
  (`_on_jpegenc_buffer_probe`, `_on_native_preview_buffer_probe`,
  `_on_record_branch_buffer_probe`, audio-presence probe), appsink
  `new-sample` signal handlers, splitmuxsink's `format-location`
  callback, `prepare-window-handle` sync bus messages.
- **Bus monitor thread (per pipeline)** — `_monitor_bus_loop` polls
  asynchronous bus messages (`ERROR`, `WARNING`, `INFO`, `EOS`, `QOS`).
- **Frame feed thread (python_push only)** — `_feed_appsrc_loop`,
  pushes synthetic frames into `appsrc`. Native sources don't have
  one — they ingest natively.
- **Audio feed thread (python_push only)** — `_feed_audio_appsrc_loop`,
  same shape for audio.
- **splitmuxsink async-finalize worker** — internal to GStreamer when
  `async-finalize=True` is set. Writes the matroskamux trailer
  off-thread so the streaming thread doesn't block on disk flush.

### 4.2 Locks

Each lock has a single purpose; ordering is informal (no canonical
acquire order across all locks) but in practice nested acquisition is
limited:

| Lock | Owner | Protects | Type |
|---|---|---|---|
| `_pipeline_lock` | `PipelineManager` | tree mutations against streaming-thread reads | `threading.Lock` |
| `_metadata_lock` | `PipelineManager` | `_frame_metadata` ring buffer (python_push appsink callback) | `threading.Lock` |
| `_write_lock` | `MetadataDb` | SQLite writes from streaming thread + Qt main thread | `threading.Lock` |
| `_lock` | `SegmentIndex` | per-feed list mutations | `threading.Lock` |
| `_lock` | `PlaybackController` | UiState + replay clock | **`threading.RLock`** |
| `_lock` | `PlayManager` | currently-open-play pointer | `threading.Lock` |
| `HealthEventLog._lock` | `HealthEventLog` | append-only JSONL writes | `threading.Lock` |

### 4.3 Cross-thread patterns

**State-under-lock + render-outside-lock (`PlaybackController`).**
Every transport method has the same shape: mutate UiState under
`self._lock`, capture the target session-time, drop the lock, call
`_render_at_session_time_ns(target)`, emit state. The render path
calls `OutputRenderer.show_frame(frame)`, which can emit Qt signals
that bounce back into the controller — holding the lock during render
would deadlock. **`self._lock` is `RLock`, not `Lock`,** because the
render path re-enters the controller to update
`feeds_in_freeze_frame` (`_render_at_session_time_ns` line ~686).

**SQLite cross-thread access (`MetadataDb`).** The connection is
opened with `check_same_thread=False` and every write is gated on
`_write_lock`. Inserts come from both the streaming thread (segment
rotation via splitmuxsink's `format-location`) and the Qt main thread
(recording stop, post-session processor). Reads are also serialized
through the lock for simplicity — replay queries are not
high-frequency.

**Streaming-thread-safe segment-row insert path.** The
`_finalize_pending_segment_locked` call inside the
`format-location` callback (and inside `disable_file_recording`) reads
the in-memory `_pending_segment` dict and writes a `Segment` row
synchronously. The dict is updated only by the same callback chain
plus `_on_jpegenc_buffer_probe` (also streaming-thread), so no lock
is needed on the dict itself; the SQLite write goes through
`MetadataDb._write_lock`.

**Qt-main-thread health events.** `record_health_event` is safe to
call from any thread (the `HealthEventLog` is fully locked). Health
events emitted from streaming-thread probes (e.g. `audio_missing`)
go through the same path as Qt-main-thread emissions.

---

## 5. Recording lifecycle

### 5.1 Pipeline construction

`PipelineManager._build_pipeline` (called once when the source
connects) builds the full graph:

```
SOURCE  (native NDI elements OR python_push appsrc)
   ↓
videoconvert (only on python_push path)
   ↓
source_tee  ──┬──→  preview branch (per-mode)
              └──→  record branch (queue → valve → videoconvert → I420/bt601 capsfilter → jpegenc → splitmuxsink)

audio_tee  ──┬──→  live audio (queue → valve → audioconvert → audioresample → wasapisink)
             └──→  audio_record drain appsink (always built — drains the tee even when audio_record into splitmuxsink is not wired)
             ↓ (Phase 9.C dynamic, conditional)
             audio_record branch (queue → valve → audioconvert → audioresample → opusenc → splitmuxsink.audio_%u)
```

Preview branch shape depends on `source.pipeline_mode`:

- **NATIVE** + not `force_python_push_preview` → native preview branch
  with `d3d11videosink` per window.
- **PYTHON_PUSH** OR escape-hatch → legacy `appsink → QImage` path.

### 5.2 Start (coordinator orchestration)

`ApplicationCoordinator.toggle_long_session_recording` Start path,
in this exact order:

1. **`RecordingState.NOT_RECORDING → STARTING_RECORDING`.**
2. **Decide game folder.** If `_resume_continuation` is set (Phase 7.D
   resume path, first Start after Resume): reuse
   `continuation.game_subdir`, take `game_start_session_time_ns`
   from the continuation, **clear `_resume_continuation` immediately**
   (one-shot — clearing before the per-feed loop ensures a partial
   failure can't leave a stale continuation that a retry would reuse).
   Otherwise: allocate a fresh `game_NNN/` via `find_next_game_index`
   and capture `session_clock.now_session_time_ns()` as the game start.
3. **For each enabled feed:**
   - Compute `start_fragment_index` via
     `find_next_fragment_index(feed_game_dir)` — scoped to the
     per-feed game folder, so a fresh game returns 0 (folder doesn't
     exist) and a resumed game returns `max+1` past pre-crash files.
     The dual-source variant
     (`find_next_fragment_index(recording_dir, db, session_id, feed_id)`)
     is used for the §11.4 quarantined-tail case where the file is
     gone but the DB row's `fragment_index` is still present.
   - Call `pipeline_manager.enable_file_recording(session_paths,
     feed_id, start_fragment_index, game_subdir)`.
4. **Set the per-game replay filter** —
   `replay_store.set_current_game_start_session_time(game_start_ns)`.
   For resume continuation this is the crashed game's earliest start
   so pre-crash segments stay visible; for a fresh game it's "now".
5. **Open the play.** `play_manager.start_game(session_id,
   game_subdir, play_start_ns)`. Play #1 for a fresh game;
   `max(existing) + 1` on resume (the auto-close from
   `_setup_resume_continuation` already ran). `play_start_ns` is the
   *now* reading on a resumed game (so the new play starts at the
   resume moment, not at the crashed game's earliest), and equals
   `game_start_ns` on a fresh game.
6. **`RecordingState.STARTING_RECORDING → RECORDING`.**
7. **Drive the `SessionState` manifest forward.** Fires from both
   `CREATED → RECORDING` (first Start of the run) and
   `STOPPED → RECORDING` (multi-game flow — second/Nth Start within
   the same session). Without the second case, `session.json` would
   stay at `"stopped"` while game N+1 is actively recording, which
   is misleading for any external tool reading the manifest.
8. **Refresh both controllers' overlays.**
   `operator_controller.refresh_recording_state()` +
   `program_controller.refresh_recording_state()` so the LIVE overlay
   updates immediately rather than waiting for the next live frame.

`pipeline_manager.enable_file_recording` itself:

- Branches on `_recording_was_disabled`:
  - Flag false (first Start): call
    `_ensure_audio_record_branch_built_locked` (Phase 9.C late-build).
  - Flag true (Start after Stop): call `_rebuild_splitmuxsink_locked`
    — see §5.5 below.
- Open the record valve (`_set_branch_enabled("record", True)`).
- Set `_recording_running = True`, clear `_recording_was_disabled`.

### 5.3 Stop (coordinator orchestration)

`ApplicationCoordinator.toggle_long_session_recording` Stop path,
in this exact order:

1. **`RecordingState.RECORDING → STOPPING_RECORDING`.**
2. **For each feed: `pipeline_manager.disable_file_recording()`** —
   the 300ms split-now ritual (§5.4 below). Each feed's last segment
   gets its trailer; the throwaway post-split segment is dropped from
   the DB.
3. **`RecordingState.STOPPING_RECORDING → FINALIZING → NOT_RECORDING`.**
4. **Close the play** —
   `play_manager.stop_game(session_clock.now_session_time_ns())`.
   Captured **after** recording stops, not before, so the play's
   `end_session_time_ns` aligns with the last frame the operator
   actually saw recorded (not with the moment the Stop button was
   pressed). The session-clock reading at this point sits past the
   splitmuxsink trailer write.
5. **Clear the per-game replay filter** —
   `replay_store.set_current_game_start_session_time(None)`. The
   recording-state gate already idles replay queries between games,
   but clearing avoids stale filter state.
6. **`SessionState.RECORDING → STOPPED`** (if the session machine was
   in `RECORDING` — defensive guard for paths that somehow reach Stop
   from a different state).
7. **Refresh both controllers' overlays.**

### 5.4 Stop (the 300ms split-now ritual, per feed)

`pipeline_manager.disable_file_recording`:

1. Emit `splitmuxsink.split-now` **while the valve is still open** so
   the next buffer triggers rotation. matroskamux writes the EBML
   trailer for the in-flight segment as part of that rotation.
2. Sleep 300ms — empirical, generous for 30 fps (one frame every
   33 ms) plus the trailer write itself (a few KB). On a slow disk
   this can be insufficient and the disable path falls back to
   "best-effort, may lack trailer" + a warning log.
3. Close the valve. Any post-split frames already captured into the
   throwaway "dud" segment are discarded.
4. If the segment counter advanced during the sleep (= split fired),
   drop the post-split pending segment without recording (it has
   no buffers / no trailer).
5. Finalize the in-flight pending segment via
   `_finalize_pending_segment_locked` — writes the row to SQLite +
   `SegmentIndex`.
6. `splitmuxsink.set_state(NULL)` to fully tear down.
7. Set `_recording_was_disabled = True`.

**Known artifact:** the rotation triggered by `split-now` opens an
empty/short "dud" segment file that we drop from the DB. The file
remains on disk and is unwatchable; the next-launch recovery scan
quarantines or marks it dirty. This is acceptable.

### 5.5 Stop/Start cycle (rebuild ordering)

State-cycling the existing splitmuxsink (NULL → PLAYING) is **not
sufficient** — splitmuxsink retains an internal "current file" pointer
across state changes, so post-Start buffers append to the previous
game's last file. `_rebuild_splitmuxsink_locked` builds a fresh
element from scratch, in this exact order:

1. Unlink `jpegenc → old splitmuxsink`.
2. `old.set_state(NULL)`; remove from pipeline.
3. Build a fresh splitmuxsink with the same config; add to pipeline.
4. Point `self._splitmuxsink` at the new element. *(critical — the
   helpers below operate on whatever this attribute references.)*
5. Call `_ensure_audio_record_branch_built_locked` (Phase 9.C —
   late-build the audio chain if audio appeared between games and
   the chain wasn't built yet).
6. Call `_link_audio_encoder_to_splitmuxsink_locked` (Phase 7.G —
   re-link the existing audio encoder's src pad to the new
   splitmuxsink's `audio_%u` pad).
7. `jpegenc.link(new_sink)`.
8. `new_sink.sync_state_with_parent()`.

**Order matters.** Reversing steps 5 / 6 with step 7 silently
produces video-only segments because of the audio-pad-request order
rule: see [GSTREAMER_INVARIANTS.md §3](GSTREAMER_INVARIANTS.md). On
top of that, `_link_audio_encoder_to_splitmuxsink_locked` itself does
a defensive `unlink(peer)` first, since the encoder's src pad is
still peer-linked to the *old* splitmuxsink at rebuild time
(GSTREAMER_INVARIANTS §5).

### 5.6 Per-game folder allocation

Per `r3_app_architecture.md` §6.2.1. Each Start press allocates a
fresh `game_NNN/` subdir under `<session>/recording/` via
`find_next_game_index` (`max(NNN) + 1`). The game folder owns:

- Per-feed segment files (`<feed_id>/segment_NNNNN.mkv`).
- The implicit Play #1 boundary (and any Next Play boundaries within
  the game).

`fragment_index` resets to 0 each game — per-game folder isolation
makes this safe (cross-game name collisions are impossible because the
game name is part of the path). For Phase 7.D resume continuation the
game folder already has files and `find_next_fragment_index` returns
`max+1` past them.

### 5.7 Audio dynamic wiring (Phase 9.C)

The audio chain is built in two parts:

- **Eagerly at pipeline construction** (`_build_audio_path_locked`):
  audio tee, source-side audio chain, live audio sink (wasapisink),
  no-op record-side appsink (drains the tee).
- **Lazily on demand** (`_ensure_audio_record_branch_built_locked`):
  the record-into-splitmuxsink chain (`audioconvert → audioresample
  → opusenc → splitmuxsink.audio_%u`). Built only when:
  - `[recording] audio_enabled = true` in TOML, AND
  - the source's `_audio_format` is non-None, AND
  - the audio_record encoder isn't already built, AND
  - the audio-presence probe has observed at least one audio buffer
    on the tee, AND
  - the splitmuxsink exists.

The audio-presence probe sits on the tee's **sink pad** so it sees
buffers regardless of whether the audio_record branch is wired yet. The
opusenc src probe (Phase 7.E) is separate and lives on the encoder's
permanent src pad (see [GSTREAMER_INVARIANTS.md §4](GSTREAMER_INVARIANTS.md)
for why it's not on the splitmuxsink request pad).

Edge cases:

- **Operator presses Start before audio's first buffer arrives** —
  Game 1 records video-only. Game 2 (after Stop/Start) re-evaluates
  and picks audio up if it's flowing by then.
- **Source produces audio mid-game** — Game 1 stays audio-less
  (splitmuxsink can't add a sink pad to a running mux). Game 2 has
  audio after the rebuild.
- **Source loses audio mid-game** — Game 1 stays with audio (chain in
  place but no buffers). The `audio_missing` health event fires after
  a 5s grace period.

---

## 6. Replay model

### 6.1 Two-clock system

Replay queries operate in two timestamp domains:

- **PTS-time (per feed).** Each feed has its own monotonic
  `Gst.PTS_NS` timeline starting at 0 on the first buffer. Used by
  `RecordingSegmentReplayStore.resolve` (operator-target lookup) and
  by every `Segment` row's `start/end_pts_ns`.
- **Session-time (cross-feed, monotonic-anchored).** A single
  `SessionClock` instance per app run. `now_session_time_ns()` returns
  monotonic-anchored nanoseconds since the clock's origin. The
  `PipelineManager` captures `session_time_ns` at the **first buffer
  of every segment**, derives `pts_to_session_offset_ns =
  first_session_time_ns - first_pts`, and stamps the segment's
  `start_session_time_ns` / `end_session_time_ns` /
  `pts_to_session_offset_ns` fields at finalize. The replay layer
  uses session-time for all multi-feed queries (§8.6).

The `PlaybackController` runs in **session-time** (slice 5.C) — the
replay clock advances `_playback_session_time_ns` and per-tick decode
goes through `nearest_frame_location(feed_id, session_time_ns)` for
every feed.

### 6.2 `nearest_frame_location` clamping rule

Spec: §8.6.1.

Returns a `SegmentReplayLocation` for any feed that has at least one
replayable segment. Decision tree:

1. Replay not available (`recording_state != RECORDING` or per-game
   filter excludes everything) → `None`.
2. Feed has no completed segments with session-time → `None`.
3. `session_time_ns` falls inside a segment → exact match,
   `is_freeze=False`.
4. `session_time_ns` is **before** the feed's earliest segment → that
   earliest segment, offset 0, `is_freeze=True` (freeze on first
   frame).
5. `session_time_ns` is **after** all segments **or** in a gap → the
   latest segment ending at-or-before `session_time_ns`, offset
   clamped to its `duration_ns`, `is_freeze=True` (freeze on last
   frame).

The `is_freeze` flag drives the operator UI's "FROZEN" badge per tile
(Phase 6).

`resolve_session_time` is the strict counterpart — returns `None`
instead of clamping. Used by the rewind-target picker which wants to
know if there's actual coverage at the target.

### 6.3 Per-game replay scoping (Phase 7.B-ext)

`RecordingSegmentReplayStore` carries a
`_current_game_start_session_time_ns` filter, set by
`ApplicationCoordinator.toggle_long_session_recording` on each Start
press. All session-time queries
(`available_session_time_range`, `nearest_frame_location`,
`resolve_session_time`, `feeds_with_coverage_at`, per-feed
earliest/latest helpers) return only segments whose
`start_session_time_ns >= filter`.

Without this, after a Stop/Start cycle within one session the operator
could rewind into the previous game's recording and the status bar
would advertise stale ranges.

### 6.4 Replay-from-LIVE entry semantics

Slow-motion / pause / rewind from LIVE never anchor on "now" — they
anchor on `latest_replayable_session_time_ns`. Reason: "now" sits
inside the in-progress segment, which has no replay coverage, so
rendering at "now" would either freeze on a clamped frame or render
nothing.

- `set_playback_rate` from LIVE: snaps to `latest_replayable`,
  bounces FSM through `LIVE_WHILE_RECORDING → SEEKING → REPLAYING →
  SLOW_MOTION/PAUSED`.
- `_resolve_pause_anchor_locked` from LIVE: returns
  `latest_replayable`.
- `_resolve_rewind_target_locked` from LIVE: anchors on
  `latest_replayable`, subtracts `rewind_ns`. Anchor on the operator's
  current playback position from REPLAY/PAUSED so repeated clicks
  accumulate.

The bounce through `SEEKING → REPLAYING` is required because the FSM
rejects direct transitions out of `LIVE_WHILE_RECORDING` to anything
but `SEEKING`/`PAUSED`/`REPLAY_DEGRADED`. Future contributors who add
a new transport entry point from LIVE must keep the bounce.

### 6.5 Replay tick clamp

`_on_replay_timer_tick` clamps `target_session_time_ns = min(target,
latest_replayable)` on every tick. Without this, a 1.0x or
slow-motion replay would eventually advance past the live edge and
silently fall into the §8.6.1 "after coverage" freeze branch on every
feed. The clamp keeps replay tracking the live edge naturally.

### 6.6 Multi-feed render loop

`_render_at_session_time_ns(target)` iterates **every enabled feed**:

1. Look up `nearest_frame_location(feed_id, target, recording_state)`.
2. Decode via the per-feed `SegmentDecoder` (one
   `cv2.VideoCapture` per feed).
3. Push the frame into `OutputRenderer.show_frame` (which routes by
   `frame.feed_id`).
4. Track `is_freeze=True` results into `feeds_in_freeze_frame`.

Every operator tile renders something on every tick during replay.
Feeds with no replayable segment yet are the only blank-tile state —
those keep showing whatever frame they last received.

### 6.7 Smooth `seconds_behind_live`

Counter advances with wall-clock during PAUSE/REPLAY (1 second per
real second). Computed in `_update_state_timestamps_locked` as
`(session_clock.now_session_time_ns() - playback_session_time_ns) /
1e9`. Independent of segment finalization cadence. Falls back to
`(latest_replayable - playback) / 1e9` when no SessionClock is
attached (test fixtures only).

### 6.8 `replay_available` AND-gate

`UiState.replay_available = (latest_replayable_session_time_ns is not None)
AND (recording_state == RECORDING)`. The per-game replay scope alone
isn't sufficient — between a Stop and the next Start, finalized
segments still satisfy "exist" but the operator UI must not advertise
replay (would invite operator action that the §10.4 gate would then
reject confusingly).

### 6.9 Jump-to-live multi-feed snap-back

`_latest_live_by_feed: dict[feed_id, MediaFrame]` is updated for every
feed on every live frame; `jump_to_live` re-shows each feed's
snapshot through the renderer so secondary tiles snap back alongside
the primary. Without this, secondary tiles would stay on whatever
replay frame they last rendered until the next live frame from each
source arrives.

### 6.10 Plays + Replay Play (Phase 7.H)

`PlayManager` owns the in-memory currently-open-play pointer and
persists boundary transitions to the `plays` SQLite table. Lifecycle
hooks fire from the Qt main thread:

- `start_game(session_id, game_subdir, start_session_time_ns)` —
  opens Play #1 (or `max+1` on Phase 7.D resume).
- `mark_next_play(now_session_time_ns)` — closes the current play,
  opens the next.
- `stop_game(end_session_time_ns)` — closes the current play.

Plays are operator-scoped (one sequence per game across all feeds),
not feed-scoped. The current play number renders as `Play #N` on the
playback overlay (`PlaybackOverlayInfo.current_play_number`) and the
operator status bar.

The "Replay Play" transport seeks to the currently-open play's
`start_session_time_ns` (clamped defensively to the per-game replay
scope's earliest). Phase 8.D writes one `<game>/plays.json` sidecar
per game during post-session processing.

---

## 7. Recovery model

### 7.1 Dirty session detection

`session_recovery.mark_dirty_sessions` runs at app launch (before any
new session is created). Walks `<sessions_root>/session_*/session.json`.
A manifest with `state ∈ {recording, stopped}` and missing
`finalized_at` is rewritten in place (atomic `tmp + replace`) with
`state = "dirty"`. Idempotent: re-runs are no-ops.

Direct manifest write (not through the `SessionState` machine) is
deliberate — instantiating a live `SessionManifest` per session just
to mutate a JSON file would be wasteful, and there's no live machine
for a closed session anyway.

### 7.2 Segment validation + quarantine

`session_recovery.validate_session_segments` walks
`recording/` recursively (handles both legacy flat and per-game
nested layouts via `rglob`), feeds each `segment_*.mkv` through a
`SegmentValidator` (default uses `cv2.VideoCapture`), and reconciles
against SQLite:

- **Valid file + matching `complete` DB row** → no action.
- **Invalid file + matching DB row** → move to `quarantine/`,
  update DB row → `quarantined`, point `file_path` at new location.
  Quarantine collisions get a `.recovered_NNN.` suffix so re-runs
  don't lose data.
- **Valid file + no DB row** → insert as `dirty` with synthetic PTS
  metadata (see §7.4 below).

Files not matching the `segment_NNNNN.mkv` pattern are skipped
(zero-byte files, leftover tooling output, etc.).

### 7.3 Resume continuation (Phase 7.D)

When the operator picks Resume in the §11.4 dialog:

1. `SessionManager.adopt_session(session_id)` loads existing
   `SessionPaths` and starts the `SessionState` machine at `DIRTY`,
   then immediately drives `DIRTY → CREATED`.
2. The coordinator's `initialize(resume_session_id=...)` rebuilds the
   `SegmentIndex` from SQLite via `load_segment_index_for_session`
   (`complete` and `dirty` rows only — quarantined / corrupt are
   excluded so they never surface in replay).
3. `_setup_resume_continuation(session_paths)` runs:
   - Walks `<recording>/` for the highest `game_NNN/` folder.
   - Filters loaded segments to that folder by path-component match
     (`Path.parts` inspection, not substring — avoids the
     `game_001 ⊂ game_0011` trap).
   - Computes `min(start_session_time_ns)` and
     `max(end_session_time_ns)` across them.
   - Calls `session_clock.rebase(latest_end + 1ms)` so post-resume
     session-time values don't overlap pre-crash ones.
   - Stashes `_ResumeContinuation(game_subdir, game_start_session_time_ns)`.
4. `PlayManager.auto_close_open_plays_for_session` closes any plays
   with NULL `end_session_time_ns` at the latest finalized segment's
   end, sets `auto_closed_on_crash = TRUE`.
5. The first `toggle_long_session_recording` Start press consumes the
   continuation: reuses `continuation.game_subdir` (no new game
   folder), sets the per-game filter to `game_start_session_time_ns`
   (so pre-crash segments stay visible), then clears
   `_resume_continuation`. Subsequent Stop/Start cycles in the
   resumed run behave normally.

`SessionClock.rebase()` is safe **only before** any buffer has been
processed by the new clock — calling it after the first segment's
first-buffer probe would corrupt that segment's
`start_session_time_ns`.

### 7.4 Recovery edge cases

**cv2 fallback policy.** `_default_segment_validator` returns
`is_valid=True` when cv2 isn't importable, so a stripped-down install
(or a future deployment that drops the cv2 dependency) doesn't
quarantine real recordings on every launch. Future contributors
considering a "stricter default" should account for that — the
existing default is deliberate.

**Recovered "dirty" segments have synthetic PTS.** The original
splitmuxsink-assigned PTS is gone after a crash (only captured in
`_pending_segment` in memory, written by
`_finalize_pending_segment_locked` which fires when the *next*
segment opens — that didn't happen). `_build_dirty_segment` synthesizes
`start_pts_ns=0`, `duration_ns=cv2_duration`. Replay queries comparing
pre-crash and post-resume segments on the same feed mis-rank by PTS;
session-time queries are unaffected because session-time fields are
NULL on these rows, which silently excludes them (see §8.2).

**`find_next_fragment_index` consults BOTH disk AND DB.** Specifically
covers the §11.4 quarantined-tail edge case: file is gone from disk,
but the DB row at the original `fragment_index` is still present.
Without the DB consultation, the post-resume Start could pick a
`fragment_index` that filename-collides with the DB row's `file_path`.

**Resume offered only for the most recent dirty session.** The dialog
iterates dirty sessions in directory-sorted order; only `dirty[-1]`
is offered the Resume button. Older dirty sessions get only Finalize
and Discard. Reason: only one game folder can be the next-Start
target, the per-game replay filter takes one anchor, the clock
rebase takes one point. See `r3_app_architecture.md` §11.4.

**Resume manifest write happens twice.** `resolve_dirty_session(RESUME)`
writes `state = "created"` directly to the JSON, then
`SessionManager.adopt_session` runs the `DIRTY → CREATED` transition
through the state machine, which writes the same `created` state
again. Briefly inconsistent on disk (the first write is bypassing the
machine), but the second write is correct.

---

## 8. Schema evolution

The SQLite schema has migrated over time. Two facts matter for any
future migration:

### 8.1 Phase 7.F UNIQUE constraint migration

The `segments` table originally had `UNIQUE(session_id, feed_id,
fragment_index)`. Phase 7.B-ext made `fragment_index` reset per
game, and the per-game folder layout was added — so two games per
session shared a `fragment_index` and the constraint started silently
dropping segments after the first game's last fragment.

Phase 7.F replaced the constraint with `UNIQUE(file_path)`. The
migration in `MetadataDb._migrate_segments_unique_constraint_locked`
detects the legacy schema by substring-matching the stored CREATE
statement, then performs the rename + recreate + copy + drop dance
inside a transaction. Idempotent on already-migrated DBs.

### 8.2 Pre-5.A rows (NULL session-time fields)

Slice 5.A added `start_session_time_ns`, `end_session_time_ns`, and
`pts_to_session_offset_ns` to the `Segment` row. Rows created before
5.A have NULL values for these fields.

`SegmentIndex`'s session-time queries (`segments_overlapping_session_time`,
`feeds_with_coverage_at`, `earliest_session_time`,
`latest_replayable_session_time`, `cross_feed_session_time_range`)
**silently exclude** any segment whose session-time fields are NULL.
This is by design: pre-5.A rows predate the SessionClock origin so
they can't be safely placed on the session timeline.

PTS-time queries continue to work for those rows. The `Segment`
dataclass itself remains the same shape — the migration was additive,
not a schema change.

Practical implication: if you adopt a session created on a pre-5.A
build, its segments won't appear in any session-time replay. The
operator can still play them back via the older PTS-time path
(`RecordingSegmentReplayStore.resolve`), but `nearest_frame_location`
and the multi-feed render loop will skip them.

---

## 9. Storage layout

Mirrors `r3_app_architecture.md` §6.2 — that section is the
canonical reference. Quick visual:

```
<base_data_dir>/
  metadata.db                    # shared across every session
  sessions/
    session_001/
      session.json               # SessionState manifest
      logs/
        health_events.jsonl
      recording/
        game_001/                # one subdir per Start/Stop cycle
          ndi_main/
            segment_00000.mkv
            ...
        game_002/
          ndi_main/
            segment_00000.mkv
      processed/                 # post-session processor output
        game_001/
          ndi_main.mp4
          plays.json
      quarantine/                # created on demand by recovery
        ndi_main/
          segment_00007.mkv
```

Key invariants (from §6.2.1):

- `session_NNN` is allocated at app launch via `FileManager.get_next_session_id`
  (`max(NNN) + 1`). One session per app run.
- `metadata.db` lives at the base level, not inside each session, so
  the post-session processor can address every session through one
  SQLite file.
- `recording/<game_NNN>/<feed_id>/segment_NNNNN.mkv` — `fragment_index`
  resets per game, per-game folder isolation prevents cross-game
  filename collisions.
- `quarantine/` is created lazily by the recovery scan only when
  something needs quarantining.
- `logs/` is created up front by `FileManager.create_session_paths`.
- `processed/` is created by the post-session processor; not present
  during recording.

---

## 10. Observability surfaces

### 10.1 TelemetryHub

Per-feed runtime counters (`source_fps`, `preview_fps`, `recording_fps`,
`dropped_per_sec`, `python_frames_per_sec`, `pipeline_mode`, queue
depths). Emits a 1Hz log line per feed plus health-event side effects:
`feed_lost` after 3 consecutive zero-source-fps samples (the threshold
constant `FEED_LOST_ZERO_SAMPLES = 3`), `disk_low` below 5% free
(`DISK_LOW_FRACTION = 0.05`), paired `feed_recovered` / `disk_recovered`
on recovery.

`register_queue_depth_sampler` runs `PipelineManager.sample_queue_depths`
once per tick; sustained record saturation drives `RecordingState
RECORDING_ERROR` and sustained preview saturation drives
`FeedState DEGRADED` (slice 3.B).

**Invariants worth knowing before refactoring the hub:**

- **Qt-decoupled.** `start(periodic_registrar)` accepts a callable —
  production passes `_qt_periodic_registrar` (QTimer-backed) and tests
  drive `_log_all_snapshots` / `_log_disk_snapshot` directly without
  an event loop. Adding a Qt dependency to the hub would break unit
  tests that don't run an `QApplication`.
- **Saturation evaluation requires a 2-tick streak.** Single-tick
  spikes don't flap state (operator focusing/unfocusing windows,
  transient stalls). Threshold: 75% queue utilization, hardcoded.
  The `RECORDING → RECORDING_ERROR` transition only fires when
  currently `RECORDING` so a transient saturation during startup
  can't trip the error path.
- **Disk write-rate is volume-wide, not app-specific.** Computed
  from `shutil.disk_usage(path).free` deltas between samples, so it
  captures all writes to the underlying volume — including unrelated
  processes. The framing was "is the disk too slow?", not "how much
  is this app writing?".
- **`feed_recovered` has two emission paths.** (a) The `on_enter`
  hook on the `FeedState` machine itself (production path); (b) a
  legacy path inside `_evaluate_feed_health` for hubs without a
  registered state machine (tests/tooling that exercise the hub
  directly). Future code must avoid emitting `feed_recovered` from a
  third location — duplicate events would double-clear the dedup
  flag.

### 10.2 health_events.jsonl

`<session>/logs/health_events.jsonl` — append-only, one JSON object per
line. Schema in `r3_app_architecture.md` §14.6. `HealthEventLog` is
process-wide and thread-safe. Categories in active use:

- `feed_lost` / `feed_recovered`
- `disk_low` / `disk_recovered`
- `recording_started` / `recording_stopped` / `recording_error`
- `recording_branch_saturated` / `preview_branch_saturated`
- `audio_missing`
- `disk_budget_warn` / `disk_budget_over`
- `session_dirty` / `session_finalized`
- `invalid_transition` (state-machine consistency check)

**De-duplication via `has_open_event` / `clear_open_event`.** The
`record_health_event(...)` API does NOT auto-deduplicate; producers
must check `has_open_event(category=..., feed_id=...)` before emitting,
and `clear_open_event(...)` when the condition recovers. The pattern is:

```python
if not log.has_open_event(category="feed_lost", feed_id=fid):
    log.record(severity=WARNING, category="feed_lost", ...)
# ... later, on recovery:
log.record(severity=INFO, category="feed_recovered", ...)
log.clear_open_event(category="feed_lost", feed_id=fid)
```

Used by `feed_lost` / `recording_branch_saturated` / `disk_low` /
`session_dirty` / `recording_error`. Categories that fire as one-off
informational events (`recording_started`, `recording_stopped`,
`session_finalized`, `invalid_transition`) skip the dedup pattern.

### 10.3 Bus logging

`app/media/gst_bus_log.py`. Every GStreamer bus message is logged with
`feed_id` + `pipeline_role` (`live` after Phase 4.D — the legacy
`replay` and `replay-audio` roles are gone). Filter is
`ERROR | WARNING | INFO | EOS | QOS`.

### 10.4 Diagnostics widget

Operator-window only. Shows per-feed FPS / drops/sec / pipeline_mode /
queue gauges, a transitional pipeline banner when any feed runs
`python_push` (escalates to high-contrast warning when
`[app] app_mode = "production"`), the disk-budget readout, and the
latest-replayable surface.

### 10.5 Latency samplers

`replay_seek` (operator-initiated lookups via the replay store) and
`segment_write_video` / `segment_write_audio` (hot-path writes). Counts
+ avg + p95 + max per tick. Wired through `LatencyRegistry`.

**Adding a new measurement** is a one-liner via `time_block(name)`:

```python
from app.core.telemetry import time_block

with time_block("my_op"):
    do_the_thing()
```

The contextmanager records into the module-level `_LATENCY_REGISTRY`
singleton. The hub's 1Hz log emitter pulls every named sampler that
has at least one sample in its trailing window. No registration step
is needed — first `time_block(name)` call creates the sampler.

---

## 11. Operator UI invariants

### 11.1 Two output channels

Operator and program windows each have their own `PlaybackController`,
`MultiFeedOutputRenderer`, and `MainWindow`. Program is `live_only`;
only the operator has pause / replay / slow / jump-to-live / Replay
Play. Long recording continues regardless of what either window is
showing.

### 11.2 Native preview path (slice 3.A.3 retry)

NATIVE-mode sources (NDI) feed `d3d11videosink` directly into each
`VideoWidget._live_surface` child window — no `appsink → MediaFrame
→ QImage` hop on the live path. Replay still goes through
`SegmentDecoder → display_frame → QLabel`;
`VideoWidget.set_video_surface_visible(enabled, live=...)` flips the
`QStackedLayout` between `_live_surface` (LIVE) and `_frame_label`
(REPLAY/PAUSED). `display_frame()` itself auto-flips to QLabel in
native mode for the same reason.

`[app] force_python_push_preview = true` is the escape hatch — falls
back to the qimage path even for NATIVE sources, useful if d3d11
misbehaves on local hardware. Cost: preview stays Python-bound
(~720p ceiling).

### 11.3 Long recording is opt-in

Sessions are created on app launch, but recording on disk only starts
when the operator presses "Start game recording"
(`ApplicationCoordinator.toggle_long_session_recording`). Until then,
recording branches drain into the no-op record-side appsink (so the
tee never back-pressures) and no segment files are created.

### 11.4 Status bar replay surface (Phase 7.B)

When `replay_available` is True, the status bar reads
`Replay covers M:SS – M:SS (latest finalized −Ns)`. When False, it
reads `Replay not yet available — first segment finalizing` (during
the first segment's lifetime) or `Replay unavailable: start game
recording first` (between games).

### 11.5 FROZEN badges (Phase 6)

`SegmentReplayLocation.is_freeze=True` for the three §8.6.1 clamping
branches (before-earliest / after-latest / in-gap), `False` for exact
coverage. Per-tile amber "FROZEN" badge in each `VideoWidget`'s top-right
corner. Cleared on `jump_to_live` and on the recording-stop snap-back.

### 11.6 PlaybackController still leans on the primary feed

Some replay paths still center the primary feed (`_primary_runtime`,
`_primary_feed_id`) — `on_live_sample` only updates the overlay state
for the primary, the source-name surface is single-feed, etc. The
multi-feed render loop (Phase 5.C) is correct, but a few transport
edge cases haven't been fully de-primary'd. Not load-bearing for
current behavior; on the architecture-doc "Still open" list.

---

## 12. Layer map

- **`app/config/`** — `AppSettings` dataclass + TOML loader. `[source]`
  legacy single-feed, `[[feeds]]` multi-feed. See
  `r3_app_architecture.md` §13 for the shipped TOML schema.
- **`app/core/`**
  - `application_coordinator.py` — coordinator, recovery flow,
    Phase 7.D continuation.
  - `feed_registry.py` — `FeedDefinition` / `FeedRegistry`.
  - `playback_controller.py` — per-output transport, replay clock,
    multi-feed render. Replay-state machine lives here; one per
    operator output.
  - `app_state.py` — `UiState` dataclass (every per-output controller
    holds one).
  - `application_state.py` — top-level `AppState` enum + aggregator.
  - `feed_state.py` / `recording_state.py` / `replay_state.py` /
    `session_state.py` — the four authoritative state machines.
  - `state_machine.py` — generic `StateMachine[E]` framework that
    rejects illegal transitions and emits an `invalid_transition`
    health event.
  - `session_clock.py` — monotonic-anchored session clock,
    `now_session_time_ns()` + `rebase()` (Phase 7.D).
  - `play_manager.py` — `PlayManager` (currently-open-play pointer +
    `plays` SQLite owner).
  - `disk_budget.py` — Phase 7.A startup throughput estimator.
  - `health_events.py` — `HealthEventLog` (JSONL persistence).
  - `telemetry.py` — `TelemetryHub`, `FeedMetrics`, `RateCounter`,
    `LatencySampler`, `LatencyRegistry`, `DiskSampler`.
  - `signals.py` — Qt signal carriers.
  - `models.py` — `MediaFrame`, `AudioChunk`, `Segment`, `Play`,
    `ExportArtifact`, `SessionPaths`, `FeedPaths`, `PlaybackMode`,
    `FrameOverlayInfo`, `PlaybackOverlayInfo`, `IngestTelemetry`.
- **`app/media/`**
  - `pipeline_manager.py` — the GStreamer graph owner. By far the
    most invariant-dense file; see GSTREAMER_INVARIANTS.md and §5
    above.
  - `feed_runtime.py` — bundles source + pipeline + preview output.
  - `source_interface.py` / `source_factory.py` — pluggable ingest.
  - `ndi_receiver.py` — native NDI source bin (Phase 3.A.2).
  - `test_source.py` — synthetic dev fallback (python_push only).
  - `recording_manager.py` — `RecordingState` machine owner.
  - `output_renderer.py` — `OutputRenderer`,
    `MultiFeedOutputRenderer`, routes by `frame.feed_id`.
  - `preview_output.py` — per-feed preview-side ingest sink.
  - `segment_decoder.py` — `cv2.VideoCapture` wrapper for replay.
  - `frame_overlay.py` — overlay drawing for python_push frames.
  - `gst_bus_log.py` / `gst_ingest_telemetry.py` — observability
    helpers.
- **`app/storage/`**
  - `file_manager.py` — directory tree creation.
  - `metadata_db.py` — SQLite schema + the Phase 7.F migration.
  - `session_manager.py` — `SessionState` orchestration +
    `adopt_session` (Resume).
  - `segment_index.py` — in-memory hot-path query layer.
  - `segment_replay_store.py` — replay query layer over the index.
  - `session_recovery.py` — dirty marking, segment validation,
    `find_next_*_index` helpers.
- **`app/tools/`** — post-session processor entry points
  (`post_session_processor.py`, `long_form_export.py`,
  `plays_json_export.py`).
- **`app/ui/`** — `MainWindow` (re-used for operator + program with
  flags), `controls_widget`, `multi_feed_video_panel`,
  `status_bar_widget`, `video_widget`, `diagnostics_widget`,
  `recovery_dialog`.
- **`tests/`** — fast unit tests using stdlib `unittest`. They mock or
  stub GStreamer where needed. Notable test contracts:
  - `test_replay_safety_invariants.py` — locks in §15.2 / §15.7
    "writing tail excluded" + transport methods take no FS-mutation
    actions.
  - `test_session_recovery.py` — covers `mark_dirty_sessions`,
    `validate_session_segments`, quarantine collisions, `find_next_*`.
  - `test_resume_continuation.py` — Phase 7.D continuation paths.
  - `test_session_clock.py` — including `SessionClockRebaseTests`
    (Phase 7.D `rebase()` safety contract).

---

## When to update this doc

Update when any of these shifts:

- New state machine, or new states / transitions on an existing one.
- New thread, lock, or cross-thread pattern.
- Recording or replay lifecycle changes (new step in start/stop, new
  buffer probe, new entry path into replay).
- Recovery model changes (new dirty-detection mechanism, new
  recovery action, new edge case).
- Schema migration.
- Storage layout change.

Don't update for:

- Bug fixes that don't change the contract.
- Renames of internal helpers.
- New tests that don't introduce a new contract.
- Doc-only changes elsewhere.
