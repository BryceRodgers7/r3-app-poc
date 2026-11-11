# r3-app — Documentation Index

This directory and the repo root together hold every design and
implementation reference for the project. This page is the entry
point — read it first, then follow the links you need.

---

## Goal-driven entry points

### "I want to understand the system before proposing an extension"

Read these in order:

1. [`r3_app_architecture.md`](r3_app_architecture.md) — declared
   target production architecture. Long but exhaustive. The §
   numbering used elsewhere refers to this file.
2. [`IMPLEMENTATION.md`](IMPLEMENTATION.md) — current implementation
   reference: state machines, threading model, recording / replay /
   recovery lifecycle, schema evolution, storage layout, observability
   surfaces, file-by-file index.
3. [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — current code's object
   graph as a status snapshot. Substantial overlap with
   `IMPLEMENTATION.md`; `ARCHITECTURE.md` is the older
   "design verdict" framing, `IMPLEMENTATION.md` is the deep
   reference.

After reading these three you should be able to reason about any
proposed change in terms of: what the spec calls for, what the
implementation actually does, and where the two differ.

### "I'm fixing the open NDI bugs"

Start with [`NDI_OPEN_BUGS.md`](NDI_OPEN_BUGS.md): a feed freezing at
Begin Game (cause found, fix on a branch awaiting real-app verification),
its footage going unindexed, choppy replay, exit hanging inside the NDI
library, how to reproduce them, and the test traps that fake failures.

### "I'm refactoring the GStreamer pipeline"

1. [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md) — first.
   Twelve construction rules with file:line refs. Each rule was
   learned from a concrete failure (silent color shift, pipeline
   freeze, video-only segments, etc.) — easy to re-break.
2. [`IMPLEMENTATION.md` §5 (Recording lifecycle)](IMPLEMENTATION.md#5-recording-lifecycle)
   — the start/stop/rebuild ritual.
3. [`IMPLEMENTATION.md` §4 (Threading model)](IMPLEMENTATION.md#4-threading-model)
   — which thread runs what, which lock protects what.

### "I'm extending replay or transport behavior"

1. [`IMPLEMENTATION.md` §6 (Replay model)](IMPLEMENTATION.md#6-replay-model)
   — two-clock system, clamping rule, replay-from-LIVE entry
   semantics, FSM bouncing, multi-feed render loop.
2. [`IMPLEMENTATION.md` §3.3 (ReplayState)](IMPLEMENTATION.md#33-replaystate)
   — the FSM itself + transition rules.
3. [`r3_app_architecture.md` §15 / §10.4](r3_app_architecture.md) —
   target-spec rules for replay availability.

### "Stepping across a segment seam repeats or skips frames, I'm planning for 6-8 feeds / 60 fps, or I'm changing the segment length"

1. [`REPLAY_SEAM_GLITCH.md`](REPLAY_SEAM_GLITCH.md) — why a single-step click
   across the boundary between two mkv segments repeats a frame and then
   skips 0-2 (segment times that don't match the files, a hole between
   segments, reads past the end of a file), measured evidence, how it gets
   worse with more feeds and at 60 fps, and which fixes survive scale. Section 6
   covers `segment_duration_seconds`: what 30 s instead of 4 s costs (replay lag
   behind live, crash recovery, queue memory) and gains, and how variable-length
   segments work. Read it before changing segment bookkeeping,
   `nearest_frame_location`, `SegmentDecoder`, `step_frames`, or the segment
   length.

### "I'm touching crash recovery or the recovery dialog"

1. [`IMPLEMENTATION.md` §7 (Recovery model)](IMPLEMENTATION.md#7-recovery-model)
   — dirty detection, segment validation, Phase 7.D continuation,
   edge cases.
2. [`r3_app_architecture.md` §11.4](r3_app_architecture.md) —
   target-spec rules for the recovery dialog.

### "I'm touching startup, the pre-match dialog, or camera names / order"

1. [`PREMATCH.md`](PREMATCH.md) — the pre-match dialog (match details,
   camera setup), its two config files, the startup order, and how the
   camera layout is layered over `[[feeds]]`.

### "I'm touching the Challenge button, challenge records, or the Elapsed readout"

1. [`CHALLENGES.md`](CHALLENGES.md) — the challenge details screen (team,
   category, outcome, timer), where records are stored (a JSON file, not the
   database), the `[challenge]` config, and the `Elapsed: h:mm` readout.
2. [`IMPLEMENTATION.md` Phase 14](IMPLEMENTATION.md) — the clip rows and the
   referee lockout that Challenge also drives.

### "I'm planning a schema migration"

1. [`IMPLEMENTATION.md` §8 (Schema evolution)](IMPLEMENTATION.md#8-schema-evolution)
   — Phase 7.F UNIQUE-constraint migration as the working example;
   pre-5.A NULL session-time fields and how they're handled silently.
2. [`r3_app_architecture.md` §6.3 / §14](r3_app_architecture.md) —
   the canonical entity schemas.

### "I'm investigating video quality, or considering a codec change"

1. [`VIDEO_QUALITY_AND_CODECS.md`](VIDEO_QUALITY_AND_CODECS.md) — the
   three stages where quality is lost (ingest downscale → MJPEG master
   → H.264 re-encode), and why ProRes/DNxHR are export-only (not viable
   on the live path). Read before tuning resolution/CRF or revisiting
   the recording codec.

### "I'm setting up a dev environment"

1. [`UCRT64_DEVELOPMENT.md`](UCRT64_DEVELOPMENT.md) — MSYS2 UCRT64
   GStreamer + PyGObject install.
2. [`NDI_SETUP.md`](NDI_SETUP.md) — NewTek NDI runtime + `gst-plugins-rs`.

---

## Documents at a glance

| File | What it is | Updated when… |
|---|---|---|
| [`r3_app_architecture.md`](r3_app_architecture.md) | Target production architecture spec. Authoritative for what the system *should* be. | A design decision changes the target. Phased plan progress is tracked here. |
| [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | Current implementation reference. Authoritative for what the system *is*. | A state machine, lifecycle step, lock, thread, recovery edge case, or schema changes. |
| [`PREMATCH.md`](PREMATCH.md) | The pre-match dialog, its config files, startup order, and the camera layout layering. | The dialog's fields, the config file shape, or the startup order changes. |
| [`CHALLENGES.md`](CHALLENGES.md) | The challenge details screen, its JSON record store, the `[challenge]` pick-lists, and the operator `Elapsed: h:mm` readout. | The challenge fields, the record shape, or the game-clock behavior changes. |
| [`REPLAY_SEAM_GLITCH.md`](REPLAY_SEAM_GLITCH.md) | The repeat/skip glitch when stepping across a segment seam: causes, evidence, the 6-8 feed / 60 fps outlook, and the effect of the segment length (4 s vs 30 s, variable lengths). | The seam bookkeeping, resolver, decoder or step logic changes, a multi-feed / 60 fps measurement is taken, or the segment length or cutting policy changes. |
| [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md) | GStreamer construction rules, each tied to a known failure mode. | A new GStreamer rule whose violation causes a non-obvious failure is introduced. |
| [`VIDEO_QUALITY_AND_CODECS.md`](VIDEO_QUALITY_AND_CODECS.md) | The quality-loss stages from NDI source to MP4, and ProRes/DNxHR viability (export-only). | Quality behavior, resolution/CRF defaults, or codec support changes. |
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Status snapshot of the current code's object graph. Older framing than `IMPLEMENTATION.md`; substantial overlap. | "Implemented today" / "Still open" lists need refreshing. |
| [`../CLAUDE.md`](../CLAUDE.md) | Claude-specific working instructions for this repo. | A coding-tool-specific rule changes. |
| [`UCRT64_DEVELOPMENT.md`](UCRT64_DEVELOPMENT.md) | Dev-env setup for MSYS2 UCRT64 + PyGObject. | The dev toolchain changes. |
| [`NDI_SETUP.md`](NDI_SETUP.md) | NDI runtime + plugin setup. | NDI deployment requirements change. |

---

## When to add a new doc

Add a new doc when **all** of these are true:

- The topic genuinely doesn't fit any existing doc (don't split
  `IMPLEMENTATION.md` because a section got long — split because the
  topic is orthogonal to everything else there).
- The content has a clear "read this before doing X" trigger.
- The information is dense enough that scattering it across code
  comments would lose load-bearing context.

Otherwise extend an existing doc. The bar for adding to this index is
intentionally high — every additional file is one more thing that
goes stale.




DEVELOPMENT RUN COMMANDS:


NAVIGATE TO THE DIRECTORY:
cd /c/git/r3-app


SETUP VIRTUAL ENVIRONMENT:
/ucrt64/bin/python3 -m venv --system-site-packages .venv
source .venv/bin/activate


RUN THE APP (regular, or with extra logging):
python main.py

OR

GST_DEBUG=3 python main.py 2>&1 | tee run.log


POST-SESSION MP4 PROCESSING:
python -m app.tools.post_session_processor C:/SportsReplay/sessions/session_118 --ffmpeg-path C:/msys64/ucrt64/bin/ffmpeg.exe

