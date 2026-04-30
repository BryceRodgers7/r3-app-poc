# GStreamer Pipeline Invariants

This document captures GStreamer-specific construction rules that the
pipeline depends on but that aren't obvious from reading the high-level
architecture. They are easy to violate during a refactor; each one was
learned from a concrete failure mode that took meaningful debugging time.

This is not aspirational — every rule is enforced by the current code in
`app/media/pipeline_manager.py`. Cross-references below use `file:line`
format so a future contributor can read the original context.

---

## 1. Color-space pinning before `jpegenc`

**Rule.** A `capsfilter` enforcing `format=I420,colorimetry=bt601` MUST sit
between the source-side videoconvert and `jpegenc` on the recording branch.

**Why.** JPEG has no standardized colorimetry tag, so every player decodes
JPEG content assuming BT.601. NDI / production camera sources deliver
BT.709. Without this capsfilter, `videoconvert` would forward BT.709
straight into `jpegenc`, the file would be tagged BT.709 (or nothing),
and downstream players would apply the BT.601 YUV→RGB matrix to BT.709
content — colors visibly shift (skin tones turn ruddy, greens go olive).
Pinning caps here forces `videoconvert` to do the 709→601 conversion
*before* the encode, so the encoded JPEG content actually matches what
decoders expect.

**Code:** `pipeline_manager.py:1120-1133`.

---

## 2. `async=False` on every appsink and on the audio sink

**Rule.** Every `appsink` and the live audio sink (`wasapisink`) must be
configured with `async=False`.

**Why.** When a sink is `async=True` (the GStreamer default), it gates
the parent pipeline's `PAUSED → PLAYING` transition on receiving its
preroll buffer. With NDI sources that have no audio (NDI Tools Screen
Capture, muted mic, audio-disabled camera), the audio sink would never
receive a buffer, never preroll, and the entire pipeline would stay
stuck in `PAUSED` forever — preview freezes, recording never starts.
Setting `async=False` makes those sinks non-gating: the pipeline
transitions to `PLAYING` whether or not the sink ever sees a buffer.

The same rule applies to the preview / record / audio-record appsinks
even on sources that *do* produce data — there's no scenario where you
want a downstream consumer to gate the upstream live pipeline.

**Code:** `pipeline_manager.py:913-915` (preview appsink),
`1639-1644` (live audio sink), `1879` (record-side appsink drains).

---

## 3. Splitmuxsink `audio_%u` request order

**Rule.** When wiring audio into `splitmuxsink`, request the `audio_%u`
sink pad on splitmuxsink **before** linking the encoder's src pad to it.
Reversing the order silently produces video-only segments.

**Why.** `splitmuxsink` instantiates an internal `matroskamux` and
forwards request pads through to it. `matroskamux` decides on its
caps configuration (video-only vs muxed) at the moment the first sink
pad is requested. If the encoder's src pad is linked first, the muxer
sees only the video pad and commits to video-only. The subsequent
`audio_%u` request appears to succeed but never produces audio output.

Requesting the `audio_%u` pad first lets the muxer see "video + audio"
during caps negotiation; the encoder.src → audio_pad link then succeeds
and audio actually muxes in.

**Code:** `pipeline_manager.py:1672-1685` (initial wiring),
`1842-1865` (re-link helper, used on splitmuxsink rebuild).

---

## 4. Audio probe on encoder's permanent src pad, not splitmuxsink request pad

**Rule.** The Phase 7.E audio-presence probe goes on `opusenc.src_pad`,
NOT on the `splitmuxsink.audio_%u` request pad it links to.

**Why.** Splitmuxsink request pads get *reassigned* every time
splitmuxsink is rebuilt (Phase 7.G's Stop/Start cycle). A probe
installed on a request pad would be silently dropped on the first
Stop/Start. The encoder's static src pad is permanent for the lifetime
of the pipeline.

**Code:** `pipeline_manager.py:1715-1726`.

---

## 5. Defensive unlink before re-linking on splitmuxsink rebuild

**Rule.** Before linking `opusenc.src` to a freshly-built splitmuxsink's
`audio_%u` pad on rebuild, check whether `opusenc.src` is still
peer-linked to the OLD splitmuxsink and `unlink(peer)` first.

**Why.** When `_rebuild_splitmuxsink_locked` swaps the splitmuxsink
element, the encoder's src pad is still peer-linked to the old element
(which has been removed from the pipeline but not destroyed). Calling
`encoder_src_pad.link(new_audio_sink_pad)` without unlinking first
returns `Gst.PadLinkReturn.WAS_LINKED`, leaves the encoder pointing at
the dead old sink, and silently produces audio-less recordings.

**Code:** `pipeline_manager.py:1856-1865`.

---

## 6. Buffer probes must do no pixel-data work

**Rule.** Buffer pad probes installed on hot-path elements
(`videoconvert.src` for native preview, `convert.src` for record,
`jpegenc.src` for segment metadata, audio tee sink for presence
detection) must read only `buffer.pts` / `buffer.offset`, increment
counters, or set sticky flags. They must not map the buffer, copy
pixel data, or call into Python code that does either.

**Why.** Probes run on the GStreamer streaming thread, which holds the
GIL while the probe runs. A probe that maps and processes pixel data
runs *every frame* and serializes the entire pipeline behind the GIL —
preview latency balloons and the source tee back-pressures. The
existing probes are intentionally minimal: they tick metrics, capture
timestamps, or set "I have seen at least one of these" booleans.
Anything heavier belongs on an appsink branch with its own queue, not
in a probe.

**Code:** `pipeline_manager.py:1055-1089` (native preview probe —
explicit "no pixel-data work" comment), `1367-1399` (jpegenc probe —
PTS + counter only), `1485-1496` (record-branch probe — counter +
audio-missing grace check).

---

## 7. `format-location` before active recording session: route to `unrouted_segments_dir()`

**Rule.** `splitmuxsink`'s `format-location` callback must always return
a writable path, even when `enable_file_recording` has not been called
yet. The current code routes those throwaway requests into
`tempfile.gettempdir() / "r3-app-unrouted-segments"` and purges the
folder on next launch.

**Why.** `format-location` can fire during pipeline startup (caps
propagation), muxer reset, state cycling, or splitmuxsink rebuild —
all paths that may run before the operator ever clicks "Start game
recording." If the callback returns `None`, splitmuxsink raises and
takes the pipeline down. If it returns a path inside the active
session's `recording/<game_NNN>/<feed_id>/`, those throwaway segments
get tracked as real segments and confuse replay. Routing to a known
temp folder lets us purge them deterministically.

**Code:** `pipeline_manager.py:46-68` (helpers), `1310-1319` (callback).

---

## 8. `do-timestamp=False` on `appsrc` + manual PTS computation

**Rule.** The python_push appsrc (synthetic source's video and audio)
must be configured with `do-timestamp=False`. Buffer PTS is computed
manually as `int((frame.timestamp - stream_start_timestamp) * Gst.SECOND)`
where `stream_start_timestamp` is the first frame's timestamp captured
on the first push.

**Why.** `do-timestamp=True` makes appsrc stamp buffers with the
pipeline's running clock at *push time*, not the source's actual
capture time. That introduces a wall-clock-dependent jitter and breaks
the session-time math (Phase 5.A), which assumes monotonically
increasing PTS that maps cleanly to session time via a per-segment
offset. Manual PTS, anchored on the first frame's timestamp so the
first segment's PTS starts at 0, gives the segment finalizer a clean
domain to capture `pts_to_session_offset_ns`.

**Code:** `pipeline_manager.py:706-717` (video appsrc),
`1592-1602` (audio appsrc), `2031-2036` (video PTS computation),
`2066-2071` (audio PTS computation).

---

## When to add a new invariant here

If you introduce a GStreamer rule whose violation causes a non-obvious
runtime failure (silent data loss, pipeline freeze, color shift, caps
negotiation collapse, etc.), document it here. The bar is "a future
contributor doing a reasonable refactor would otherwise re-break this."
Things that are obvious from reading the surrounding code (queue
linkage, `sync_state_with_parent` after add) don't need a doc entry.
