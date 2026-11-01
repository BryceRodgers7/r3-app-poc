"""Drive the real app against a real NDI camera.

NDI video is drawn straight into a native child window, so QWidget.grab()
can't see it. Everything about the picture is read from the screen here.
"""
import ctypes
import math
import os
import statistics
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from ctypes import wintypes

sys.path.insert(0, "C:/git/r3-app")
watchdog = threading.Timer(480, lambda: (print("WATCHDOG", flush=True), os._exit(3)))
watchdog.daemon = True
watchdog.start()

import numpy as np
from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as app_main
from app.ui.control_button import PRESS_FEEDBACK_COLOR, ControlButton

LEFT = Qt.MouseButton.LeftButton
os.makedirs("shots", exist_ok=True)
results = []
events = []
flash_ms = []


def check(name, ok, detail=""):
    results.append((bool(ok), name, str(detail)))
    suffix = f"  [{detail}]" if detail != "" else ""
    print(("PASS " if ok else "FAIL ") + name + suffix, flush=True)


def wait(ms):
    """Spin the event loop. QTest's own wait holds the GIL throughout, which
    stalls every Python pad probe on the GStreamer threads."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()


# --- Screen capture -------------------------------------------------------
SRCCOPY = 0x00CC0020
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
]
ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM_PROC, wintypes.LPARAM]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class Capture:
    """Whole-screen BGRA grabs. One per thread: GDI DCs can't be shared."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        self.bitmap = gdi32.CreateCompatibleBitmap(self.screen, width, height)
        gdi32.SelectObject(self.mem, self.bitmap)
        self.header = BITMAPINFOHEADER(
            ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0
        )
        self.buffer = np.empty((height, width, 4), dtype=np.uint8)

    def grab(self):
        gdi32.BitBlt(
            self.mem, 0, 0, self.width, self.height, self.screen, 0, 0, SRCCOPY
        )
        gdi32.GetDIBits(
            self.mem, self.bitmap, 0, self.height,
            self.buffer.ctypes.data, ctypes.byref(self.header), 0,
        )
        return self.buffer


# Tile background, window background (BGR), and pure black.
CHROME_COLORS = ((0, 0, 0), (16, 16, 16), (28, 23, 20))


def classify(frame, roi):
    x, y, w, h = roi
    view = frame[y : y + h : 3, x : x + w : 3, :3].astype(np.int16)
    chrome = np.zeros(view.shape[:2], dtype=bool)
    for color in CHROME_COLORS:
        chrome |= np.abs(view - np.array(color, dtype=np.int16)).max(axis=2) <= 4
    white = view.min(axis=2) >= 240
    return float(chrome.mean()), float(white.mean())


class Sampler(threading.Thread):
    """Reads the screen on its own thread, so it keeps sampling while the
    UI thread is busy reflowing."""

    def __init__(self, width, height):
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.roi = None
        self.samples = []
        self.done = threading.Event()
        self.peak_score = -1.0
        self.peak_frame = None

    def reset_peak(self):
        self.peak_score = -1.0
        self.peak_frame = None

    def run(self):
        capture = Capture(self.width, self.height)
        while not self.done.is_set():
            roi = self.roi
            if roi is None:
                time.sleep(0.01)
                continue
            frame = capture.grab()
            stamp = time.perf_counter()
            chrome, white = classify(frame, roi)
            self.samples.append((stamp, chrome, white))
            if chrome + white > self.peak_score:
                self.peak_score = chrome + white
                self.peak_frame = frame.copy()


def rect_of(widget):
    origin = widget.mapToGlobal(QPoint(0, 0))
    return origin.x(), origin.y(), widget.width(), widget.height()


def save_bgra(frame, rect, path):
    x, y, w, h = rect
    x, y = max(x, 0), max(y, 0)
    crop = frame[y : y + h, x : x + w]
    height, width = crop.shape[:2]
    QImage(
        crop.tobytes(), width, height, width * 4, QImage.Format.Format_RGB32
    ).copy().save(path)


def shot(win, name):
    frame = grabber.grab()
    g = win.frameGeometry()
    save_bgra(frame, (g.x(), g.y(), g.width(), g.height()), f"shots/{name}.png")


def covered(win, x, y):
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return True
    return int(user32.GetAncestor(hwnd, 2) or 0) != int(win.winId())


def own_top_levels():
    found = []

    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(ENUM_PROC(visit), 0)
    return found


def pictures(win, label, moving=True):
    """Every visible tile on `win` shows a right-sized picture, on screen."""
    panel = win.video_panel
    frames = []
    for _ in range(5):
        frames.append(grabber.grab().copy())
        wait(110)
    tiles = panel.widgets_by_feed_id
    for feed_id in panel._arrangement[0]:
        x, y, w, h = rect_of(tiles[feed_id]._surface_stack_host)
        if covered(win, x + w // 2, y + h // 2):
            check(f"{label}: {feed_id} picture", False, "another window covers it")
            continue
        lit = frames[-1][y : y + h, x : x + w, :3].max(axis=2) > 24
        cols = np.flatnonzero(lit.mean(axis=0) > 0.1)
        rows = np.flatnonzero(lit.mean(axis=1) > 0.1)
        if cols.size == 0 or rows.size == 0:
            check(f"{label}: {feed_id} picture", False, f"blank, tile {w}x{h}")
            continue
        left, right = int(cols[0]), w - 1 - int(cols[-1])
        top, bottom = int(rows[0]), h - 1 - int(rows[-1])
        pic_w, pic_h = int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)
        distinct = len(
            {f[y : y + h : 8, x : x + w : 8].tobytes() for f in frames}
        )
        fills = min(left + right, top + bottom) <= 6
        centered = abs(left - right) <= 6 and abs(top - bottom) <= 6
        ratio_ok = abs(pic_w / pic_h - 16 / 9) < 0.06
        alive = distinct >= 3 if moving else True
        check(
            f"{label}: {feed_id} picture",
            fills and centered and ratio_ok and alive,
            f"tile {w}x{h} pic {pic_w}x{pic_h} "
            f"margins l{left} r{right} t{top} b{bottom} distinct {distinct}/5",
        )


@contextmanager
def watch(name, win, moved=False):
    """Compare the panel on screen during an action with before and after.

    `moved` is for actions that change the window's geometry: only the
    after-state is comparable, so the region is re-read when they return.
    """
    panel = win.video_panel
    sampler.roi = rect_of(panel)
    wait(260)
    start = time.perf_counter()
    sampler.reset_peak()
    yield
    if moved:
        sampler.roi = rect_of(panel)
        start = time.perf_counter()
        sampler.reset_peak()
    wait(750)
    end = time.perf_counter()
    samples = list(sampler.samples)
    pre = [s for s in samples if start - 0.25 <= s[0] < start]
    during = [s for s in samples if start <= s[0] < end - 0.2]
    post = [s for s in samples if end - 0.2 <= s[0] <= end]
    if not during or not post or (not moved and not pre):
        check(f"flicker sampled: {name}", False, "no samples")
        return
    settled = [post] if moved else [pre, post]
    base_c = max(statistics.median(s[1] for s in part) for part in settled)
    base_w = max(statistics.median(s[2] for s in part) for part in settled)
    peak_c = max(s[1] for s in during)
    peak_w = max(s[2] for s in during)
    bad = [s for s in during if s[1] > base_c + 0.03 or s[2] > base_w + 0.03]
    rate = len(during) / max(during[-1][0] - during[0][0], 0.001)
    events.append(
        (name, len(during), rate, base_c, peak_c, base_w, peak_w, len(bad))
    )
    if bad and sampler.peak_frame is not None:
        save_bgra(
            sampler.peak_frame,
            sampler.roi,
            "shots/spike_" + "".join(c if c.isalnum() else "_" for c in name) + ".png",
        )
    check(
        f"no flicker: {name}",
        not bad,
        f"{len(bad)}/{len(during)} samples, dark {base_c:.3f}->{peak_c:.3f}, "
        f"white {base_w:.3f}->{peak_w:.3f}",
    )


# --- Input ----------------------------------------------------------------
def fill(btn):
    return btn.grab().toImage().pixelColor(8, btn.height() // 2).name()


def on_screen_fill(btn):
    origin = btn.mapToGlobal(QPoint(8, btn.height() // 2))
    blue, green, red, _ = grabber.grab()[origin.y(), origin.x()]
    return f"#{red:02x}{green:02x}{blue:02x}"


def press(btn, label, win=None):
    """Real press/release; measures the flash and how long clicked blocks."""
    t0 = time.perf_counter()
    QTest.mousePress(btn, LEFT)
    during = fill(btn)
    # Held like a real finger: DWM gets a frame to composite the flash,
    # and the sample lands before a slow click handler can outlast it.
    wait(45)
    visible = on_screen_fill(btn)
    frame = grabber.grab().copy() if win is not None else None
    release_at = time.perf_counter()
    QTest.mouseRelease(btn, LEFT)
    handler_ms = (time.perf_counter() - release_at) * 1000
    after_release = fill(btn)
    while btn.is_flashing() and time.perf_counter() - t0 < 3:
        wait(5)
    total_ms = (time.perf_counter() - t0) * 1000
    flash_ms.append(total_ms)
    if frame is not None:
        g = win.frameGeometry()
        save_bgra(frame, (g.x(), g.y(), g.width(), g.height()), f"shots/flash_{label}.png")
    check(f"{label}: amber while held", during == PRESS_FEEDBACK_COLOR, during)
    check(
        f"{label}: still amber after release",
        after_release == PRESS_FEEDBACK_COLOR,
        after_release,
    )
    check(f"{label}: amber on the real screen", visible == PRESS_FEEDBACK_COLOR, visible)
    check(
        f"{label}: flash lasted >=240ms",
        total_ms >= 240,
        f"{total_ms:.0f}ms, click blocked {handler_ms:.0f}ms",
    )


def jog(wheel, deg):
    cx, cy, r = wheel.width() / 2, wheel.height() / 2, wheel.width() / 2 - 20

    def ev(kind, a, btns):
        pos = QPointF(cx + r * math.cos(a), cy - r * math.sin(a))
        return QMouseEvent(kind, pos, LEFT, btns, Qt.KeyboardModifier.NoModifier)

    a = math.pi / 2
    wheel.mousePressEvent(ev(QEvent.Type.MouseButtonPress, a, LEFT))
    engaged = wheel.grab()
    steps = 12
    for _ in range(steps):
        a -= math.radians(deg / steps)
        wheel.mouseMoveEvent(ev(QEvent.Type.MouseMove, a, LEFT))
        wait(12)
    wait(150)
    wheel.mouseReleaseEvent(
        ev(QEvent.Type.MouseButtonRelease, a, Qt.MouseButton.NoButton)
    )
    return engaged


# --- Telemetry ------------------------------------------------------------
cpu_mark = [time.perf_counter(), time.process_time()]


def telemetry(tag):
    now, cpu = time.perf_counter(), time.process_time()
    share = (cpu - cpu_mark[1]) / max(now - cpu_mark[0], 0.001) * 100
    cpu_mark[:] = [now, cpu]
    print(f"   [{tag}] cpu {share:.0f}% of one core ({os.cpu_count()} logical)", flush=True)
    for s in coordinator.telemetry_hub.snapshot():
        print(
            f"   [{tag}] {s.feed_id} mode={s.pipeline_mode} src={s.source_fps:.1f} "
            f"prv={s.preview_fps:.1f} rec={s.recording_fps:.1f} "
            f"drop={s.dropped_per_sec:.1f}/s dup={s.rate_duplicated_per_sec:.1f}/s "
            f"qprv={s.queue_depth_preview}/{s.queue_max_preview} "
            f"qrec={s.queue_depth_recording}/{s.queue_max_recording} "
            f"enc={s.recording_encoder}",
            flush=True,
        )


def wait_for_feeds(seconds):
    deadline = time.perf_counter() + seconds
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        snaps = coordinator.telemetry_hub.snapshot()
        if len(snaps) == len(FEEDS) and all(s.source_fps > 5 for s in snaps):
            return time.perf_counter() - t0
        wait(200)
    return None


# --- Scenarios ------------------------------------------------------------
def visibility_cycle(win, label, moving=True):
    """Hide every camera one at a time, then bring each back."""
    ribbon, panel = win.camera_ribbon, win.video_panel
    home = (win.width(), win.height())
    for feed_id in reversed(FEEDS):
        with watch(f"{label} hide {feed_id}", win):
            press(ribbon._buttons_by_feed[feed_id], f"{label}_hide_{feed_id}")
        check(
            f"{label}: {feed_id} button not blue when hidden",
            fill(ribbon._buttons_by_feed[feed_id]) != "#1f6feb",
        )
        pictures(win, f"{label} after hide {feed_id}", moving)
    shot(win, f"{label}_all_hidden")
    for feed_id in FEEDS:
        with watch(f"{label} show {feed_id}", win):
            press(ribbon._buttons_by_feed[feed_id], f"{label}_show_{feed_id}")
        pictures(win, f"{label} after show {feed_id}", moving)
    check(f"{label}: all cameras restored", panel._arrangement[0] == FEEDS)
    check(
        f"{label}: reflow did not resize the window",
        (win.width(), win.height()) == home,
        f"{win.width()}x{win.height()}",
    )
    shot(win, f"{label}_restored")


def resize_sweep(win, label, moving=True):
    """Drag the window narrow and back, crossing the grid's column changes."""
    panel = win.video_panel
    home = (win.width(), win.height())
    # The panel's left edge stays panel at every width in the sweep.
    x, y, _, h = rect_of(panel)
    sampler.roi = (x, y, 360, h)
    wait(260)
    start = time.perf_counter()
    seen = []
    widths = list(range(home[0] - 40, 599, -40)) + list(range(640, home[0] + 1, 40))
    for width in widths:
        win.resize(width, home[1])
        wait(45)
        columns = panel._arrangement[1]
        if not seen or seen[-1] != columns:
            seen.append(columns)
            wait(350)
            pictures(win, f"{label} at {width}w {columns}col", moving)
            shot(win, f"{label}_{width}w_{columns}col")
    end = time.perf_counter()
    wait(400)
    during = [s for s in sampler.samples if start <= s[0] <= end]
    base = statistics.median(s[2] for s in sampler.samples[-10:])
    peak = max(s[2] for s in during)
    check(
        f"{label}: no white flash during drag",
        peak <= base + 0.03,
        f"white {base:.3f}->{peak:.3f} over {len(during)} samples",
    )
    print(f"   {label}: columns seen {seen}", flush=True)
    check(f"{label}: sweep restored size", (win.width(), win.height()) == home)
    pictures(win, f"{label} after sweep", moving)


def maximize_cycle(win, label, moving=True):
    panel = win.video_panel
    win.raise_()
    home = (win.width(), win.height())
    with watch(f"{label} maximize", win, moved=True):
        win.showMaximized()
        wait(150)
    x, y, w, h = rect_of(next(iter(panel.widgets_by_feed_id.values()))._surface_stack_host)
    print(f"   {label} maximized {win.width()}x{win.height()} tile {w}x{h}", flush=True)
    pictures(win, f"{label} maximized", moving)
    shot(win, f"{label}_maximized")
    with watch(f"{label} restore", win, moved=True):
        win.showNormal()
        wait(150)
    check(
        f"{label}: restores size",
        (win.width(), win.height()) == home,
        f"{win.width()}x{win.height()}",
    )
    pictures(win, f"{label} restored", moving)


def run():
    ref, op = windows
    dx = op.geometry().x() - op.frameGeometry().x()
    op.move(-dx, 0)
    ref.move(1280 - dx, 0)
    for window in (op, ref):
        user32.SetWindowPos(int(window.winId()), wintypes.HWND(-1), 0, 0, 0, 0, 0x13)
    wait(300)
    connected = wait_for_feeds(75)
    check("camera connected", connected is not None, f"{connected}s")
    if connected is None:
        telemetry("no camera")
        raise RuntimeError("camera never delivered frames; nothing below would mean anything")
    wait(2500)
    print(
        "platform", QApplication.platformName(),
        "dpr", QGuiApplication.primaryScreen().devicePixelRatio(),
        "feeds", FEEDS, flush=True,
    )
    telemetry("idle")
    for s in coordinator.telemetry_hub.snapshot():
        check(f"{s.feed_id}: native pipeline", s.pipeline_mode == "native", s.pipeline_mode)
    tops = own_top_levels()
    check("exactly two top-level windows", len(tops) == 2, len(tops))

    o, r = op.operator_controls, ref.referee_controls
    for w, n in ((ref, "referee"), (op, "operator")):
        for feed_id, tile in w.video_panel.widgets_by_feed_id.items():
            check(f"{n} {feed_id}: native render mode", tile._render_mode == "native", tile._render_mode)
            check(
                f"{n} {feed_id}: native surface is the visible page",
                tile._surface_stack.currentWidget() is tile._live_surface,
            )
        check(f"{n}: opened at 1280x860", (w.width(), w.height()) == (1280, 860), f"{w.width()}x{w.height()}")
        pictures(w, f"{n} idle")
        shot(w, f"01_idle_{n}")

    # Begin Game
    check("Next Play disabled before game", not o.next_play_button.isEnabled())
    press(o.long_recording_button, "begin_game", op)
    wait(400)
    check("Begin -> End Game label", o.long_recording_button.text() == "End Game")
    color = fill(o.long_recording_button)
    check("End Game is red", color == "#c33b3b", color)
    message = op._status_bar.text()
    check("Begin Game status reaches the operator", message == "Game recording started.", message)
    wait(1500)
    telemetry("recording")

    # Next Play / Mark Play / Time-out
    before = op.camera_ribbon.selector_label.text()
    press(o.next_play_button, "next_play", op)
    wait(200)
    after = op.camera_ribbon.selector_label.text()
    check("Next Play moved the readout", before != after, f"{before!r} -> {after!r}")
    press(o.mark_play_button, "mark_play", op)
    wait(150)
    check(
        "Mark Play latched",
        o.mark_play_button.is_active() and o.mark_play_button.text() == "✓ Marked",
        o.mark_play_button.text(),
    )
    color = on_screen_fill(o.mark_play_button)
    check("Marked shows blue on screen", color == "#1f6feb", color)
    shot(op, "02_marked_operator")
    press(o.mark_play_button, "unmark_play")
    wait(150)
    check("Unmark clears latch", not o.mark_play_button.is_active())
    press(o.timeout_button, "timeout", op)
    wait(150)
    check("Time-out latched", o.timeout_button.is_active())
    press(o.next_play_button, "next_play_2")
    wait(150)
    check("Time-out cleared by Next Play", not o.timeout_button.is_active())

    # Live native surfaces, while recording: the case synthetic can't reach.
    for w, n in ((op, "operator"), (ref, "referee")):
        visibility_cycle(w, f"{n} live")
        resize_sweep(w, f"{n} live")
        maximize_cycle(w, f"{n} live")
    telemetry("after reflow")
    check("still two top-level windows", len(own_top_levels()) == 2, len(own_top_levels()))
    pictures(op, "operator after all reflow")
    pictures(ref, "referee after all reflow")

    print("   waiting for segments to finalize", flush=True)
    wait(9000)
    press(o.next_play_button, "next_play_3")
    wait(6000)

    # Challenge: referee leaves the native surface for the replay layer
    check("Referee transport disabled pre-challenge", not r.half_speed_button.isEnabled())
    with watch("referee live->replay", ref):
        press(o.challenge_button, "challenge", op)
    wait(400)
    check(
        "Challenge latched while disabled",
        o.challenge_button.is_active() and not o.challenge_button.isEnabled(),
    )
    color = on_screen_fill(o.challenge_button)
    check("Challenge shows blue on screen", color == "#1f6feb", color)
    check(
        "Referee transport enabled in challenge",
        r.half_speed_button.isEnabled() and ref.jog_wheel.is_wheel_enabled(),
    )
    shot(op, "03_challenge_operator")
    shot(ref, "03_challenge_referee")
    pictures(ref, "referee in replay", moving=False)
    pictures(op, "operator during replay")
    telemetry("challenge")

    # Referee transport
    rate = lambda: ref._controller.get_state().playback_overlay.playback_rate
    check("Paused at play start", rate() == 0.0, rate())
    for btn, expected, label in (
        (r.half_speed_button, 0.5, "half"),
        (r.quarter_speed_button, 0.25, "quarter"),
        (r.eighth_speed_button, 0.125, "eighth"),
        (r.speed_2x_button, 2.0, "double"),
    ):
        press(btn, f"speed_{label}", ref)
        wait(150)
        lit = [b.text() for b, _ in r._rate_buttons if b.is_active()]
        check(
            f"{label}: rate={expected} and only its button lit",
            rate() == expected and lit == [btn.text()],
            f"rate={rate()} lit={lit}",
        )
        shot(ref, f"04_rate_{label}")
    press(r.pause_button, "pause", ref)
    wait(150)
    check(
        "Paused: no speed button lit",
        rate() == 0.0 and not any(b.is_active() for b, _ in r._rate_buttons),
    )
    t0 = ref._controller.get_playback_session_time_ns()
    press(r.step_forward_button, "step_forward", ref)
    wait(200)
    t1 = ref._controller.get_playback_session_time_ns()
    check("Step advanced replay position", t0 is not None and t1 is not None and t1 > t0, f"{t0} -> {t1}")
    press(r.step_back_button, "step_back", ref)
    press(r.rewind_button, "rewind", ref)
    wait(200)

    # Jog wheel
    def amber_pixels(image):
        return sum(
            image.pixelColor(x, y).name() == PRESS_FEEDBACK_COLOR
            for x in range(image.width())
            for y in range(image.height())
        )

    wheel = ref.jog_wheel
    t0 = ref._controller.get_playback_session_time_ns()
    idle = amber_pixels(wheel.grab().toImage())
    engaged_pixmap = jog(wheel, deg=-90)
    engaged = amber_pixels(engaged_pixmap.toImage())
    wait(400)
    t1 = ref._controller.get_playback_session_time_ns()
    check("Jog rim amber while engaged", idle == 0 and engaged > 100, f"{idle} -> {engaged} amber px")
    check("Jog moved replay position", t0 != t1, f"{t0} -> {t1}")
    check("Rim returns after gesture", amber_pixels(wheel.grab().toImage()) == 0)

    # Hidden native surfaces: resized while the replay layer covers them
    visibility_cycle(ref, "referee replay", moving=False)
    maximize_cycle(ref, "referee replay", moving=False)
    ref.resize(1100, 700)
    wait(400)
    pictures(ref, "referee replay resized", moving=False)

    # Challenge ends with the window at a size the live surface never had
    with watch("referee replay->live resized", ref):
        press(o.next_play_button, "next_play_end_challenge")
    wait(600)
    check("Challenge latch cleared", not o.challenge_button.is_active())
    check(
        "Referee transport greyed again",
        not r.half_speed_button.isEnabled() and not ref.jog_wheel.is_wheel_enabled(),
    )
    for feed_id, tile in ref.video_panel.widgets_by_feed_id.items():
        check(
            f"referee {feed_id}: native surface back on top",
            tile._surface_stack.currentWidget() is tile._live_surface,
        )
    pictures(ref, "referee back to live at new size")
    shot(ref, "05_back_to_live_resized")
    ref.resize(1280, 860)
    wait(500)
    pictures(ref, "referee back at home size")
    telemetry("back to live")

    # End Game with the real confirm modal
    def accept():
        modal = QApplication.activeModalWidget()
        check("End Game confirm modal shown", isinstance(modal, QMessageBox))
        modal.button(QMessageBox.StandardButton.Yes).click()

    QTimer.singleShot(500, accept)
    end_btn = o.long_recording_button
    during_stop = []
    # Connected after the coordinator, so this runs as the blocking stop
    # returns, before the event loop can service the flash timer.
    o.long_recording_toggle_requested.connect(lambda: during_stop.append(fill(end_btn)))
    t0 = time.perf_counter()
    QTest.mouseClick(end_btn, LEFT)
    blocked = (time.perf_counter() - t0) * 1000
    check(
        "End Game amber through the blocking stop",
        during_stop == [PRESS_FEEDBACK_COLOR],
        f"{during_stop}, click blocked {blocked:.0f}ms",
    )
    wait(800)
    check("End -> Begin Game label", o.long_recording_button.text() == "Begin Game")
    color = fill(o.long_recording_button)
    check("Begin Game is green", color == "#2c8a3a", color)
    message = op._status_bar.text()
    check("End Game status reaches the operator", message == "Game recording stopped.", message)
    check(
        "Buttons greyed after game",
        not o.next_play_button.isEnabled() and not o.mark_play_button.is_active(),
    )
    shot(op, "08_ended_operator")
    pictures(op, "operator after game")
    pictures(ref, "referee after game")
    telemetry("after game")


def wrapper():
    try:
        run()
    except Exception:
        traceback.print_exc()
        results.append((False, "driver exception", traceback.format_exc()))
    finally:
        sampler.done.set()
        fails = [item for item in results if not item[0]]
        print("\nflicker events (dark = chrome share of panel):", flush=True)
        for name, n, hz, base_c, peak_c, base_w, peak_w, bad in events:
            print(
                f"  {name:38s} n={n:3d} {hz:4.0f}Hz dark {base_c:.3f}->{peak_c:.3f} "
                f"white {base_w:.3f}->{peak_w:.3f} bad={bad}",
                flush=True,
            )
        if flash_ms:
            print(
                f"flash n={len(flash_ms)} mean={statistics.mean(flash_ms):.0f}ms "
                f"max={max(flash_ms):.0f}ms",
                flush=True,
            )
        print(f"\n{len(results) - len(fails)}/{len(results)} checks passed", flush=True)
        for item in fails:
            print("  FAIL:", item[1], item[2], flush=True)
        QApplication.quit()


qt_app, coordinator, windows = app_main.build_application()
FEEDS = list(windows[0].video_panel.widgets_by_feed_id)
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
# Skips DLL teardown, where the NDI library can hang forever at exit.
ctypes.WinDLL("kernel32").TerminateProcess(wintypes.HANDLE(-1), code)
