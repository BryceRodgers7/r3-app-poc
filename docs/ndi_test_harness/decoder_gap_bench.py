"""Time SegmentDecoder.decode() against a real recorded segment as the requested
frame gap per call grows (read-only: opens a COMPLETED segment, never writes).

Usage: python decoder_gap_bench.py PATH_TO_SEGMENT.mkv
With the direct JPEG path the ms/call is flat; with direct_decode=False it jumps
once the gap passes _MAX_SKIP_FRAMES. The "path" column labels the OpenCV path."""
import os, statistics, sys, time, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cv2
from app.media.segment_decoder import SegmentDecoder, _MAX_SKIP_FRAMES

seg_path = sys.argv[1]
cap = cv2.VideoCapture(seg_path)
w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); fps = cap.get(cv2.CAP_PROP_FPS)
print(f"segment {os.path.basename(seg_path)}: {w}x{h}, {n} frames, {fps:.2f} fps, _MAX_SKIP_FRAMES={_MAX_SKIP_FRAMES}")

# 1) raw OpenCV building blocks
def t(fn, reps):
    xs = []
    for _ in range(reps):
        a = time.perf_counter(); fn(); xs.append((time.perf_counter() - a) * 1000)
    return statistics.median(xs), min(xs), max(xs)

cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
med, lo, hi = t(lambda: cap.read(), 40)
print(f"raw read() (decode+convert one frame): median {med:.1f} ms (min {lo:.1f}, max {hi:.1f})")
cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
med, lo, hi = t(lambda: cap.grab(), 40)
print(f"raw grab() (decode, no convert):       median {med:.1f} ms")
def seek_read(pos):
    def f():
        cap.set(cv2.CAP_PROP_POS_MSEC, pos * 1000.0 / fps); cap.read()
    return f
for pos in (30, 60, 90):
    med, lo, hi = t(seek_read(pos), 10)
    print(f"seek (POS_MSEC to frame {pos}) + read:  median {med:.1f} ms  (min {lo:.1f}, max {hi:.1f})")
cap.release()

# 2) the app's own SegmentDecoder: one call per tick, frame index advancing by `gap`
def loc(i):
    seg = types.SimpleNamespace(file_path=seg_path)
    return types.SimpleNamespace(segment=seg, offset_in_segment_ns=int(i * 1e9 / fps), frame_index=i, is_freeze=False)

print("\nSegmentDecoder.decode(): median ms per call when each call asks for a frame `gap` ahead of the last")
print(f"{'gap':>4} {'ms/call':>9} {'ms per frame advanced':>22}  OpenCV path at this gap")
for gap in (1, 2, 4, 6, 8, 9, 10, 12, 16, 20, 30):
    d = SegmentDecoder("bench", "bench")
    d.decode(loc(0))  # open + prime
    xs = []
    i = 0
    while i + gap < n - 1 and len(xs) < 40:
        i += gap
        a = time.perf_counter(); fr = d.decode(loc(i)); xs.append((time.perf_counter() - a) * 1000)
        assert fr is not None
    d.close()
    med = statistics.median(xs)
    print(f"{gap:>4} {med:>9.1f} {med/gap:>22.1f}  {'skip' if gap <= _MAX_SKIP_FRAMES else 'seek'}   (n={len(xs)})")
