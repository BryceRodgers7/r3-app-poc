"""Application configuration for the replay proof of concept."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppSettings:
    """Centralized runtime defaults for the desktop application."""

    app_name: str = "Sports Replay POC"
    window_title: str = "Sports Replay Control"
    base_data_dir: Path = Path(r"C:\SportsReplay")
    replay_buffer_seconds: int = 120
    touch_button_height: int = 72
    default_source_name: str = "Test Source"
    test_camera_index: int = 0
    target_frame_width: int = 640
    target_frame_height: int = 360
    target_fps: float = 15.0
    replay_buffer_jpeg_quality: int = 80
    recording_filename: str = "session_recording.mp4"
    recording_manifest_filename: str = "recording_manifest.json"

    @property
    def sessions_root(self) -> Path:
        """Return the root folder that stores all replay sessions."""
        return self.base_data_dir / "sessions"

    @property
    def metadata_db_path(self) -> Path:
        """Return the SQLite file used for lightweight session metadata."""
        return self.base_data_dir / "metadata.db"
