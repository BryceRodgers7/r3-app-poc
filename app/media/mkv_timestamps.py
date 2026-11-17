"""Frame timestamps of a Matroska segment file, read from its block headers.

`SegmentDecoder` needs to know which timestamp the k-th frame of a file
has. OpenCV positions a seek by `frame / declared fps`, which is only the
k-th frame when frames are evenly spaced. NDI recordings are (the ingest
`videorate` makes them so), but the synthetic source and some older
recordings are not, and a seek there lands on a neighbouring frame
(docs/REPLAY_SEAM_GLITCH.md §2.3 / §7.4 step 2).

This module walks the file's EBML structure and reads each video block's
timestamp without touching the frame data, so it costs a few
milliseconds per file. It understands what `matroskamux` writes (Segment,
Info/TimestampScale, Tracks, Clusters of SimpleBlocks or BlockGroups) and
returns None for anything it can't follow, in which case the decoder
falls back to its frame-rate arithmetic.

The same walk also records where each frame's bytes sit in the file
(`read_video_frame_index`). The recordings are MJPEG, so every frame is a
standalone JPEG: a replay can read just the bytes of the frame it wants and
decode them, instead of making OpenCV seek and decode its way there
(docs/REPLAY_RANDOM_ACCESS_DESIGN.md).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import BinaryIO, NamedTuple

LOGGER = logging.getLogger(__name__)

_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CLUSTER = 0x1F43B675
_CLUSTER_TIMESTAMP = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
# Elements that may sit inside a Cluster; anything else ends an
# unknown-size Cluster.
_CLUSTER_CHILDREN = {_CLUSTER_TIMESTAMP, _SIMPLE_BLOCK, _BLOCK_GROUP, 0xA7, 0xAB, 0x5854, 0xAF}
_TRACK_TYPE_VIDEO = 1
_DEFAULT_TIMESTAMP_SCALE_NS = 1_000_000
_MAX_ELEMENTS = 200_000
_UNKNOWN = -1


class _Malformed(Exception):
    pass


class _Block(NamedTuple):
    """One (Simple)Block: where it is, and the frame bytes inside it."""

    track: int
    timestamp: int  # in TimestampScale units
    data_offset: int  # file offset of the frame's first byte
    data_size: int
    laced: bool  # several frames packed in one block, not a single frame


class _VideoBlock(NamedTuple):
    timestamp_ns: int
    data_offset: int
    data_size: int
    laced: bool


@dataclass(frozen=True, slots=True)
class MkvFrameIndex:
    """Every video frame of a segment: its timestamp and where its bytes are.

    `timestamps_ms[k]` (ms from the first frame), `offsets[k]` and `sizes[k]`
    describe frame k, in file order. `laced` is True when any block packs
    several frames, in which case `offsets` / `sizes` don't describe single
    frames and must not be used to read them.
    """

    timestamps_ms: list[float]
    offsets: list[int]
    sizes: list[int]
    laced: bool


def read_video_timestamps_ms(path: str | Path) -> list[float] | None:
    """Timestamps (ms, relative to the first frame) of the video frames, in file order.

    Returns None when the file isn't Matroska, can't be read, has no
    video frames, or its timestamps aren't increasing (the decoder then
    keeps its frame-rate arithmetic).
    """
    index = read_video_frame_index(path)
    return None if index is None else index.timestamps_ms


def read_video_frame_index(path: str | Path) -> MkvFrameIndex | None:
    """Timestamps and byte positions of the video frames, in file order.

    Returns None under the same conditions as `read_video_timestamps_ms`.
    The frame data itself is never read.
    """
    try:
        with open(path, "rb") as fh:
            blocks = _read(fh)
    except (OSError, _Malformed, ValueError, IndexError):
        return None
    except Exception:
        LOGGER.debug("mkv timestamp read failed for %s", path, exc_info=True)
        return None
    if not blocks or any(b.timestamp_ns <= a.timestamp_ns for a, b in zip(blocks, blocks[1:])):
        return None
    first = blocks[0].timestamp_ns
    return MkvFrameIndex(
        timestamps_ms=[(block.timestamp_ns - first) / 1_000_000.0 for block in blocks],
        offsets=[block.data_offset for block in blocks],
        sizes=[block.data_size for block in blocks],
        laced=any(block.laced for block in blocks),
    )


def _read(fh: BinaryIO) -> list[_VideoBlock]:
    fh.seek(0, 2)
    file_end = fh.tell()
    fh.seek(0)
    element_id, size = _read_header(fh)
    if element_id != _EBML:
        raise _Malformed("not an EBML file")
    _skip(fh, size)
    element_id, size = _read_header(fh)
    if element_id != _SEGMENT:
        raise _Malformed("no Segment")
    segment_end = file_end if size == _UNKNOWN else min(file_end, fh.tell() + size)

    scale_ns = _DEFAULT_TIMESTAMP_SCALE_NS
    video_track: int | None = None
    pending: list[_Block] = []
    count = 0
    while fh.tell() < segment_end:
        count += 1
        if count > _MAX_ELEMENTS:
            raise _Malformed("too many elements")
        start = fh.tell()
        element_id, size = _read_header(fh)
        if element_id == _INFO:
            scale_ns = _read_info(fh, fh.tell() + size) or scale_ns
        elif element_id == _TRACKS:
            video_track = _read_tracks(fh, fh.tell() + size)
        elif element_id == _CLUSTER:
            end = segment_end if size == _UNKNOWN else fh.tell() + size
            _read_cluster(fh, end, pending, size == _UNKNOWN)
        elif size == _UNKNOWN:
            raise _Malformed(f"unknown-size element {element_id:#x}")
        else:
            fh.seek(start)
            _read_header(fh)
            _skip(fh, size)
    if video_track is None:
        raise _Malformed("no video track")
    return [
        _VideoBlock(block.timestamp * scale_ns, block.data_offset, block.data_size, block.laced)
        for block in pending
        if block.track == video_track
    ]


def _read_info(fh: BinaryIO, end: int) -> int | None:
    scale = None
    while fh.tell() < end:
        element_id, size = _read_header(fh)
        if element_id == _TIMESTAMP_SCALE:
            scale = _read_uint(fh, size)
        else:
            _skip(fh, size)
    return scale


def _read_tracks(fh: BinaryIO, end: int) -> int | None:
    video = None
    while fh.tell() < end:
        element_id, size = _read_header(fh)
        if element_id != _TRACK_ENTRY:
            _skip(fh, size)
            continue
        entry_end = fh.tell() + size
        number = track_type = None
        while fh.tell() < entry_end:
            child_id, child_size = _read_header(fh)
            if child_id == _TRACK_NUMBER:
                number = _read_uint(fh, child_size)
            elif child_id == _TRACK_TYPE:
                track_type = _read_uint(fh, child_size)
            else:
                _skip(fh, child_size)
        if track_type == _TRACK_TYPE_VIDEO and video is None:
            video = number
    return video


def _read_cluster(fh: BinaryIO, end: int, out: list[_Block], unknown_size: bool) -> None:
    cluster_ts = 0
    while fh.tell() < end:
        start = fh.tell()
        element_id, size = _read_header(fh)
        if unknown_size and element_id not in _CLUSTER_CHILDREN:
            fh.seek(start)  # the next top-level element ends this cluster
            return
        if element_id == _CLUSTER_TIMESTAMP:
            cluster_ts = _read_uint(fh, size)
        elif element_id == _SIMPLE_BLOCK:
            out.append(_read_block_header(fh, size, cluster_ts))
        elif element_id == _BLOCK_GROUP:
            group_end = fh.tell() + size
            while fh.tell() < group_end:
                child_id, child_size = _read_header(fh)
                if child_id == _BLOCK:
                    out.append(_read_block_header(fh, child_size, cluster_ts))
                else:
                    _skip(fh, child_size)
        else:
            _skip(fh, size)


def _read_block_header(fh: BinaryIO, size: int, cluster_ts: int) -> _Block:
    """Track, timestamp and frame-byte position of a (Simple)Block; skips its frame data.

    Layout: track number (variable-length integer), 2-byte signed timestamp
    offset from the cluster, 1 flags byte (bits 0x06 = lacing), then the
    frame bytes.
    """
    start = fh.tell()
    track, _ = _read_vint(fh, keep_marker=False)
    raw = fh.read(3)
    if len(raw) != 3:
        raise _Malformed("truncated block")
    relative = int.from_bytes(raw[:2], "big", signed=True)
    laced = bool(raw[2] & 0x06)
    data_offset = fh.tell()
    fh.seek(start + size)
    return _Block(track, cluster_ts + relative, data_offset, start + size - data_offset, laced)


def _read_header(fh: BinaryIO) -> tuple[int, int]:
    element_id, _ = _read_vint(fh, keep_marker=True)
    size, all_ones = _read_vint(fh, keep_marker=False)
    return element_id, (_UNKNOWN if all_ones else size)


def _read_vint(fh: BinaryIO, *, keep_marker: bool) -> tuple[int, bool]:
    first = fh.read(1)
    if not first:
        raise _Malformed("unexpected end of file")
    b = first[0]
    length = 1
    mask = 0x80
    while length <= 8 and not (b & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise _Malformed("bad variable-length integer")
    rest = fh.read(length - 1)
    if len(rest) != length - 1:
        raise _Malformed("truncated variable-length integer")
    value = b if keep_marker else (b & (mask - 1))
    for byte in rest:
        value = (value << 8) | byte
    data_bits = 7 * length
    all_ones = not keep_marker and value == (1 << data_bits) - 1
    return value, all_ones


def _read_uint(fh: BinaryIO, size: int) -> int:
    if size < 0 or size > 8:
        raise _Malformed("bad integer size")
    raw = fh.read(size)
    if len(raw) != size:
        raise _Malformed("truncated integer")
    return int.from_bytes(raw, "big") if raw else 0


def _skip(fh: BinaryIO, size: int) -> None:
    if size < 0:
        raise _Malformed("cannot skip an unknown-size element")
    fh.seek(size, 1)
