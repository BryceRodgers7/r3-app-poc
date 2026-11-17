"""Simulate replay ticks for 2 feeds (as PlaybackController does: pool decode, wait for all).

Usage: python replay_tick_sim.py SEGMENT_FEED_A.mkv SEGMENT_FEED_B.mkv
Compares the OpenCV path with direct JPEG decode at several tick intervals.
Read-only; stays inside the first ~3.5 s of each file."""
import sys, time, statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.media.segment_decoder import SegmentDecoder
from app.storage.segment_replay_store import SegmentReplayLocation
fa, fb = sys.argv[1], sys.argv[2]
fps = 29.97
def run(direct, T_ms, label):
    decs = [SegmentDecoder("a","a", direct_decode=direct), SegmentDecoder("b","b", direct_decode=direct)]
    paths = [fa, fb]
    pool = ThreadPoolExecutor(2)
    costs = []; shown = 0
    c0 = time.process_time(); w0 = time.perf_counter()
    t = 0.0
    while t < 3500:  # stay inside one ~4 s segment
        k = int(t * fps / 1000.0)
        tick0 = time.perf_counter()
        futs = [pool.submit(d.decode, SegmentReplayLocation(segment=SimpleNamespace(file_path=p), offset_in_segment_ns=int(t*1e6), frame_index=k)) for d, p in zip(decs, paths)]
        frames = [f.result() for f in futs]
        shown += sum(1 for f in frames if f is not None)
        costs.append((time.perf_counter() - tick0) * 1000)
        t += T_ms
        sleep = (w0 + t/1000.0) - time.perf_counter()
        if sleep > 0: time.sleep(sleep)
    wall = time.perf_counter() - w0; cpu = time.process_time() - c0
    for d in decs: d.close()
    print(f"{label:<28} T={T_ms:>4} ms: ticks {len(costs):>3}, tick cost median {statistics.median(costs):6.1f} ms, max {max(costs):6.1f} ms | CPU {cpu/wall:4.2f} cores for 2 feeds, {shown/wall:4.1f} frames shown/s")
for T in (33, 67, 133, 283, 633):
    run(False, T, "OpenCV (before)")
    run(True, T, "direct JPEG (after)")
