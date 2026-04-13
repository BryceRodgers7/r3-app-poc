"""Application configuration for the replay proof of concept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

CONFIG_FILENAME = "app_settings.toml"


@dataclass(slots=True)
class AppSettings:
    """Centralized runtime defaults for the desktop application."""

    app_name: str = "Sports Replay POC"
    window_title: str = "Sports Replay Control"
    operator_window_title: str = "Sports Replay Operator"
    program_window_title: str = "Sports Replay Program"
    base_data_dir: Path = Path(r"C:\SportsReplay")
    replay_buffer_seconds: int = 120
    touch_button_height: int = 72
    default_feed_id: str = "feed_main"
    default_source_name: str = "Test Source"
    default_source_kind: str = "auto"
    test_camera_index: int = 0
    ndi_source_name: str | None = None
    target_frame_width: int = 640
    target_frame_height: int = 360
    target_fps: float = 15.0
    replay_buffer_jpeg_quality: int = 80
    recording_filename: str = "session_recording.mp4"
    recording_manifest_filename: str = "recording_manifest.json"
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
        if "replay_buffer_seconds" in app_config:
            settings.replay_buffer_seconds = int(app_config["replay_buffer_seconds"])
        if "touch_button_height" in app_config:
            settings.touch_button_height = int(app_config["touch_button_height"])
        if "target_frame_width" in app_config:
            settings.target_frame_width = int(app_config["target_frame_width"])
        if "target_frame_height" in app_config:
            settings.target_frame_height = int(app_config["target_frame_height"])
        if "target_fps" in app_config:
            settings.target_fps = float(app_config["target_fps"])
        if "replay_buffer_jpeg_quality" in app_config:
            settings.replay_buffer_jpeg_quality = int(app_config["replay_buffer_jpeg_quality"])
        if "recording_filename" in app_config:
            settings.recording_filename = str(app_config["recording_filename"])
        if "recording_manifest_filename" in app_config:
            settings.recording_manifest_filename = str(app_config["recording_manifest_filename"])

        if "feed_id" in source_config:
            settings.default_feed_id = str(source_config["feed_id"])
        if "display_name" in source_config:
            settings.default_source_name = str(source_config["display_name"])
        if "kind" in source_config:
            settings.default_source_kind = str(source_config["kind"]).strip().lower() or "auto"
        if "camera_index" in source_config:
            settings.test_camera_index = int(source_config["camera_index"])
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
            kind = str(row.get("kind", "auto")).strip().lower() or "auto"
            camera_index = int(row.get("camera_index", 0))
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
                    "camera_index": camera_index,
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
