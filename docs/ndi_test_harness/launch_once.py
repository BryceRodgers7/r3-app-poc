"""One fresh app launch -> Begin Game once (a game 1) -> did a feed stall?

`cycle.py` cannot reproduce the Begin Game freeze: it repeats Begin/End Game in
ONE session, so only its first cycle is a game 1 and the rest take the benign
game-2+ path. This launches the app, waits for the feeds, waits EXTRA seconds,
clicks Begin Game once, and checks 3.5 s later. If a feed has stalled it takes
two native stack dumps of this process (`stack.py`, 1.5 s apart) while the feed
is frozen: that is how the circular wait in NDI_OPEN_BUGS.md bug 1 was found.

    python launch_once.py <trial-number> <extra-seconds>

Environment: R3_REPO (tree to run), STACK_DIR (default ./stacks).
Prints one `RESULT trial=...` line. Run it through `run_launches.sh`, which wipes
the data folder between launches; a launch that is cut short leaves a dirty
session, and the next one would stop on the resume dialog.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

REPO = os.environ["R3_REPO"]
TRIAL, EXTRA = sys.argv[1], float(sys.argv[2])
STACK_DIR = Path(os.environ.get("STACK_DIR", "stacks"))
STACK_PY = Path(__file__).with_name("stack.py")
sys.path.insert(0, REPO)


def terminate(code=0):
    """TerminateProcess on ourselves: it skips NDI's DLL teardown, which can hang forever.

    The handle must be typed; a plain int from GetCurrentProcess() can fail silently.
    """
    sys.stdout.flush()
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess(wintypes.HANDLE(-1), code)


watchdog = threading.Timer(150, lambda: (print(f"WATCHDOG trial={TRIAL}", flush=True), terminate(3)))
watchdog.daemon = True
watchdog.start()

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as app_main

LEFT = Qt.MouseButton.LeftButton


def wait(ms):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()


def healthy(limit_s):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit_s:
        snaps = coordinator.telemetry_hub.snapshot()
        if snaps and all(s.source_fps >= 25 for s in snaps):
            return True
        wait(200)
    return False


def dump_stacks(tag):
    STACK_DIR.mkdir(parents=True, exist_ok=True)
    with open(STACK_DIR / f"t{TRIAL}_{tag}.txt", "w", encoding="utf-8") as out:
        subprocess.run(
            [sys.executable, "-I", str(STACK_PY), str(os.getpid())],
            stdout=out, stderr=subprocess.STDOUT, timeout=60,
        )


def run():
    _referee, operator = windows
    controls = operator.operator_controls
    launched = time.perf_counter()
    if not healthy(60):
        print(f"RESULT trial={TRIAL} never-healthy", flush=True)
        return
    healthy_after = time.perf_counter() - launched
    wait(int(EXTRA * 1000))
    QTest.mouseClick(controls.long_recording_button, LEFT)
    wait(3500)
    early = [s.feed_id for s in coordinator.telemetry_hub.snapshot() if s.source_fps < 40]
    if early:
        print(f"trial {TRIAL}: STALL at +3.5s on {early}; dumping stacks", flush=True)
        dump_stacks(f"{'_'.join(early)}_a")
        wait(1500)
        dump_stacks(f"{'_'.join(early)}_b")
        wait(2000)
    else:
        wait(3500)
    snaps = coordinator.telemetry_hub.snapshot()
    stalled = [s.feed_id for s in snaps if s.source_fps < 10]
    print(
        f"RESULT trial={TRIAL} tree={REPO} healthy_after={healthy_after:.1f}s extra={EXTRA:.1f}s "
        f"src={[round(s.source_fps) for s in snaps]} rec={[round(s.recording_fps) for s in snaps]} "
        f"early_stall={early} stalled_at_7s={stalled}",
        flush=True,
    )

    def accept():
        QApplication.activeModalWidget().button(QMessageBox.StandardButton.Yes).click()

    QTimer.singleShot(400, accept)
    QTest.mouseClick(controls.long_recording_button, LEFT)
    wait(1500)


def wrapper():
    try:
        run()
    except Exception:
        traceback.print_exc()
    finally:
        terminate(0)


qt_app, coordinator, windows = app_main.build_application()
for window in windows:
    window.show()
QTimer.singleShot(0, wrapper)
qt_app.exec()
terminate(0)
