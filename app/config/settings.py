"""Application configuration for the replay proof of concept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

CONFIG_FILENAME = "app_settings.toml"

_VALID_SOURCE_KINDS = {"ndi", "synthetic"}
_VALID_HWACCEL_VALUES = {"auto", "none", "nvidia", "intel", "amd"}
# Phase 11.B follow-up: live recording matrix is MJPEG+MKV only.
# ProRes / DNxHR / MOV were trialed (Phase 11.B) but the live
# recording path can't reliably mux audio into qtmux without
# hardware-class capture infrastructure that real broadcast
# instant-replay systems use. The post-session processor exists
# for archive-format export — operators wanting ProRes-MOV
# deliverables get them via that path, transcoded from the
# MJPEG-MKV master with audio properly multiplexed.
_VALID_CODECS = {"mjpeg"}
_VALID_CONTAINERS = {"mkv"}
_VALID_CODEC_CONTAINER_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("mjpeg", "mkv"),
})

# Phase 11.C — pipeline tuning matrix.
#
# Queue-policy defaults reproduce what `pipeline_manager.py` had
# hardcoded since Phase 3.B. Preview and recording have *different*
# defaults — preview is leaky-downstream so a slow preview drops
# rather than pushes back on the source; recording is non-leaky so
# disk pressure surfaces as a health event rather than dropping
# frames. `leaky` corresponds to GstQueueLeaky (0=no leak,
# 1=upstream/oldest, 2=downstream/newest).
#
# `max_size_time_ms = None` on the recording default means "derive
# from `recording_segment_duration_seconds` at pipeline-build time"
# (the §4.4 invariant: the queue holds at most one segment).
# Operators can override with an explicit ms value.
_DEFAULT_PREVIEW_QUEUE_POLICY: dict[str, Any] = {
    "leaky": 2,
    "max_buffers": 4,
    "max_size_time_ms": 200,
}
_DEFAULT_RECORDING_QUEUE_POLICY: dict[str, Any] = {
    "leaky": 0,
    "max_buffers": 256,
    "max_size_time_ms": None,
}
_VALID_LEAKY_VALUES = {0, 1, 2}

# Per-codec encoder settings. Defaults match what the encoder factory
# applies when no `[recording.encoder_settings]` block is present.
# 11.C fixes 11.B's prores profile off-by-one (was integer 2 = standard;
# correct LT integer is 1) and switches DNxHR from a `bitrate` workaround
# to the `profile` enum surface — both flow through the profile-name
# mapping in `app/media/encoder_factory.py`.
_VALID_PRORES_PROFILES = {"proxy", "lt", "standard", "hq"}
_VALID_DNXHR_PROFILES = {"lb", "sq", "hq"}
_DEFAULT_ENCODER_SETTINGS: dict[str, dict[str, Any]] = {
    "mjpeg": {"quality": 85},
    "prores": {"profile": "lt"},
    "dnxhr": {"profile": "lb"},
}


def _validate_source_kind(kind: str, *, where: str) -> None:
    """Reject obsolete or unknown `kind` values with a clear migration hint."""
    if kind in _VALID_SOURCE_KINDS:
        return
    if kind == "auto":
        raise RuntimeError(
            f"{where}: kind='auto' is no longer supported. USB-camera ingest was "
            f"removed in Phase 2.5. Use kind='ndi' for production or kind='synthetic' "
            f"for development without NDI hardware."
        )
    raise RuntimeError(
        f"{where}: unsupported kind={kind!r}. Valid values are "
        f"{sorted(_VALID_SOURCE_KINDS)}."
    )


def _validate_hwaccel(value: str, *, where: str) -> None:
    """Reject unknown `[media] hardware_acceleration` values."""
    if value in _VALID_HWACCEL_VALUES:
        return
    raise RuntimeError(
        f"{where}: hardware_acceleration={value!r} is not supported. "
        f"Valid values are {sorted(_VALID_HWACCEL_VALUES)}."
    )


def _validate_codec(value: str, *, where: str) -> None:
    """Phase 11.B follow-up: reject anything but MJPEG.

    ProRes / DNxHR were trialed for the live recording path but
    couldn't be made to reliably mux audio in our software-encoder
    pipeline. They remain useful as *export* formats — see the
    post-session processor for archive-quality transcoding from
    the MJPEG-MKV master.
    """
    if value in _VALID_CODECS:
        return
    if value in {"prores", "dnxhr"}:
        raise RuntimeError(
            f"{where}: codec={value!r} is not supported on the live "
            f"recording path (Phase 11.B follow-up — qtmux audio-mux "
            f"timing issues couldn't be resolved without hardware "
            f"capture). Use codec='mjpeg' here; transcode to "
            f"{value} via the post-session processor for archive "
            f"deliverables."
        )
    raise RuntimeError(
        f"{where}: codec={value!r} is not supported. "
        f"Valid values are {sorted(_VALID_CODECS)}."
    )


def _validate_container(value: str, *, where: str) -> None:
    """Phase 11.B follow-up: reject anything but MKV."""
    if value in _VALID_CONTAINERS:
        return
    if value == "mov":
        raise RuntimeError(
            f"{where}: container={value!r} is not supported on the "
            f"live recording path. Use container='mkv' here; "
            f"transcode to .mov via the post-session processor."
        )
    raise RuntimeError(
        f"{where}: container={value!r} is not supported. "
        f"Valid values are {sorted(_VALID_CONTAINERS)}."
    )


def _validate_queue_policy(policy: dict[str, Any], *, where: str) -> None:
    """Phase 11.C: range-check a queue-policy sub-table.

    `leaky` must match the GstQueueLeaky enum; `max_buffers` and
    `max_size_time_ms` must be positive. Unknown keys are ignored so
    that future fields in the same sub-table don't trip older configs.
    """
    if "leaky" in policy:
        leaky = int(policy["leaky"])
        if leaky not in _VALID_LEAKY_VALUES:
            raise RuntimeError(
                f"{where}: leaky={leaky} is not in "
                f"{sorted(_VALID_LEAKY_VALUES)} (GstQueueLeaky enum)."
            )
    if "max_buffers" in policy:
        max_buffers = int(policy["max_buffers"])
        if max_buffers <= 0:
            raise RuntimeError(
                f"{where}: max_buffers={max_buffers} must be > 0."
            )
    if "max_size_time_ms" in policy:
        raw = policy["max_size_time_ms"]
        # `None` is a valid value for the recording queue: "derive
        # from segment duration". Validator only rejects bogus
        # integers; pipeline_manager handles the None branch.
        if raw is not None:
            max_time = int(raw)
            if max_time <= 0:
                raise RuntimeError(
                    f"{where}: max_size_time_ms={max_time} must be > 0."
                )


def _validate_encoder_settings(
    settings: dict[str, dict[str, Any]], *, where: str
) -> None:
    """Phase 11.C: range-check the per-codec encoder-settings sub-tables."""
    for codec, codec_settings in settings.items():
        if codec not in _VALID_CODECS:
            raise RuntimeError(
                f"{where}: encoder_settings has unknown codec={codec!r}. "
                f"Valid codecs are {sorted(_VALID_CODECS)}."
            )
        if codec == "mjpeg" and "quality" in codec_settings:
            quality = int(codec_settings["quality"])
            if not (1 <= quality <= 100):
                raise RuntimeError(
                    f"{where}: mjpeg.quality={quality} is out of range "
                    f"[1, 100]."
                )
        if codec == "prores" and "profile" in codec_settings:
            profile = str(codec_settings["profile"]).strip().lower()
            if profile not in _VALID_PRORES_PROFILES:
                raise RuntimeError(
                    f"{where}: prores.profile={profile!r} is not in "
                    f"{sorted(_VALID_PRORES_PROFILES)}."
                )
        if codec == "dnxhr" and "profile" in codec_settings:
            profile = str(codec_settings["profile"]).strip().lower()
            if profile not in _VALID_DNXHR_PROFILES:
                raise RuntimeError(
                    f"{where}: dnxhr.profile={profile!r} is not in "
                    f"{sorted(_VALID_DNXHR_PROFILES)}."
                )


def _validate_replay(config: dict[str, Any], *, where: str) -> None:
    """Phase 12.B: range-check the `[replay]` sub-table.

    `frame_step_count` is the number of frames the operator's Step
    buttons jog the replay clock per click. Must be a positive int;
    rejecting 0 / negative / non-int up-front so the misconfiguration
    surfaces at config-load instead of at PlaybackController.step_frames
    runtime.
    """
    if "frame_step_count" in config:
        raw = config["frame_step_count"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise RuntimeError(
                f"{where}: frame_step_count={raw!r} must be an integer."
            )
        if raw < 1:
            raise RuntimeError(
                f"{where}: frame_step_count={raw} must be >= 1."
            )


def _validate_codec_container(codec: str, container: str, *, where: str) -> None:
    """Phase 11.B: reject incompatible codec/container combinations.

    Some pairings (mjpeg+mov in particular) aren't standard and
    splitmuxsink would fail at element-construction time. Catch them
    at config-load with a clear matrix so the operator sees the
    accepted set, not a downstream GStreamer error.
    """
    if (codec, container) in _VALID_CODEC_CONTAINER_PAIRS:
        return
    accepted = ", ".join(
        f"{c}+{x}" for c, x in sorted(_VALID_CODEC_CONTAINER_PAIRS)
    )
    raise RuntimeError(
        f"{where}: codec={codec!r} + container={container!r} is not a "
        f"supported combination. Accepted pairs: {accepted}."
    )


@dataclass(slots=True)
class AppSettings:
    """Centralized runtime defaults for the desktop application."""

    app_name: str = "Sports Replay POC"
    window_title: str = "Sports Replay Control"
    operator_window_title: str = "Sports Replay Operator"
    program_window_title: str = "Sports Replay Program"
    base_data_dir: Path = Path(r"C:\SportsReplay")
    # §13 / slice 3.C runtime mode. "development" silences the
    # python_push transitional warning in the diagnostics widget;
    # "production" surfaces it loudly so refactors don't accidentally
    # ship a Python-bound preview path.
    app_mode: str = "development"
    # Slice 3.A.3 escape hatch. When True, the preview path uses the
    # python_push appsink → QImage chain even for NATIVE-mode sources,
    # bypassing the d3d11videosink path. Use this when d3d11 binding
    # misbehaves on the local hardware (third window appearing, tee
    # fan-out freezes); the cost is keeping preview Python-bound (the
    # 720p@30 ceiling stays).
    force_python_push_preview: bool = False
    touch_button_height: int = 72
    default_feed_id: str = "feed_main"
    default_source_name: str = "Test Source"
    default_source_kind: str = "synthetic"
    ndi_source_name: str | None = None
    # Defaults chosen to keep the Python frame-callback path manageable.
    # 1280x720x3 = ~2.7 MB/frame; at 30 fps that's ~81 MB/s through the
    # GIL — comfortable for modern hardware. End-to-end 1080p requires
    # bypassing Python on both the preview path (slice 3.A.3, native
    # video sink) and the recording path (Phase 4, segmented native
    # muxers). Until both land, raising these defaults will freeze the
    # operator UI under load.
    target_frame_width: int = 1280
    target_frame_height: int = 720
    target_fps: float = 30.0
    enable_embedded_audio: bool = True
    live_audio_monitor_enabled: bool = True
    audio_sample_rate: int = 48_000
    audio_channels: int = 2
    audio_bitrate: int = 128_000
    # [media] block — Phase 11.A. Picks the recording-branch encoder
    # (`app/media/encoder_factory.py`). "auto" probes for the best
    # available hwaccel encoder and falls back to software when none is
    # found; "none" pins the pipeline to software regardless of what's
    # installed. Vendor values (`nvidia`, `intel`, `amd`) bias the
    # probe toward that vendor's elements but still fall back to
    # software when unavailable. See architecture §7.4.
    media_hardware_acceleration: str = "auto"
    # Phase 11.C: per-branch queue policy. Defaults match what was
    # hardcoded in `pipeline_manager.py` since Phase 3.B. Preview is
    # leaky-downstream (slow preview drops the oldest); recording is
    # non-leaky (disk pressure surfaces as a health event). The
    # recording queue's `max_size_time_ms = None` means "derive from
    # `recording_segment_duration_seconds`" at pipeline-build time.
    media_preview_queue_policy: dict[str, Any] = field(
        default_factory=lambda: dict(_DEFAULT_PREVIEW_QUEUE_POLICY)
    )
    media_recording_queue_policy: dict[str, Any] = field(
        default_factory=lambda: dict(_DEFAULT_RECORDING_QUEUE_POLICY)
    )
    # [recording] block — Phase 4. Native segmented recording via
    # `splitmuxsink`. MJPEG-in-MKV is the 4.A target codec/container; both
    # are intra-frame seekable per §15.7 and use elements that ship in
    # `gst-plugins-good`. ProRes/DNxHR (§5.2 first-choice) are deferred
    # because their encoders live in `gst-plugins-bad` and aren't always
    # present on UCRT64. The codec/container fields are read but currently
    # only `mjpeg` + `mkv` is implemented; other values raise on startup.
    recording_enabled: bool = True
    recording_segment_duration_seconds: float = 4.0
    recording_codec: str = "mjpeg"
    recording_container: str = "mkv"
    # Phase 11.C: per-codec encoder property overrides. Keys are codec
    # names (`mjpeg` / `prores` / `dnxhr`); values are sub-dicts of
    # property-name → value. Defaults match what `encoder_factory`
    # applies when no override is configured.
    recording_encoder_settings: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            codec: dict(props)
            for codec, props in _DEFAULT_ENCODER_SETTINGS.items()
        }
    )
    # Slice 4.F: when True, the audio tee feeds opusenc into splitmuxsink
    # alongside the video branch, so segment files contain audio. When
    # False (or when the source has no audio), the audio_record branch
    # drains via a no-op appsink and segments are video-only. Sources
    # without an audio stream (e.g. NDI Tools Screen Capture) MUST set
    # this to False — splitmuxsink stalls waiting for audio buffers
    # that never arrive otherwise. Production NDI cameras that produce
    # audio can leave this on.
    recording_audio_enabled: bool = True
    # Phase 7.A: aggregate disk write budget in MB/s. Used by
    # `app.core.disk_budget` at startup to flag a rig whose configured
    # feed count × resolution × fps would saturate the disk. 200 MB/s
    # is a conservative SATA-SSD threshold; raise for NVMe, lower for
    # spinning rust or shared volumes. Per-deployment override under
    # `[recording] disk_budget_mb_s = ...`.
    disk_budget_mb_s: float = 200.0
    # Phase 10.C: pre-flight grace window. At Start, the coordinator
    # estimates `feeds × per-feed-MB/s × disk_full_grace_seconds` worth
    # of bytes and refuses to begin recording if the recording volume
    # has less than that free. 60s is enough headroom that a short
    # backup running in parallel won't tip the rig into ENOSPC.
    disk_full_grace_seconds: float = 60.0
    # Phase 12.B: number of frames the operator's Step ◀ / Step ▶
    # buttons jog the replay clock per click. The PlaybackController
    # multiplies this by `frame_period_ns` (derived from `target_fps`
    # in the coordinator builder) to compute the session-time delta.
    # Default of 1 matches "one frame per click"; sports with faster
    # motion can turn this up via [replay] frame_step_count = N.
    replay_frame_step_count: int = 1
    # Rows from optional [[feeds]] in TOML; empty means use legacy [source] only.
    feeds_table_rows: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, config_path: Path | None = None) -> "AppSettings":
        """Load settings overrides from an optional TOML file."""
        settings = cls()
        resolved_path = Path(config_path) if config_path is not None else Path.cwd() / CONFIG_FILENAME
        if not resolved_path.exists():
            return settings

        with resolved_path.open("rb") as config_file:
            data = tomllib.load(config_file)

        app_config = cls._as_dict(data.get("app"))
        source_config = cls._as_dict(data.get("source"))

        if "app_name" in app_config:
            settings.app_name = str(app_config["app_name"])
        if "window_title" in app_config:
            settings.window_title = str(app_config["window_title"])
        if "operator_window_title" in app_config:
            settings.operator_window_title = str(app_config["operator_window_title"])
        if "program_window_title" in app_config:
            settings.program_window_title = str(app_config["program_window_title"])
        if "base_data_dir" in app_config:
            settings.base_data_dir = Path(str(app_config["base_data_dir"]))
        if "app_mode" in app_config:
            mode = str(app_config["app_mode"]).strip().lower() or "development"
            if mode not in {"development", "production"}:
                raise RuntimeError(
                    f"[app] app_mode={mode!r} is not supported; "
                    f"valid values are 'development' or 'production'."
                )
            settings.app_mode = mode
        if "force_python_push_preview" in app_config:
            settings.force_python_push_preview = bool(
                app_config["force_python_push_preview"]
            )
        if "touch_button_height" in app_config:
            settings.touch_button_height = int(app_config["touch_button_height"])
        if "target_frame_width" in app_config:
            settings.target_frame_width = int(app_config["target_frame_width"])
        if "target_frame_height" in app_config:
            settings.target_frame_height = int(app_config["target_frame_height"])
        if "target_fps" in app_config:
            settings.target_fps = float(app_config["target_fps"])
        if "enable_embedded_audio" in app_config:
            settings.enable_embedded_audio = bool(app_config["enable_embedded_audio"])
        if "live_audio_monitor_enabled" in app_config:
            settings.live_audio_monitor_enabled = bool(app_config["live_audio_monitor_enabled"])
        if "audio_sample_rate" in app_config:
            settings.audio_sample_rate = int(app_config["audio_sample_rate"])
        if "audio_channels" in app_config:
            settings.audio_channels = int(app_config["audio_channels"])
        if "audio_bitrate" in app_config:
            settings.audio_bitrate = int(app_config["audio_bitrate"])

        media_config = cls._as_dict(data.get("media"))
        if "hardware_acceleration" in media_config:
            raw_hwaccel = (
                str(media_config["hardware_acceleration"]).strip().lower()
                or "auto"
            )
            _validate_hwaccel(raw_hwaccel, where="[media]")
            settings.media_hardware_acceleration = raw_hwaccel
        # Phase 11.C: queue-policy sub-tables. Validate, then merge
        # over the per-queue defaults so a partial config (only
        # `max_buffers = 16`, say) keeps the other keys at their
        # default. Preview and recording have different defaults
        # (different leaky/max-buffers semantics).
        queue_policy_config = cls._as_dict(media_config.get("queue_policy"))
        preview_policy = cls._as_dict(queue_policy_config.get("preview"))
        if preview_policy:
            _validate_queue_policy(
                preview_policy, where="[media.queue_policy.preview]"
            )
            settings.media_preview_queue_policy = {
                **_DEFAULT_PREVIEW_QUEUE_POLICY,
                **{k: v for k, v in preview_policy.items()
                   if k in _DEFAULT_PREVIEW_QUEUE_POLICY},
            }
        recording_policy = cls._as_dict(queue_policy_config.get("recording"))
        if recording_policy:
            _validate_queue_policy(
                recording_policy, where="[media.queue_policy.recording]"
            )
            settings.media_recording_queue_policy = {
                **_DEFAULT_RECORDING_QUEUE_POLICY,
                **{k: v for k, v in recording_policy.items()
                   if k in _DEFAULT_RECORDING_QUEUE_POLICY},
            }

        recording_config = cls._as_dict(data.get("recording"))
        if "enabled" in recording_config:
            settings.recording_enabled = bool(recording_config["enabled"])
        if "segment_duration_seconds" in recording_config:
            settings.recording_segment_duration_seconds = float(
                recording_config["segment_duration_seconds"]
            )
        if "codec" in recording_config:
            settings.recording_codec = str(recording_config["codec"]).strip().lower()
        if "container" in recording_config:
            settings.recording_container = (
                str(recording_config["container"]).strip().lower()
            )
        # Phase 11.B: validate codec, container, and the pair. Run
        # unconditionally so defaults round-trip too — if a future
        # default ends up incompatible the test suite catches it.
        _validate_codec(settings.recording_codec, where="[recording]")
        _validate_container(settings.recording_container, where="[recording]")
        _validate_codec_container(
            settings.recording_codec,
            settings.recording_container,
            where="[recording]",
        )
        # Phase 11.C: per-codec encoder settings. Each sub-table
        # merges over the per-codec defaults so a partial override
        # (e.g. just `mjpeg.quality = 60`) keeps the other codec
        # entries at their defaults.
        encoder_settings_config = cls._as_dict(
            recording_config.get("encoder_settings")
        )
        if encoder_settings_config:
            parsed: dict[str, dict[str, Any]] = {}
            for codec, sub in encoder_settings_config.items():
                parsed[codec.lower()] = cls._as_dict(sub)
            _validate_encoder_settings(
                parsed, where="[recording.encoder_settings]"
            )
            merged: dict[str, dict[str, Any]] = {
                codec: dict(props)
                for codec, props in _DEFAULT_ENCODER_SETTINGS.items()
            }
            for codec, codec_settings in parsed.items():
                merged.setdefault(codec, {})
                merged[codec].update(codec_settings)
            settings.recording_encoder_settings = merged
        if "audio_enabled" in recording_config:
            settings.recording_audio_enabled = bool(recording_config["audio_enabled"])
        if "disk_budget_mb_s" in recording_config:
            settings.disk_budget_mb_s = float(recording_config["disk_budget_mb_s"])
        if "disk_full_grace_seconds" in recording_config:
            settings.disk_full_grace_seconds = float(
                recording_config["disk_full_grace_seconds"]
            )

        # Phase 12.B: [replay] block — frame-step button cadence.
        replay_config = cls._as_dict(data.get("replay"))
        if replay_config:
            _validate_replay(replay_config, where="[replay]")
            if "frame_step_count" in replay_config:
                settings.replay_frame_step_count = int(
                    replay_config["frame_step_count"]
                )

        if "feed_id" in source_config:
            settings.default_feed_id = str(source_config["feed_id"])
        if "display_name" in source_config:
            settings.default_source_name = str(source_config["display_name"])
        if "kind" in source_config:
            raw_kind = str(source_config["kind"]).strip().lower()
            settings.default_source_kind = raw_kind or "synthetic"
            _validate_source_kind(raw_kind, where="[source]")
        if "ndi_name" in source_config:
            ndi_name = str(source_config["ndi_name"]).strip()
            settings.ndi_source_name = ndi_name or None

        settings.feeds_table_rows = cls._parse_feeds_table(data.get("feeds"))

        return settings

    @staticmethod
    def _parse_feeds_table(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            feed_id = str(row.get("feed_id", "")).strip()
            if not feed_id:
                continue
            display_name = str(row.get("display_name", feed_id or "Feed")).strip()
            kind = str(row.get("kind", "synthetic")).strip().lower() or "synthetic"
            _validate_source_kind(kind, where=f"[[feeds]] feed_id={feed_id!r}")
            ndi_raw = row.get("ndi_name")
            ndi_name = str(ndi_raw).strip() if ndi_raw is not None else None
            if ndi_name == "":
                ndi_name = None
            enabled = bool(row.get("enabled", True))
            out.append(
                {
                    "feed_id": feed_id,
                    "display_name": display_name or feed_id,
                    "source_kind": kind,
                    "ndi_name": ndi_name,
                    "enabled": enabled,
                }
            )
        return out

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {}

    @property
    def sessions_root(self) -> Path:
        """Return the root folder that stores all replay sessions."""
        return self.base_data_dir / "sessions"

    @property
    def metadata_db_path(self) -> Path:
        """Return the SQLite file used for lightweight session metadata."""
        return self.base_data_dir / "metadata.db"
