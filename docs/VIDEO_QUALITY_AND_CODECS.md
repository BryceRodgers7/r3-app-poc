# Video Quality & Codec Viability

**Read this when:** someone reports that recorded or post-processed
video looks worse than "the original," or when considering whether to
switch the recording codec to ProRes / DNxHR.

This doc has two parts:

1. **Where quality is lost** — the three independent stages between an
   NDI source and the final MP4 deliverable, with file:line refs.
2. **Is ProRes / DNxHR viable?** — the answer is *not on the live
   recording path* (and why), *yes as an export format* (and what's
   still missing to get there). This expands on the Phase 11.B finding
   already recorded in `r3_app_architecture.md` §11.

---

## Part 1 — Where quality is lost

A tester reported that the post-processed video looked lower quality
than "the original." That report is consistent with the pipeline:
there are **three separate, compounding quality-loss stages** between
the NDI feed and the final MP4. Only the last one is "post-processing"
— the first two happen before and during recording.

```
NDI source (near-lossless, full res)
   │   ── STAGE 1: videoscale + videorate downscale ──────────────
   ▼
720p30 raw frames
   │   ── STAGE 2: MJPEG q85 intra-frame encode ──────────────────
   ▼
.mkv segments (MJPEG-in-MKV, the "master")
   │   ── STAGE 3: ffmpeg libx264 CRF 23, 4:2:0 re-encode ────────
   ▼
.mp4 deliverable (H.264)
```

### Stage 1 — ingest downscale (the dominant loss)

`app/media/ndi_receiver.py` inserts a `videoscale` + `videorate` right
at the source and pins the output caps to the configured target:

```
width={frame_width}, height={frame_height}, framerate={fps}
```

The defaults (`app/config/settings.py:300-302`) are **1280×720 @ 30fps**:

```python
target_frame_width: int = 1280
target_frame_height: int = 720
target_fps: float = 30.0
```

If the NDI source is 1080p (or 1080p60), resolution and frame rate are
discarded **at ingest, before the mkv is ever written**. This is the
single most visible quality drop, and it affects everything
downstream. It is true whether the NDI feed is a live camera or a
stream of a pre-recorded file — either way the source typically
exceeds 720p30.

The 720p30 cap is deliberate, not accidental. The comment above those
defaults (`settings.py:293-299`) explains: the synthetic/`python_push`
frame-callback path moves frames through the Python GIL, and
1080p end-to-end requires bypassing Python on **both** the preview
path (native video sink) and the recording path (native segmented
muxers). Raising the defaults before both land freezes the operator
UI under load.

> **Note:** production NDI ingest is now native (see `CLAUDE.md` —
> Phase 3.A.2/3.A.3), so the GIL argument may no longer bind for
> NDI-only deployments. Whether the NDI path can sustain 1080p is an
> empirical question — measure before raising the cap, and confirm the
> preview render path keeps up.

### Stage 2 — MJPEG master is lossy intra-frame

The recording branch (`app/media/encoder_factory.py`) writes **MJPEG
at `quality=85`** by default (`_BUILTIN_ENCODER_SETTINGS`). MJPEG q85
is a reasonable archival master but is visibly lossy on gradients and
fine detail. NDI High Bandwidth is a much higher-bitrate, near-lossless
codec, so the mkv is already a step down from the NDI feed even
ignoring resolution.

The `quality` value is operator-tunable via
`[recording.encoder_settings] mjpeg.quality` (1–100); raising it toward
95 reduces this loss at the cost of larger segments.

### Stage 3 — post-processing re-encode (generational loss)

`app/tools/long_form_export.py:81-86` re-encodes every segment into the
final MP4:

```python
_DEFAULT_VIDEO_CODEC_ARGS = (
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "23",
    "-pix_fmt", "yuv420p",
)
```

- **CRF 23** is libx264's default — a size/quality balance, *not*
  visually lossless. CRF ≈ 18 is generally considered near-transparent;
  23 is noticeably softer, especially on motion (i.e. sports).
- **`-pix_fmt yuv420p`** forces 4:2:0 chroma. (MJPEG masters are
  already 4:2:0 — see the I420 encode caps in `pipeline_manager.py
  _recording_encode_caps_string` — so for the current MJPEG path this
  is not an *additional* loss. It would matter if the master were
  4:2:2.)

So the MP4 is a lossy H.264 transcode of an already-lossy, already-
downscaled MJPEG master. **Yes — the processed MP4 is genuinely lower
quality than the mkv it is built from, and the mkv is lower quality
than the NDI source.**

### What the tester is probably comparing

- If "the original" is the **NDI feed itself** (NDI Studio Monitor, or
  the app's live preview), they are comparing full-res near-lossless
  NDI against a 720p30 → MJPEG-q85 → H.264-CRF23 deliverable. The gap
  is large and is dominated by Stage 1.
- If they are comparing the **mkv against the MP4**, they are seeing
  Stage 3 alone — subtler, but real.

### Diagnostic to settle the live-vs-prerecorded question

Run `ffprobe` on the NDI source (or read NDI Studio Monitor's stats),
then on one `.mkv` segment, then on the final `.mp4`:

```
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,codec_name \
  -of default=noprint_wrappers=1 <file>
```

If the mkv already reads 1280×720 while the NDI source reads 1920×1080,
Stage 1 is confirmed as the dominant loss and codec/CRF tuning is
secondary.

### Levers, by stage

| Stage | Lever | Cost / caveat |
|---|---|---|
| 1 — ingest downscale | Raise `target_frame_width/height/fps` | May tax preview/record paths; was capped for the Python-fed path. Measure first. |
| 2 — MJPEG master | Raise `mjpeg.quality` toward 95 | Larger segments, more disk. |
| 3 — MP4 re-encode | Lower export CRF (e.g. 18), or `-preset slower` | Bigger files, slower encode. No code surface for this yet — it's hardcoded in `long_form_export.py`. |
| 3 — alt. | Stream-copy instead of re-encode | Only if an H.264 deliverable isn't required and the player tolerates MJPEG-in-MP4; loses the "standard delivery codec" property. |

The highest-leverage fix is almost always **Stage 1** (resolution),
then **Stage 3 CRF**, then **Stage 2 quality**.

---

## Part 2 — Is ProRes / DNxHR viable?

Short answer: **No for live recording** (in this software-encoded NDI
pipeline), **yes in principle for export** — though the export path
does not yet actually emit ProRes/DNxHR (it only emits H.264 today).

This was trialed and reverted in Phase 11.B. The full reasoning lives
in `r3_app_architecture.md` §11 ("11.B — ProRes / DNxHR codec support —
partially landed; live-path codec matrix narrowed back to MJPEG-only");
this section summarizes it and corrects a common misremembering.

### The misremembering: it wasn't *primarily* hardware-vs-software

It's easy to remember the blocker as "hardware encode vs software
encode." That is *a* real constraint but not the one that forced the
revert. Two distinct issues compounded:

**(a) The immediate blocker — qtmux late audio request pads.**
ProRes/DNxHR were paired with the MOV container, which `splitmuxsink`
muxes via `qtmux`. `qtmux` refuses `audio_%u` request pads once
`STREAM_START` has flowed through its video pad. The operator clicks
Start *after* the source has been delivering buffers (and events) for
several seconds, so by then qtmux has locked its pad set. Phase 9.C's
"wire audio when the first buffer arrives" pattern works fine for
`matroskamux` (permissive about late pads) but fails on `qtmux` with
`Not providing request pad after stream start` — game 1 records
video-only, and the audio/video interleaving on the game-2 rebuild was
observed to wedge the pipeline. Everything tried to work around it
(holding splitmuxsink in READY, `set_locked_state`, deferring the sink,
a downstream-block probe) broke sticky-event/caps propagation, because
there is no clean way to selectively block `STREAM_START` from reaching
the muxer during preroll.

**(b) The structural constraint — ProRes/DNxHR are software-only on
Windows.** `encoder_factory.py` documents this directly (lines 8-16):
there is no native ProRes hwaccel encoder anywhere, no DNxHR hwaccel
encoder anywhere, and `proresenc` from `gst-plugins-bad` isn't built
into UCRT64. The only candidates are the libav-wrapped software
encoders (`avenc_prores_ks` / `avenc_prores`, `avenc_dnxhd`). So even
if (a) were solved, every feed would be software-encoding 4:2:2 (and
10-bit for ProRes) frames on the CPU, which does not scale to multiple
feeds at higher resolutions on a workstation. This is the grain of
truth behind the "hardware vs software" memory: the pro systems that
record ProRes/DNxHR live do it on **dedicated hardware capture cards
with on-board ASIC encoders**, not general-purpose software pipelines
fed by NDI.

### Why ProRes/DNxHR don't even buy us much on the live path

For operator-driven instant replay + full-length re-watch recording,
the broadcast-archive properties of ProRes/DNxHR don't carry over:

- Replay scrubbing only needs **intra-frame** coding — MJPEG already
  qualifies (every frame is a keyframe; any frame is seekable).
- **4:2:2 / 10-bit** precision is a finishing/grading property; 4:2:0
  8-bit is fine for sports replay viewing.
- "Broadcasters expect ProRes" is a **deliverable** property — it
  belongs at the export step, not the live record step.

### What still exists in the code (and what's blocked)

The Phase 11.B implementation was kept but made unreachable:

- **Wired but unreachable:** the encoder-factory ProRes/DNxHR rows
  (`avenc_prores_ks` → `avenc_prores`, `avenc_dnxhd`), the profile-name
  → integer maps (`_PRORES_PROFILE_INT`, `_DNXHR_PROFILE_INT`), and the
  per-codec encode caps (`I422_10LE` for ProRes, `Y42B` for DNxHR) in
  `encoder_factory.py` / `pipeline_manager._recording_encode_caps_string`.
  They are well-tested (`tests/test_encoder_factory.py`) and retained
  for reuse.
- **Blocked at config load:** `_validate_codec` /
  `_validate_container` in `settings.py` reject `prores` / `dnxhr` /
  `mov` with explicit messages pointing the operator at the
  post-session processor. The live matrix is `{"mjpeg"}` / `{"mkv"}`.

### The export path: intended home, but not yet implemented

`r3_app_architecture.md` calls the post-session processor "the
canonical path" to ProRes/DNxHR. **That is the intended design, not
the current behavior.** Today `app/tools/long_form_export.py`
hardcodes `libx264` / `aac` (Stage 3 above) and has **no ProRes/DNxHR
output path** — the encoder-factory rows feed the *GStreamer recording
branch*, not the *ffmpeg post-processor*, which are separate code.

To actually deliver ProRes/DNxHR archive masters, someone would need
to add codec selection to `long_form_export._build_ffmpeg_args` —
e.g. `-c:v prores_ks -profile:v 3` or `-c:v dnxhd -profile dnxhr_hq`
into a `.mov`. This is viable and low-risk because the post-processor:

- runs **offline**, per-artifact, with no live timing constraints;
- has **no qtmux-during-live problem** (ffmpeg writes the whole MOV in
  one pass, so there are no late request pads);
- can afford **software encoding** (it's a batch job, not N concurrent
  live feeds).

But note: transcoding the **720p30 MJPEG master** to ProRes does not
recover the resolution lost at Stage 1, nor the detail lost at Stage 2.
It would produce a high-bitrate, NLE-friendly 720p file — useful as a
deliverable format, but not a quality *improvement* over the master.
**Quality is bounded by the master; codec choice at export only
controls how faithfully the master is preserved.**

### Bottom line

| Use | ProRes / DNxHR viable? | Why |
|---|---|---|
| **Live recording** | **No** | qtmux refuses late audio pads in the live-Start pattern; software-only encode doesn't scale to multi-feed. Reverted in 11.B; blocked at config load. |
| **Export / archive** | **Yes, in principle** | Offline, single-pass, no live muxer timing, software encode acceptable. **But not implemented** — `long_form_export.py` only emits H.264 today. |

If the goal is to address the tester's quality complaint, switching the
**live** codec is the wrong lever (it's blocked, and MJPEG-q95 closes
most of the gap anyway). The high-leverage fixes are **ingest
resolution (Stage 1)** and **export CRF (Stage 3)**. Adding a
ProRes/DNxHR *export* option is reasonable as a deliverable-format
feature, but it is not a quality fix.

---

## See also

- `r3_app_architecture.md` §11 — full Phase 11.B writeup, including the
  list of qtmux workarounds that were tried and failed.
- `r3_app_architecture.md` §5.2 — the codec ranking, updated to frame
  ProRes/DNxHR as export-only.
- `app/media/encoder_factory.py` — the (live-path) encoder selection,
  with the software-only ProRes/DNxHR comments.
- `app/tools/long_form_export.py` — the post-session ffmpeg transcode
  (currently H.264-only).
