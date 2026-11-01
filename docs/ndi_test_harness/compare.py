"""Same transitions, same camera, either code tree. R3_REPO picks the tree.

Uses only attributes both trees have, so the numbers are like for like.
"""
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
sys.path.insert(0, REPO)
watchdog = threading.Timer(300, lambda: (print("WATCHDOG", flush=True), os._exit(3)))
watchdog.daemon = True
watchdog.start()

import numpy as np
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as app_main

LEFT = Qt.MouseButton.LeftButton
os.makedirs("shots", exist_ok=True)
results, events = [], []
clicks = []


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


def timed_click(name, btn):
    """How long the click blocks, then how long until the loop is free again."""
    t0 = time.perf_counter()
    QTest.mouseClick(btn, LEFT)
    blocked = (time.perf_counter() - t0) * 1000
    freed = []
    QTimer.singleShot(0, lambda: freed.append(time.perf_counter()))
    while not freed:
        QApplication.processEvents()
    wait(300)
    clicks.append((name, blocked))


def run():
    ref, op = windows
    dx = op.geometry().x() - op.frameGeometry().x()
    op.move(-dx, 0)
    ref.move(1280 - dx, 0)
    for window in (op, ref):
        user32.SetWindowPos(int(window.winId()), wintypes.HWND(-1), 0, 0, 0, 0, 0x13)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 75:
        snaps = coordinator.telemetry_hub.snapshot()
        if snaps and all(s.source_fps > 5 for s in snaps):
            break
        wait(200)
    else:
        raise RuntimeError("camera never delivered frames")
    wait(2500)
    o, r = op.operator_controls, ref.referee_controls
    QTest.mouseClick(o.long_recording_button, LEFT)
    wait(13000)
    for i in range(3):
        QTest.mouseClick(o.next_play_button, LEFT)
        wait(6000)
        with watch(f"enter replay {i}", ref):
            QTest.mouseClick(o.challenge_button, LEFT)
        wait(800)
        for name, btn in (
            ("1/2x", r.half_speed_button),
            ("1/4x", r.quarter_speed_button),
            ("pause", r.pause_button),
            ("step", r.step_forward_button),
        ):
            timed_click(name, btn)
        ref.raise_()
        with watch(f"maximize replay {i}", ref, moved=True):
            ref.showMaximized()
            wait(150)
        with watch(f"restore replay {i}", ref, moved=True):
            ref.showNormal()
            wait(150)
        with watch(f"leave replay {i}", ref):
            QTest.mouseClick(o.next_play_button, LEFT)
        wait(1500)
    for i in range(3):
        op.raise_()
        with watch(f"maximize live {i}", op, moved=True):
            op.showMaximized()
            wait(150)
        with watch(f"restore live {i}", op, moved=True):
            op.showNormal()
            wait(150)

    def accept():
        QApplication.activeModalWidget().button(QMessageBox.StandardButton.Yes).click()

    QTimer.singleShot(500, accept)
    t0 = time.perf_counter()
    QTest.mouseClick(o.long_recording_button, LEFT)
    print(f"end game click {(time.perf_counter() - t0) * 1000:.0f}ms (500 is the modal)", flush=True)
    wait(800)


def wrapper():
    try:
        run()
    except Exception:
        traceback.print_exc()
    finally:
        sampler.done.set()
        print(f"tree: {REPO}", flush=True)
        for name, n, hz, base_c, peak_c, base_w, peak_w, bad in events:
            print(f"{name:22s} n={n:3d} dark {base_c:.3f}->{peak_c:.3f} bad={bad}", flush=True)
        for name in ("1/2x", "1/4x", "pause", "step"):
            values = [v for k, v in clicks if k == name]
            if values:
                print(f"click {name:6s} median {statistics.median(values):.0f}ms max {max(values):.0f}ms", flush=True)
        for s in coordinator.telemetry_hub.snapshot():
            print(f"{s.feed_id} src={s.source_fps:.1f} prv={s.preview_fps:.1f} rec={s.recording_fps:.1f} drop={s.dropped_per_sec:.1f}", flush=True)
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
