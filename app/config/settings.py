"""Application configuration for the replay proof of concept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

CONFIG_FILENAME = "app_settings.toml"

_VALID_SOURCE_KINDS = {"ndi", "synthetic"}


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


@dataclass(slots=True)
class AppSettings:
    """Centralized runtime defaults for the desktop application."""

    app_name: str = "Sports Replay POC"
    window_title: str = "Sports Replay Control"
    operator_window_title: str = "Sports Replay Operator"
    program_window_title: str = "Sports Replay Program"
    base_data_dir: Path = Path(r"C:\SportsReplay")
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
