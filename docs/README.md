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

### "I'm refactoring the GStreamer pipeline"

1. [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md) — first.
   Eight construction rules with file:line refs. Each rule was
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

### "I'm touching crash recovery or the recovery dialog"

1. [`IMPLEMENTATION.md` §7 (Recovery model)](IMPLEMENTATION.md#7-recovery-model)
   — dirty detection, segment validation, Phase 7.D continuation,
   edge cases.
2. [`r3_app_architecture.md` §11.4](r3_app_architecture.md) —
   target-spec rules for the recovery dialog.

### "I'm planning a schema migration"

1. [`IMPLEMENTATION.md` §8 (Schema evolution)](IMPLEMENTATION.md#8-schema-evolution)
   — Phase 7.F UNIQUE-constraint migration as the working example;
   pre-5.A NULL session-time fields and how they're handled silently.
2. [`r3_app_architecture.md` §6.3 / §14](r3_app_architecture.md) —
   the canonical entity schemas.

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
| [`GSTREAMER_INVARIANTS.md`](GSTREAMER_INVARIANTS.md) | GStreamer construction rules, each tied to a known failure mode. | A new GStreamer rule whose violation causes a non-obvious failure is introduced. |
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
