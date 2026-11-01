"""Real app, real NDI camera, real Win32 mouse: zoom, pan and the seek bar.

The real cursor moves, because QTest events skip the native-window routing
this has to prove. Everything about the picture is read from the screen.
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

sys.path.insert(0, "C:/git/r3-app")
watchdog = threading.Timer(300, lambda: (print("WATCHDOG", flush=True), os._exit(3)))
watchdog.daemon = True
watchdog.start()

import numpy as np
from PySide6.QtCore import QEvent, QEventLoop, QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as app_main
from app.ui.feed_view import FeedView, MAX_ZOOM

LEFT = Qt.MouseButton.LeftButton
os.makedirs("shots", exist_ok=True)
results, events = [], []


def check(name, ok, detail=""):
    results.append((bool(ok), name, str(detail)))
    suffix = f"  [{detail}]" if detail != "" else ""
    print(("PASS " if ok else "FAIL ") + name + suffix, flush=True)


def wait(ms):
    """Never QTest.qWait: it holds the GIL and stalls the pad probes."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()


here = os.path.dirname(os.path.abspath(__file__))
source = open(os.path.join(here, "drive_ndi.py"), encoding="utf-8").read()
exec(source[source.index("# --- Screen capture") : source.index("def pictures(")], globals())
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]


def to_screen(widget, x, y):
    point = widget.mapToGlobal(QPoint(int(x), int(y)))
    return point.x(), point.y()


def real_ctrl_wheel(x, y, notches):
    user32.SetCursorPos(x, y)
    wait(60)
    user32.keybd_event(0x11, 0, 0, 0)
    wait(20)
    for _ in range(abs(notches)):
        user32.mouse_event(0x0800, 0, 0, 120 if notches > 0 else -120 & 0xFFFFFFFF, 0)
        wait(25)
    user32.keybd_event(0x11, 0, 2, 0)
    wait(60)


def real_wheel(x, y):
    user32.SetCursorPos(x, y)
    wait(60)
    user32.mouse_event(0x0800, 0, 0, 120, 0)
    wait(80)


def real_drag(start, end, steps=8):
    user32.SetCursorPos(*start)
    wait(60)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    wait(30)
    for step in range(1, steps + 1):
        user32.SetCursorPos(
            start[0] + (end[0] - start[0]) * step // steps,
            start[1] + (end[1] - start[1]) * step // steps,
        )
        wait(16)
    wait(30)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    wait(120)


def tile_pixels(win, tile):
    x, y, w, h = rect_of(tile._surface_stack_host)
    if covered(win, x + w // 2, y + h // 2):
        return None
    return grabber.grab()[y : y + h, x : x + w, :3].copy()


def shows_picture(frame):
    return frame is not None and float((frame.max(axis=2) > 24).mean())


def fills_tile(frame):
    """Zoomed in past the bars, the picture reaches every edge of the tile."""
    lit = frame.max(axis=2) > 24
    return bool(lit[:6].mean() > 0.2 and lit[-6:].mean() > 0.2 and lit[:, :6].mean() > 0.2 and lit[:, -6:].mean() > 0.2)


def alive(win, tile, count=5):
    frames = []
    for _ in range(count):
        frame = tile_pixels(win, tile)
        frames.append(None if frame is None else frame[::8, ::8].tobytes())
        wait(110)
    return len(set(frames))


def centered_detail(frame):
    """A cheap fingerprint of what the tile is looking at."""
    h, w = frame.shape[:2]
    return frame[h // 4 : 3 * h // 4 : 12, w // 4 : 3 * w // 4 : 12].astype(np.int16)


def view_of(tile):
    view = tile.view()
    return f"{view.zoom:.2f}x ({view.center_x:.3f},{view.center_y:.3f})"


def shot(win, name):
    frame = grabber.grab()
    g = win.frameGeometry()
    save_bgra(frame, (g.x(), g.y(), g.width(), g.height()), f"shots/{name}.png")


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
    check("camera connected", True, f"{time.perf_counter() - t0:.1f}s")
    wait(2500)

    feeds = list(ref.video_panel.widgets_by_feed_id)
    tiles = ref.video_panel.widgets_by_feed_id
    a = tiles[feeds[0]]
    o, r, bar = op.operator_controls, ref.referee_controls, ref.review_seek_bar
    print("feeds", feeds, flush=True)

    # ---- Live zoom on the native surface ----
    check(f"{feeds[0]} is on the native path", a._render_mode == "native", a._render_mode)
    x, y, w, h = rect_of(a._surface_stack_host)
    cx, cy = to_screen(a, w // 2, h // 2)
    before = tile_pixels(ref, a)
    shot(ref, "01_live_1x")

    real_wheel(cx, cy)
    check("plain wheel does not zoom", a.view() == FeedView(), view_of(a))
    real_ctrl_wheel(cx, cy, 4)
    check("real Ctrl+wheel zooms the live feed", a.view().is_zoomed, view_of(a))
    wait(300)
    zoomed_frame = tile_pixels(ref, a)
    check("zoomed live picture is on screen", shows_picture(zoomed_frame) > 0.5, f"lit {shows_picture(zoomed_frame):.2f}")
    check("zoomed picture fills past the letterbox", fills_tile(zoomed_frame))
    change = float(np.abs(centered_detail(before) - centered_detail(zoomed_frame)).mean())
    check("picture actually changed", change > 2, f"mean diff {change:.1f}")
    check("zoomed live feed keeps updating", alive(ref, a) >= 3)
    shot(ref, "02_live_zoomed")
    title = ref.video_panel._titles[feeds[0]].text()
    check("zoom level shows in the title", "×" in title, title)

    # Operator is a separate sink and must be untouched.
    op_tile = op.video_panel.widgets_by_feed_id[feeds[0]]
    check("operator tile not zoomed", op_tile.view() == FeedView())
    op_frame = tile_pixels(op, op_tile)
    check("operator picture intact", shows_picture(op_frame) > 0.3, f"lit {shows_picture(op_frame):.2f}")
    op_cx, op_cy = to_screen(op_tile, op_tile.width() // 2, op_tile.height() // 2)
    real_ctrl_wheel(op_cx, op_cy, 2)
    check("operator window ignores Ctrl+wheel", op_tile.view() == FeedView(), view_of(op_tile))

    # ---- Live pan ----
    before_view = a.view()
    before_frame = tile_pixels(ref, a)
    real_drag((cx, cy), (cx - 200, cy - 80))
    check(
        "real drag pans the live feed",
        a.view().center_x > before_view.center_x and a.view().center_y > before_view.center_y,
        f"{before_view.center_x:.3f} -> {view_of(a)}",
    )
    wait(250)
    panned_frame = tile_pixels(ref, a)
    change = float(np.abs(centered_detail(before_frame) - centered_detail(panned_frame)).mean())
    check("pan moved the picture on screen", change > 1.5, f"mean diff {change:.1f}")
    check("panned picture still fills the tile", fills_tile(panned_frame))
    real_drag((cx, cy), (cx + 3000, cy + 3000), steps=6)
    wait(250)
    check("dragging far off the edge stops at the edge", fills_tile(tile_pixels(ref, a)), view_of(a))
    real_drag((cx, cy), (cx - 150, cy - 60))
    shot(ref, "03_live_panned")
    saved = a.view()

    # ---- Resize while zoomed (explicit rect no longer tracks the window) ----
    home = (ref.width(), ref.height())
    ref.resize(1000, 640)
    wait(500)
    frame = tile_pixels(ref, a)
    check("zoom survives a window resize", a.view().zoom == saved.zoom, view_of(a))
    check("picture still fills the tile after resize", fills_tile(frame))
    shot(ref, "04_live_resized")
    ref.resize(*home)
    wait(400)
    check("picture fills after restore", fills_tile(tile_pixels(ref, a)))

    # ---- Hide and show: the request's persistence case ----
    button = ref.camera_ribbon._buttons_by_feed[feeds[0]]
    saved = a.view()
    QTest.mouseClick(button, LEFT)
    wait(500)
    QTest.mouseClick(button, LEFT)
    wait(700)
    check("view survives hide and show", a.view() == saved, f"{view_of(a)}")
    frame = tile_pixels(ref, a)
    check("zoomed picture comes back after show", shows_picture(frame) > 0.5 and fills_tile(frame))
    check("feed live after hide and show", alive(ref, a) >= 3)
    shot(ref, "05_after_hide_show")

    if len(feeds) > 1:
        # Hiding the OTHER feeds reshapes this tile: the case that used
        # to overwrite the stored pan.
        saved = a.view()
        others = [ref.camera_ribbon._buttons_by_feed[f] for f in feeds[1:]]
        for other in others:
            QTest.mouseClick(other, LEFT)
            wait(350)
        wait(400)
        shot(ref, "05b_others_hidden")
        check("alone in the panel, picture still on screen", shows_picture(tile_pixels(ref, a)) > 0.3)
        for other in others:
            QTest.mouseClick(other, LEFT)
            wait(350)
        wait(500)
        check("view survives the other feeds being hidden", a.view() == saved, view_of(a))
        check("picture fills the tile again", fills_tile(tile_pixels(ref, a)))

        b = tiles[feeds[1]]
        check("other feed keeps its own view", b.view() == FeedView(), view_of(b))
        bx, by = to_screen(b, b.width() // 2, b.height() // 2)
        real_ctrl_wheel(bx, by, 2)
        check("each feed zooms independently", b.view().is_zoomed and b.view() != a.view(), f"{view_of(a)} vs {view_of(b)}")
        real_ctrl_wheel(bx, by, -3)
        wait(300)
        check("second feed zoomed back to 1x", b.view() == FeedView(), view_of(b))
        lit = shows_picture(tile_pixels(ref, b))
        check("zoomed-out live picture not blank", lit > 0.3, f"lit {lit:.2f}")
        check("zoomed-out live feed keeps updating", alive(ref, b) >= 3)
        shot(ref, "06_two_views")

    # ---- Seek bar ----
    check("seek bar disabled outside a challenge", not bar.is_bar_enabled())
    QTest.mouseClick(o.long_recording_button, LEFT)
    wait(2500)
    QTest.mouseClick(o.next_play_button, LEFT)
    wait(9000)
    QTest.mouseClick(o.next_play_button, LEFT)
    wait(5500)
    QTest.mouseClick(o.challenge_button, LEFT)
    wait(1200)
    state = ref._controller.get_state()
    check("challenge enables the seek bar", bar.is_bar_enabled())
    check("state carries the clip bounds", state.review_range_ns is not None, state.review_range_ns)
    start_ns, end_ns = state.review_range_ns
    print(f"clip length {(end_ns - start_ns) / 1e9:.2f}s", flush=True)

    # Zoom follows the feed into replay.
    check("view carried into replay", a.view() == saved, view_of(a))
    frame = tile_pixels(ref, a)
    check("replay picture is zoomed too", fills_tile(frame), f"lit {shows_picture(frame):.2f}")
    shot(ref, "07_replay_zoomed")
    pre = a.view()
    real_drag((cx, cy), (cx + 120, cy + 40))
    check("real drag pans in replay", a.view() != pre, view_of(a))
    real_ctrl_wheel(cx, cy, 1)
    check("real Ctrl+wheel zooms in replay", a.view().zoom > pre.zoom, view_of(a))
    wait(200)
    shot(ref, "08_replay_panned")

    # Real clicks on the bar.
    left, right = bar._track_span()
    for fraction in (0.25, 0.75, 0.1):
        px, py = to_screen(bar, left + (right - left) * fraction, bar.height() // 2)
        user32.SetCursorPos(px, py)
        wait(60)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        wait(40)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        wait(400)
        position = ref._controller.get_playback_session_time_ns()
        landed = (position - start_ns) / (end_ns - start_ns)
        check(f"click at {fraction:.0%} seeks there", abs(landed - fraction) < 0.05, f"landed {landed:.3f}")
    shot(ref, "09_seek_bar")

    # A real drag across the bar.
    px0, py0 = to_screen(bar, left + (right - left) * 0.1, bar.height() // 2)
    px1, _ = to_screen(bar, left + (right - left) * 0.6, bar.height() // 2)
    seeks = []
    bar.seek_requested.connect(seeks.append)
    t0 = time.perf_counter()
    real_drag((px0, py0), (px1, py0), steps=20)
    elapsed = time.perf_counter() - t0
    wait(300)
    position = ref._controller.get_playback_session_time_ns()
    landed = (position - start_ns) / (end_ns - start_ns)
    check("drag lands where released", abs(landed - 0.6) < 0.05, f"landed {landed:.3f}")
    check("drag seeks along the way", len(seeks) >= 3, f"{len(seeks)} seeks in {elapsed:.2f}s")
    bar.seek_requested.disconnect(seeks.append)

    # Resume policy: a seek interrupts playback, then restores it.
    QTest.mouseClick(r.half_speed_button, LEFT)
    wait(500)
    rate = lambda: ref._controller.get_state().playback_overlay.playback_rate
    check("playing at 1/2x before the seek", rate() == 0.5, rate())
    px, py = to_screen(bar, left + (right - left) * 0.4, bar.height() // 2)
    user32.SetCursorPos(px, py)
    wait(60)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    wait(120)
    during = rate()
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    wait(400)
    check("seek pauses under the hand, then resumes at 1/2x", during == 0.0 and rate() == 0.5, f"{during} -> {rate()}")
    t0 = ref._controller.get_playback_session_time_ns()
    wait(1200)
    fraction_before = (t0 - start_ns) / (end_ns - start_ns)
    check("handle advances during playback", bar._played_fraction() > fraction_before, f"{fraction_before:.3f} -> {bar._played_fraction():.3f}")

    # Resize during replay, back to live at a size the sink never saw.
    QTest.mouseClick(r.pause_button, LEFT)
    wait(300)
    ref.resize(1100, 700)
    wait(500)
    QTest.mouseClick(o.next_play_button, LEFT)
    wait(1500)
    check("seek bar disabled after the challenge", not bar.is_bar_enabled())
    check("no clip under review", ref._controller.get_state().review_range_ns is None)
    check("view carried back to live", a.view().is_zoomed, view_of(a))
    frame = tile_pixels(ref, a)
    check("live picture zoomed at the new size", fills_tile(frame))
    check("back on the native surface", a._surface_stack.currentWidget() is a._live_surface)
    check("live again after replay", alive(ref, a) >= 3)
    shot(ref, "10_back_to_live")
    ref.resize(*home)
    wait(400)

    # Zoom all the way out: the (-1,-1,-1,-1) blanking trap.
    real_ctrl_wheel(cx, cy, -14)
    wait(400)
    check("zoomed fully out", a.view() == FeedView(), view_of(a))
    lit = shows_picture(tile_pixels(ref, a))
    check("fully zoomed-out live picture not blank", lit > 0.3, f"lit {lit:.2f}")
    check("fully zoomed-out feed live", alive(ref, a) >= 3)
    ref.resize(1000, 640)
    wait(500)
    lit = shows_picture(tile_pixels(ref, a))
    check("zoomed-out tile still tracks a resize", lit > 0.3, f"lit {lit:.2f}")
    ref.resize(*home)
    wait(400)
    shot(ref, "11_zoomed_out")

    # Max zoom on a maximized window: the largest surface this can ask for.
    ref.raise_()
    ref.showMaximized()
    wait(700)
    mx, my = to_screen(a, a.width() // 2, a.height() // 2)
    real_ctrl_wheel(mx, my, 10)
    wait(600)
    check("max zoom reached", a.view().zoom == MAX_ZOOM, view_of(a))
    frame = tile_pixels(ref, a)
    check("max zoom picture on screen", shows_picture(frame) > 0.3 and fills_tile(frame))
    check("max zoom feed live", alive(ref, a) >= 3)
    x, y, w, h = rect_of(a._surface_stack_host)
    print(f"max surface {round(w * MAX_ZOOM)}x{round(h * MAX_ZOOM)}", flush=True)
    shot(ref, "12_max_zoom")
    for s in coordinator.telemetry_hub.snapshot():
        print(f"{s.feed_id} src={s.source_fps:.1f} prv={s.preview_fps:.1f} rec={s.recording_fps:.1f} drop={s.dropped_per_sec:.1f}", flush=True)
    ref.showNormal()
    wait(500)

    def accept():
        modal = QApplication.activeModalWidget()
        modal.button(QMessageBox.StandardButton.Yes).click()

    QTimer.singleShot(500, accept)
    QTest.mouseClick(o.long_recording_button, LEFT)
    wait(800)
    check("game ended", o.long_recording_button.text() == "Begin Game")


def wrapper():
    position = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(position))
    try:
        run()
    except Exception:
        traceback.print_exc()
        results.append((False, "driver exception", traceback.format_exc()))
    finally:
        user32.keybd_event(0x11, 0, 2, 0)
        user32.SetCursorPos(position.x, position.y)
        sampler.done.set()
        fails = [item for item in results if not item[0]]
        print(f"\n{len(results) - len(fails)}/{len(results)} checks passed", flush=True)
        for item in fails:
            print("  FAIL:", item[1], item[2][:200], flush=True)
        QApplication.quit()


qt_app, coordinator, windows = app_main.build_application()
screen_size = QGuiApplication.primaryScreen().size()
grabber = Capture(screen_size.width(), screen_size.height())
sampler = Sampler(screen_size.width(), screen_size.height())
for window in windows:
    window.show()
QTimer.singleShot(0, wrapper)
code = qt_app.exec()
print("exit", code, flush=True)
watchdog.cancel()
# Skips DLL teardown, where the NDI library can hang forever at exit.
ctypes.WinDLL("kernel32").TerminateProcess(wintypes.HANDLE(-1), code)
