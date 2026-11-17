"""Builds tiny Matroska files for tests, without needing GStreamer or ffmpeg.

Writes the same structure `matroskamux` does for one video track: an EBML
header, a Segment holding Info, Tracks and one Cluster of SimpleBlocks (or
BlockGroups). Each frame is stored as given, so a test can use real JPEG
bytes or placeholder bytes.
"""

from __future__ import annotations


def _vint(value: int) -> bytes:
    """EBML variable-length size: length marker + value."""
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | value).to_bytes(length, "big")
    raise ValueError("size too large")


def _uint(value: int) -> bytes:
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _element(element_id: bytes, payload: bytes) -> bytes:
    return element_id + _vint(len(payload)) + payload


def build_mkv(
    frames: list[bytes],
    *,
    timestamps_ms: list[int] | None = None,
    fps: float = 15.0,
    block_groups: bool = False,
    lacing_flag: int = 0,
    track_number: int = 1,
    audio_frames: int = 0,
) -> bytes:
    """A Matroska file with `frames` as the video track.

    `timestamps_ms` defaults to evenly spaced at `fps`. `lacing_flag` is the
    block flags byte's lacing bits (0x02/0x04/0x06) for testing laced files;
    0 means no lacing. `audio_frames` adds that many blocks on track 2
    between the video blocks, to prove they are skipped.
    """
    if timestamps_ms is None:
        timestamps_ms = [round(i * 1000.0 / fps) for i in range(len(frames))]
    ebml = _element(b"\x1a\x45\xdf\xa3", b"")
    info = _element(b"\x15\x49\xa9\x66", _element(b"\x2a\xd7\xb1", _uint(1_000_000)))
    video_entry = _element(b"\xae", _element(b"\xd7", _uint(track_number)) + _element(b"\x83", b"\x01"))
    audio_entry = _element(b"\xae", _element(b"\xd7", _uint(2)) + _element(b"\x83", b"\x02"))
    tracks = _element(b"\x16\x54\xae\x6b", video_entry + audio_entry)

    blocks = b""
    for k, (frame, ts) in enumerate(zip(frames, timestamps_ms)):
        header = _vint(track_number) + (ts).to_bytes(2, "big", signed=True) + bytes([0x80 | lacing_flag])
        if block_groups:
            blocks += _element(b"\xa0", _element(b"\xa1", header + frame))
        else:
            blocks += _element(b"\xa3", header + frame)
        if k < audio_frames:
            audio_header = _vint(2) + ts.to_bytes(2, "big", signed=True) + b"\x80"
            blocks += _element(b"\xa3", audio_header + b"\x00\x01\x02")
    cluster = _element(b"\x1f\x43\xb6\x75", _element(b"\xe7", _uint(0)) + blocks)
    segment = _element(b"\x18\x53\x80\x67", info + tracks + cluster)
    return ebml + segment
