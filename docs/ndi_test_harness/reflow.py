"""Hide/show flicker on four live NDI tiles, either tree. R3_REPO picks it."""
import ctypes
import os
import statistics
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from ctypes import wintypes

REPO = os.environ["R3_REPO"]
ROUNDS = 6
sys.path.insert(0, REPO)
watchdog = threading.Timer(150, lambda: (print("WATCHDOG", flush=True), os._exit(3)))
watchdog.daemon = True
watchdog.start()

import numpy as np
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import main as app_main

LEFT = Qt.MouseButton.LeftButton
os.makedirs("shots", exist_ok=True)
results, events = [], []


def check(name, ok, detail=""):
    results.append((bool(ok), name, str(detail)))


def wait(ms):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()


here = os.path.dirname(os.path.abspath(__file__))
source = open(os.path.join(here, "drive_ndi.py"), encoding="utf-8").read()
exec(source[source.index("# --- Screen capture") : source.index("# --- Input")], globals())


def run():
    ref, op = windows
    dx = op.geometry().x() - op.frameGeometry().x()
    op.move(-dx, 0)
    ref.move(1280 - dx, 0)
    for window in (op, ref):
        user32.SetWindowPos(int(window.winId()), wintypes.HWND(-1), 0, 0, 0, 0, 0x13)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 60:
        snaps = coordinator.telemetry_hub.snapshot()
        if snaps and all(s.source_fps > 5 for s in snaps):
            break
        wait(200)
    else:
        raise RuntimeError("camera never delivered frames")
    wait(2500)
    buttons = op.camera_ribbon._buttons_by_feed
    for i in range(ROUNDS):
        for feed_id, verb in (
            ("ndi_4", "hide"), ("ndi_3", "hide"), ("ndi_3", "show"), ("ndi_4", "show"),
        ):
            with watch(f"{verb} {feed_id} #{i}", op):
                QTest.mouseClick(buttons[feed_id], LEFT)


def wrapper():
    try:
        run()
    except Exception:
        traceback.print_exc()
    finally:
        sampler.done.set()
        for action in ("hide ndi_4", "hide ndi_3", "show ndi_3", "show ndi_4"):
            rows = [e for e in events if e[0].startswith(action)]
            hits = sum(1 for e in rows if e[7] > 0)
            worst = max((e[7] for e in rows), default=0)
            print(f"{action}: {hits}/{len(rows)} transitions flickered, worst {worst} samples", flush=True)
        total = sum(1 for e in events if e[7] > 0)
        print(f"RESULT tree={REPO} {total}/{len(events)} transitions flickered", flush=True)
        QApplication.quit()


qt_app, coordinator, windows = app_main.build_application()
screen_size = QGuiApplication.primaryScreen().size()
grabber = Capture(screen_size.width(), screen_size.height())
sampler = Sampler(screen_size.width(), screen_size.height())
sampler.start()
for window in windows:
    window.show()
QTimer.singleShot(0, wrapper)
code = qt_app.exec()
print("exit", code, flush=True)
watchdog.cancel()
sys.exit(code)
