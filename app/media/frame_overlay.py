"""Helpers for formatting and rendering video metadata overlays."""

from __future__ import annotations

from datetime import datetime

import cv2

from app.core.models import FrameArray, FrameOverlayInfo, PlaybackOverlayInfo

_FONT = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE = 0.72
_FONT_THICKNESS = 2
_LINE_SPACING = 12
_PADDING_X = 14
_PADDING_Y = 12
_MARGIN = 18
_BACKGROUND_COLOR = (24, 24, 24)
_TEXT_COLOR = (240, 240, 240)
_BORDER_COLOR = (70, 70, 70)


def format_timestamp(timestamp: float | None) -> str:
    """Return a human-readable local timestamp with millisecond precision."""
    if timestamp is None:
        return "--"
    dt_value = datetime.fromtimestamp(timestamp)
    return f"{dt_value:%Y-%m-%d %H:%M:%S}.{dt_value.microsecond // 1000:03d}"


def build_frame_overlay_lines(frame_overlay: FrameOverlayInfo) -> list[str]:
    """Build immutable frame-metadata lines for an overlay block."""
    lines: list[str] = []
    if frame_overlay.source_name:
        lines.append(f"Source: {frame_overlay.source_name}")
    if frame_overlay.frame_id is not None:
        lines.append(f"Frame: {frame_overlay.frame_id:09d}")
    if frame_overlay.capture_timestamp is not None:
        lines.append(f"Capture: {format_timestamp(frame_overlay.capture_timestamp)}")
    return lines


def build_playback_overlay_lines(playback_overlay: PlaybackOverlayInfo) -> list[str]:
    """Build operator-view playback lines for the transient UI overlay."""
    lines = [playback_overlay.mode.value]
    if playback_overlay.status_text:
        lines.append(playback_overlay.status_text)
    if playback_overlay.playback_timestamp is not None:
        lines.append(f"Viewed: {format_timestamp(playback_overlay.playback_timestamp)}")
    if playback_overlay.wall_clock_timestamp is not None:
        lines.append(f"Now: {format_timestamp(playback_overlay.wall_clock_timestamp)}")
    if playback_overlay.mode.value in {"PAUSED", "REPLAY"}:
        lines.append(f"Behind live: {playback_overlay.seconds_behind_live:0.1f}s")
        lines.append(f"Rate: {playback_overlay.playback_rate:0.2f}x")
    return lines


def render_frame_overlay(image_bgr: FrameArray, frame_overlay: FrameOverlayInfo) -> FrameArray:
    """Return a copy of the frame with immutable metadata burned into the pixels."""
    lines = build_frame_overlay_lines(frame_overlay)
    if not lines:
        return image_bgr.copy()

    annotated = image_bgr.copy()
    _draw_text_block(
        image=annotated,
        lines=lines,
        origin_x=_MARGIN,
        origin_y=_MARGIN,
    )
    return annotated


def _draw_text_block(image: FrameArray, lines: list[str], origin_x: int, origin_y: int) -> None:
    if not lines:
        return

    line_sizes = [
        cv2.getTextSize(line, _FONT, _FONT_SCALE, _FONT_THICKNESS)[0]
        for line in lines
    ]
    max_width = max(width for width, _ in line_sizes)
    line_height = max(height for _, height in line_sizes)
    block_height = (
        (_PADDING_Y * 2)
        + (line_height * len(lines))
        + (_LINE_SPACING * max(len(lines) - 1, 0))
    )
    block_width = (_PADDING_X * 2) + max_width

    top_left = (origin_x, origin_y)
    bottom_right = (origin_x + block_width, origin_y + block_height)
    cv2.rectangle(image, top_left, bottom_right, _BACKGROUND_COLOR, thickness=-1)
    cv2.rectangle(image, top_left, bottom_right, _BORDER_COLOR, thickness=1)

    baseline_y = origin_y + _PADDING_Y + line_height
    for line in lines:
        cv2.putText(
            image,
            line,
            (origin_x + _PADDING_X, baseline_y),
            _FONT,
            _FONT_SCALE,
            _TEXT_COLOR,
            _FONT_THICKNESS,
            cv2.LINE_AA,
        )
        baseline_y += line_height + _LINE_SPACING
