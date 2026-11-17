"""CPU cost and colour of the NDI receive chain: BGR vs NV12 vs NV12 + hardware JPEG.

Windows only. Sends a picture over NDI (`ndisink`, UYVY / BT.709), receives it with a
`gst-launch` chain shaped like the app's (ingest -> tee -> two d3d11 preview sinks +
a JPEG record branch) and measures the receiver process's CPU time. Read-only: it
touches neither the app nor its files. Windows open on screen while it runs.

    python nv12_bench.py bench bgr,nv12,nv12_qsv [--seconds 20] [--size 1920x1080] [--feeds 2] [--nopreview]
    python nv12_bench.py color bgr,nv12,nv12_qsv      # real frame: JPEG colours vs the BGR chain
    python nv12_bench.py bars  bgr,nv12,nv12_qsv      # SMPTE bars: a wrong colour matrix shows here

Variants: raw (ingest only), bgr (the old chain), nv12 (the app's chain), nv12_qsv
(hardware JPEG), nv12_nomatrix (informational: skips the 709 -> 601 conversion, NOT
shippable). All chains scale first and convert second, like `NDIReceiver`.

Environment: R3_BENCH_SEGMENT = a recorded 1080p .mkv to take a real picture from
(without it the SMPTE pattern is sent); GST_LAUNCH = path to gst-launch-1.0.exe.
Needs the UCRT64 python (OpenCV + numpy) for `color` / `bars`.
Results on the development laptop are in docs/IMPLEMENTATION.md section 5.1.
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

WORK = os.path.join(tempfile.gettempdir(), "r3-nv12-bench")
GST = os.environ.get("GST_LAUNCH") or shutil.which("gst-launch-1.0") or "gst-launch-1.0"
NDI_NAME = "R3BENCH"
HOST = os.environ.get("COMPUTERNAME", "localhost")
RECEIVE_NAME = f"{HOST} ({NDI_NAME})"
W, H = 1920, 1080

PREVIEW_Q = "queue leaky=2 max-size-buffers=4 max-size-time=200000000 max-size-bytes=0"
RECORD_Q = "queue max-size-buffers=256 max-size-time=4000000000 max-size-bytes=0"
NV12_BT601 = "video/x-raw,format=NV12,colorimetry=bt601"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def cpu_seconds(proc: subprocess.Popen) -> float:
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    ok = _kernel32.GetProcessTimes(
        wintypes.HANDLE(int(proc._handle)), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)
    )
    if not ok:
        return -1.0
    return sum(((x.dwHighDateTime << 32) | x.dwLowDateTime) / 1e7 for x in (k, u))


def _gpath(path: str) -> str:
    return path.replace("\\", "/")


def make_frame() -> str | None:
    """A real frame from R3_BENCH_SEGMENT as a JPEG, or None to send the SMPTE pattern."""
    segment = os.environ.get("R3_BENCH_SEGMENT")
    if not segment:
        return None
    os.makedirs(WORK, exist_ok=True)
    frame = os.path.join(WORK, "frame.jpg")
    if not os.path.exists(frame):
        import cv2

        cap = cv2.VideoCapture(segment)
        for _ in range(40):
            ok, img = cap.read()
        if not ok:
            raise SystemExit(f"cannot read a frame from {segment}")
        cv2.imwrite(frame, img, [cv2.IMWRITE_JPEG_QUALITY, 98])
    return frame


def sender(pattern_only: bool = False) -> subprocess.Popen:
    frame = None if pattern_only else make_frame()
    if frame:
        src = (
            f'filesrc location="{_gpath(frame)}" ! jpegdec ! videoscale '
            f"! video/x-raw,width={W},height={H} ! videoconvert ! imagefreeze is-live=true "
            f"! video/x-raw,framerate=30000/1001 ! videoconvert "
        )
    else:
        src = f"videotestsrc pattern=smpte is-live=true ! video/x-raw,width={W},height={H},framerate=30000/1001 ! videoconvert "
    pipe = f'{src}! video/x-raw,format=UYVY,colorimetry=bt709 ! ndisink ndi-name="{NDI_NAME}"'
    return subprocess.Popen([GST, "-q"] + shlex.split(pipe), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


def receiver_chain(variant: str, width: int, height: int, preview: bool, tail: str | None = None) -> str:
    """The chain for `variant`. `tail` replaces the record sink (used by the colour checks)."""
    if variant == "raw":
        return f'ndisrc ndi-name="{RECEIVE_NAME}" ! ndisrcdemux name=d d.video ! fakesink sync=false'
    work_format = "BGR" if variant == "bgr" else "NV12"
    head = f'ndisrc ndi-name="{RECEIVE_NAME}" ! ndisrcdemux name=d d.video ! videoscale ! videoconvert ! videorate'
    work = f"video/x-raw,format={work_format},width={width},height={height},framerate=30000/1001"
    enc_caps = {"bgr": NV12_BT601, "nv12_nomatrix": "video/x-raw,format=NV12"}.get(variant, NV12_BT601)
    encoder = "qsvjpegenc quality=85" if variant.endswith("_qsv") else "jpegenc quality=85"
    sink = tail or "fpsdisplaysink name=rfps text-overlay=false sync=false video-sink=fakesink"
    parts = [
        f"{head} ! {work} ! tee name=t",
        f"t. ! {RECORD_Q} ! valve drop=false ! videoconvert ! {enc_caps} ! {encoder} ! {sink}",
    ]
    if preview:
        parts.insert(
            1,
            f"t. ! {PREVIEW_Q} ! valve drop=false ! videoconvert ! tee name=pt "
            f"pt. ! {PREVIEW_Q} ! videocrop ! d3d11videosink sync=false async=false force-aspect-ratio=true "
            f"pt. ! {PREVIEW_Q} ! d3d11videosink sync=false async=false force-aspect-ratio=true",
        )
    return " ".join(parts)


def cmd_bench(variants: list[str], seconds: float, width: int, height: int, preview: bool, feeds: int) -> None:
    os.makedirs(WORK, exist_ok=True)
    snd = sender()
    time.sleep(4)
    try:
        for variant in variants:
            chain = receiver_chain(variant, width, height, preview)
            procs, logs = [], []
            for i in range(feeds):
                log = os.path.join(WORK, f"bench_{variant}_{i}.log")
                logs.append(log)
                procs.append(
                    subprocess.Popen([GST, "-v"] + shlex.split(chain), stdout=open(log, "w"), stderr=subprocess.STDOUT)
                )
            time.sleep(7 + 2 * feeds)
            c0, t0 = [cpu_seconds(p) for p in procs], time.time()
            time.sleep(seconds)
            c1, t1 = [cpu_seconds(p) for p in procs], time.time()
            for proc in procs:
                stop(proc)
            cores = sum(b - a for a, b in zip(c0, c1)) / (t1 - t0)
            fps, errors = [], []
            for log in logs:
                text = open(log, errors="replace").read()
                found = re.findall(r"rfps.*?current: ([\d.]+)", text)
                fps.append(found[-1] if found else "-")
                errors += [ln[:140] for ln in text.splitlines() if "ERROR" in ln or "not-negotiated" in ln][:2]
            print(
                f"{variant:14s} feeds={feeds} {width}x{height}  cores={cores:.3f} ({cores / feeds:.3f}/feed)  "
                f"record fps={fps}  {errors}",
                flush=True,
            )
    finally:
        stop(snd)


def _decode(path: str):
    import cv2
    import numpy as np

    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def _record_jpegs(variant: str, width: int, height: int) -> list[str]:
    out = os.path.join(WORK, "color")
    os.makedirs(out, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(variant)}_\d{{5}}\.jpg$")

    def mine() -> list[str]:
        return sorted(f for f in glob.glob(os.path.join(out, "*.jpg")) if pattern.match(os.path.basename(f)))

    for old in mine():
        os.remove(old)
    location = _gpath(os.path.join(out, f"{variant}_%05d.jpg"))
    tail = f'multifilesink location="{location}" max-files=3'
    proc = subprocess.Popen(
        [GST, "-q"] + shlex.split(receiver_chain(variant, width, height, False, tail)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(8)
    stop(proc)
    return mine()


def _stats(name: str, a, b) -> None:
    import numpy as np

    d = a.astype(np.int16) - b.astype(np.int16)
    bias = d.reshape(-1, 3).mean(axis=0)
    print(
        f"  {name:24s} mean|diff|={np.abs(d).mean():5.2f}  bias(BGR)=({bias[0]:+.2f},{bias[1]:+.2f},{bias[2]:+.2f})"
        f"  p99={np.percentile(np.abs(d), 99):.0f}  max={np.abs(d).max()}"
    )


def cmd_color(variants: list[str], bars: bool) -> None:
    import cv2
    import numpy as np

    os.makedirs(WORK, exist_ok=True)
    snd = sender(pattern_only=bars)
    time.sleep(4)
    images = {}
    try:
        for variant in variants:
            files = _record_jpegs(variant, W, H)
            if not files:
                print(variant, "recorded nothing")
                continue
            with open(files[-1], "rb") as fh:
                starts = fh.read(3).hex()
            images[variant] = _decode(files[-1])
            print(f"{variant}: {len(files)} files, last {os.path.getsize(files[-1])} bytes, starts {starts}")
    finally:
        stop(snd)
    if not images:
        return
    if bars:
        centres = [(int((i + 0.5) * W / 7), int(H * 0.3)) for i in range(7)]
        names = ["white", "yellow", "cyan", "green", "magenta", "red", "blue"]
        print("bar centres, BGR mean difference between each variant and the first one")
        first = next(iter(images.values()))
        for variant, img in images.items():
            row = []
            for (x, y) in centres:
                patch = img[y - 20:y + 20, x - 20:x + 20].reshape(-1, 3).mean(0) - first[y - 20:y + 20, x - 20:x + 20].reshape(-1, 3).mean(0)
                row.append(f"{np.abs(patch).max():4.0f}")
            print(f"  {variant:14s} max|diff| per bar [{' '.join(names)}]: {' '.join(row)}")
        print("  (JPEG holds limited-range YCbCr, so white decodes near 235, not 255: same for every variant.)")
    else:
        names = list(images)
        print("difference from the first variant:")
        for other in names[1:]:
            _stats(f"{names[0]} vs {other}", images[names[0]], images[other])


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("bench", "color", "bars"):
        raise SystemExit(__doc__)
    mode, variants = sys.argv[1], sys.argv[2].split(",")
    seconds, width, height, preview, feeds = 20.0, W, H, True, 1
    args = sys.argv[3:]
    while args:
        arg = args.pop(0)
        if arg == "--seconds":
            seconds = float(args.pop(0))
        elif arg == "--size":
            width, height = (int(v) for v in args.pop(0).split("x"))
        elif arg == "--feeds":
            feeds = int(args.pop(0))
        elif arg == "--nopreview":
            preview = False
    if mode == "bench":
        cmd_bench(variants, seconds, width, height, preview, feeds)
    else:
        cmd_color(variants, bars=(mode == "bars"))


if __name__ == "__main__":
    main()
