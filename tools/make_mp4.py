#!/usr/bin/env python3
"""Stand-alone: turn recorded MKV segments into one MP4 per game per camera.

For each session it reads  <root>/sessions/<session>/recording/game_NNN/<feed_id>/segment_*.mkv,
concatenates the segments of each (game, camera) and encodes them to
<session>/processed/<date>_<scheduled time>_game<N>_<camera short_name>.mp4
(date, time and camera names come from the session's session.json).
When every game and camera has an MP4, session.json is marked finalized.

Uses only the standard library plus an ffmpeg binary. Source MKVs are
never modified. Re-running skips MP4s that already exist.

    python tools/make_mp4.py session_038
    python tools/make_mp4.py --all --root D:/SportsReplay
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger("make_mp4")

DEFAULT_ROOT = Path("C:/SportsReplay")
# A session still in another state (recording, stopped, created, archived)
# is skipped unless --force: it may be live, or empty, or already shipped.
PROCESSABLE_STATES = {"finalized", "dirty"}

# No -s / -r: resolution and frame rate stay as they are in the input.
ENCODE_ARGS = [
    "-map", "0:v:0", "-map", "0:a:0?",
    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
    "-maxrate", "5000k", "-bufsize", "10000k",
    "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
    # The MJPEG source is full-range; without this the H.264 would carry the
    # full-range flag (ffprobe shows yuvj420p) and some players look washed out.
    "-vf", "scale=out_range=tv", "-color_range", "tv",
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "+faststart",
]


def read_manifest(session_dir: Path) -> dict | None:
    try:
        return json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_manifest(session_dir: Path, manifest: dict) -> None:
    path = session_dir / "session.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(path)


def filename_prefix(session_dir: Path, manifest: dict) -> str:
    """`YYYY-MM-DD_HHMM` from the match block, else from the session's creation time."""
    match = manifest.get("match") or {}
    date, start = match.get("date"), match.get("scheduled_start_time")
    if date and start:
        return f"{date}_{str(start).replace(':', '')}"
    LOGGER.warning("%s: no match date/time in session.json; using the session's creation time", session_dir.name)
    try:
        created = datetime.fromisoformat(manifest["created_at"]).astimezone()
    except (KeyError, ValueError):
        created = datetime.fromtimestamp(session_dir.stat().st_mtime)
    return created.strftime("%Y-%m-%d_%H%M")


def safe_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip().replace(" ", "_")


def segment_files(camera_dir: Path) -> list[Path]:
    def number(path: Path) -> int:
        digits = re.findall(r"\d+", path.stem)
        return int(digits[-1]) if digits else 0

    files = sorted(camera_dir.glob("*.mkv"), key=number)
    # Stopping a recording leaves the next, never-written segment behind as a
    # 0-byte file. It holds no video, so skip it (a damaged non-empty file is
    # still refused later).
    empty = [p for p in files if p.stat().st_size == 0]
    for p in empty:
        LOGGER.warning("%s/%s: skipping empty file", camera_dir.name, p.name)
    return [p for p in files if p not in empty]


def plan_jobs(session_dir: Path, manifest: dict) -> list[tuple[Path, list[Path]]]:
    """One `(output_path, segments)` per game directory and camera directory."""
    names = {c["feed_id"]: c.get("short_name") for c in manifest.get("cameras") or [] if "feed_id" in c}
    prefix = filename_prefix(session_dir, manifest)
    out_dir = session_dir / "processed"
    jobs: list[tuple[Path, list[Path]]] = []
    used: set[Path] = set()
    for game_dir in sorted((session_dir / "recording").glob("game_*")):
        game_no = int(game_dir.name.split("_", 1)[1]) if game_dir.name.split("_", 1)[1].isdigit() else game_dir.name
        for camera_dir in sorted(p for p in game_dir.iterdir() if p.is_dir()):
            segments = segment_files(camera_dir)
            if not segments:
                continue
            camera = safe_name(names.get(camera_dir.name) or camera_dir.name) or camera_dir.name
            out = out_dir / f"{prefix}_game{game_no}_{camera}.mp4"
            if out in used:  # two cameras with the same short name
                out = out_dir / f"{prefix}_game{game_no}_{camera}_{camera_dir.name}.mp4"
            used.add(out)
            jobs.append((out, segments))
    return jobs


def concat_line(path: Path) -> str:
    escaped = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'\n"


def probe_duration(ffprobe: str, path: Path) -> float | None:
    """Duration in seconds, or None when the file cannot be read."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def encode(ffmpeg: str, ffprobe: str, segments: list[Path], out: Path) -> bool:
    # ffmpeg's concat demuxer silently skips a segment it cannot open, which
    # would cut that stretch out of the game. Refuse instead.
    durations = [probe_duration(ffprobe, p) for p in segments]
    unreadable = [p.name for p, d in zip(segments, durations) if d is None]
    if unreadable:
        LOGGER.error("%s: unreadable segment(s) %s; not encoding", out.name, ", ".join(unreadable))
        return False
    expected = sum(durations)

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_suffix(".partial.mp4")  # a faststart MP4 is only valid once ffmpeg finishes
    with tempfile.TemporaryDirectory() as tmp:
        list_file = Path(tmp) / "segments.txt"
        list_file.write_text("".join(concat_line(p) for p in segments), encoding="utf-8")
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            *ENCODE_ARGS, str(partial),
        ]
        # Below-normal priority so a running app is not starved.
        flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0
        result = subprocess.run(command, capture_output=True, text=True, creationflags=flags)
    if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        LOGGER.error("ffmpeg failed for %s (exit %s): %s", out.name, result.returncode, result.stderr.strip()[-500:])
        partial.unlink(missing_ok=True)
        return False
    actual = probe_duration(ffprobe, partial)
    if actual is None or abs(actual - expected) > max(1.0, expected * 0.005):
        LOGGER.error("%s: output is %s s but the segments total %.1f s; discarding", out.name, actual, expected)
        partial.unlink(missing_ok=True)
        return False
    partial.replace(out)
    return True


def process_session(ffmpeg: str, ffprobe: str, session_dir: Path, force: bool) -> bool:
    """Encode every game/camera of one session; finalize it when all succeed."""
    manifest = read_manifest(session_dir)
    if manifest is None:
        LOGGER.error("%s: no readable session.json", session_dir.name)
        return False
    state = manifest.get("state")
    if state not in PROCESSABLE_STATES and not force:
        LOGGER.warning("%s: state is %r, skipping (use --force to override)", session_dir.name, state)
        return False
    jobs = plan_jobs(session_dir, manifest)
    if not jobs:
        LOGGER.warning("%s: no .mkv segments found", session_dir.name)
        return False

    ok = True
    for out, segments in jobs:
        if out.exists():
            LOGGER.info("%s: already done, skipping", out.name)
            continue
        LOGGER.info("%s: encoding %d segments", out.name, len(segments))
        ok = encode(ffmpeg, ffprobe, segments, out) and ok
    if not ok:
        LOGGER.error("%s: not all outputs succeeded; session.json left unchanged", session_dir.name)
        return False

    manifest["state"] = "finalized"
    manifest["finalized_at"] = manifest.get("finalized_at") or datetime.now(timezone.utc).isoformat()
    write_manifest(session_dir, manifest)
    LOGGER.info("%s: %d MP4(s) in %s; session finalized", session_dir.name, len(jobs), session_dir / "processed")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="*", help="session folder names, e.g. session_038")
    parser.add_argument("--all", action="store_true", help="process every session under the root")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"data folder (default {DEFAULT_ROOT})")
    parser.add_argument("--ffmpeg", default=None, help="path to ffmpeg (default: found on PATH)")
    parser.add_argument("--force", action="store_true", help="also process sessions in other states")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        LOGGER.error("ffmpeg not found; install it or pass --ffmpeg")
        return 2
    ffprobe = shutil.which("ffprobe", path=str(Path(ffmpeg).parent)) or shutil.which("ffprobe")
    if not ffprobe:
        LOGGER.error("ffprobe not found next to ffmpeg or on PATH")
        return 2
    sessions_root = args.root / "sessions"
    if args.all:
        names = sorted(p.name for p in sessions_root.glob("session_*") if p.is_dir())
    else:
        names = args.sessions
    if not names:
        parser.error("name at least one session, or use --all")

    results = [process_session(ffmpeg, ffprobe, sessions_root / name, args.force) for name in names]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
