"""How often does a feed stall when recording starts? R3_REPO picks the tree."""
import ctypes
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

REPO = os.environ["R3_REPO"]
CYCLES = int(os.environ.get("CYCLES", "9"))
sys.path.insert(0, REPO)
watchdog = threading.Timer(280, lambda: (print("WATCHDOG", flush=True), os._exit(3)))
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


def run():
    ref, op = windows
    o = op.operator_controls
    if not healthy(60):
        raise RuntimeError("feeds never reached 25fps")
    wait(1500)
    starts = stalled_total = 0
    for i in range(CYCLES):
        if not healthy(25):
            print(f"cycle {i}: feeds not healthy before start", flush=True)
        QTest.mouseClick(o.long_recording_button, LEFT)
        # A stalled feed's 5s average is near zero by now, before the
        # health check rebuilds it.
        wait(7000)
        snaps = coordinator.telemetry_hub.snapshot()
        stalled = [s.feed_id for s in snaps if s.source_fps < 10]
        starts += len(snaps)
        stalled_total += len(stalled)
        print(
            f"cycle {i}: src={[round(s.source_fps) for s in snaps]} "
            f"rec={[round(s.recording_fps) for s in snaps]} stalled={stalled}",
            flush=True,
        )

        # Give a rebuilt feed time to write segments of its own.
        stop_here = bool(stalled and os.environ.get("STOP_ON_STALL"))
        if stop_here:
            print(f"stalled {stalled}; letting the rebuild record", flush=True)
            wait(18000)

        def accept():
            modal = QApplication.activeModalWidget()
            modal.button(QMessageBox.StandardButton.Yes).click()

        QTimer.singleShot(400, accept)
        QTest.mouseClick(o.long_recording_button, LEFT)
        wait(1500)
        if stop_here:
            break
    print(f"RESULT tree={REPO} feed-starts={starts} stalled={stalled_total}", flush=True)


def wrapper():
    try:
        run()
    except Exception:
        traceback.print_exc()
    finally:
        QApplication.quit()


qt_app, coordinator, windows = app_main.build_application()
for window in windows:
    window.show()
QTimer.singleShot(0, wrapper)
code = qt_app.exec()
print("exit", code, flush=True)
watchdog.cancel()
# Skips DLL teardown, where the NDI library can hang forever at exit.
ctypes.WinDLL("kernel32").TerminateProcess(wintypes.HANDLE(-1), code)
