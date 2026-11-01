"""Drive the real app: real windows, real coordinator, synthetic feeds."""
import math
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, "C:/git/r3-app")
watchdog = threading.Timer(150, lambda: (print("WATCHDOG", flush=True), os._exit(3)))
watchdog.daemon = True
watchdog.start()

from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as app_main
from app.ui.control_button import PRESS_FEEDBACK_COLOR, ControlButton

LEFT = Qt.MouseButton.LeftButton
os.makedirs("shots", exist_ok=True)
results = []


def wait(ms):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()


def check(name, ok, detail=""):
    results.append((bool(ok), name, str(detail)))
    suffix = f"  [{detail}]" if detail != "" else ""
    print(("PASS " if ok else "FAIL ") + name + suffix, flush=True)


def fill(btn):
    return btn.grab().toImage().pixelColor(8, btn.height() // 2).name()


def shot(win, name):
    win.grab().save(f"shots/{name}.png")


def press(btn, label, win=None):
    """Real press/release; measures the flash and how long clicked blocks."""
    t0 = time.perf_counter()
    QTest.mousePress(btn, LEFT)
    during = fill(btn)
    QTest.mouseRelease(btn, LEFT)
    handler_ms = (time.perf_counter() - t0) * 1000
    after_release = fill(btn)
    if win is not None:
        shot(win, f"flash_{label}")
    while btn.is_flashing() and time.perf_counter() - t0 < 3:
        wait(5)
    total_ms = (time.perf_counter() - t0) * 1000
    check(f"{label}: amber while held", during == PRESS_FEEDBACK_COLOR, during)
    check(
        f"{label}: still amber after release",
        after_release == PRESS_FEEDBACK_COLOR,
        after_release,
    )
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


def text_fit(win, name):
    bad = []
    for b in win.findChildren(ControlButton):
        b.ensurePolished()
        need = b.fontMetrics().horizontalAdvance(b.text()) + 2 * 4
        if need > b.width():
            bad.append((b.text(), need, b.width()))
    check(f"{name}: all button labels fit", not bad, bad or "")


def run():
    ref, op = windows
    op.move(40, 40)
    ref.move(1200, 480)
    wait(1800)
    print(
        "platform", QApplication.platformName(),
        "style", QApplication.style().objectName(),
        "font", QApplication.font().family(), flush=True,
    )
    for w, n in ((ref, "referee"), (op, "operator")):
        tile = next(iter(w.video_panel.widgets_by_feed_id.values()))
        check(f"{n}: synthetic video rendering", tile._current_image is not None)
        check(
            f"{n}: opened at requested size",
            (w.width(), w.height()) == (1280, 860),
            f"{w.width()}x{w.height()}",
        )
        text_fit(w, n)
        rail = w.camera_ribbon.parentWidget()
        check(
            f"{n}: rail min width fits 220",
            rail.minimumSizeHint().width() <= 220,
            rail.minimumSizeHint().width(),
        )
        hint = w.minimumSizeHint()
        print(f"   {n} window min size {hint.width()}x{hint.height()}", flush=True)
        shot(w, f"01_idle_{n}")

    o, r = op.operator_controls, ref.referee_controls

    # Begin Game
    check("Next Play disabled before game", not o.next_play_button.isEnabled())
    press(o.long_recording_button, "begin_game", op)
    wait(400)
    check("Begin -> End Game label", o.long_recording_button.text() == "End Game")
    color = fill(o.long_recording_button)
    check("End Game is red", color == "#c33b3b", color)
    message = op._status_bar.text()
    check("Begin Game status reaches the operator", message == "Game recording started.", message)

    # Stylesheet churn while UI ticks are running
    calls = []
    orig = o.long_recording_button.setStyleSheet
    o.long_recording_button.setStyleSheet = lambda *a: (calls.append(1), orig(*a))
    wait(1000)
    o.long_recording_button.setStyleSheet = orig
    check("No stylesheet churn on UI ticks (1s)", len(calls) == 0, len(calls))

    # Next Play / clip readout
    before = op.camera_ribbon.selector_label.text()
    press(o.next_play_button, "next_play", op)
    wait(200)
    after = op.camera_ribbon.selector_label.text()
    check("Next Play moved the readout", before != after, f"{before!r} -> {after!r}")
    ref_text = ref.camera_ribbon.selector_label.text()
    check("Referee readout matches operator", ref_text == after, ref_text)

    # Mark Play
    press(o.mark_play_button, "mark_play", op)
    wait(150)
    check(
        "Mark Play latched",
        o.mark_play_button.is_active() and o.mark_play_button.text() == "✓ Marked",
        o.mark_play_button.text(),
    )
    color = fill(o.mark_play_button)
    check("Marked shows blue", color == "#1f6feb", color)
    message = op._status_bar.text()
    check("Mark ack message in rail", "marked" in message.lower(), message)
    shot(op, "02_marked_operator")
    press(o.mark_play_button, "unmark_play")
    wait(150)
    check(
        "Unmark clears latch",
        not o.mark_play_button.is_active()
        and o.mark_play_button.text() == "Mark Play",
    )
    press(o.mark_play_button, "remark_play")
    wait(150)
    press(o.next_play_button, "next_play_2")
    wait(150)
    check("New clip starts unmarked", not o.mark_play_button.is_active())

    # Time-out latch
    press(o.timeout_button, "timeout", op)
    wait(150)
    check("Time-out latched", o.timeout_button.is_active())
    press(o.next_play_button, "next_play_3")
    wait(150)
    check("Time-out latch cleared by Next Play", not o.timeout_button.is_active())
    print("   waiting for segments to finalize", flush=True)
    wait(4500)
    press(o.next_play_button, "next_play_4")
    wait(3000)

    # Challenge: the self-disabling case
    check("Referee transport disabled pre-challenge", not r.half_speed_button.isEnabled())
    press(o.challenge_button, "challenge", op)
    wait(600)
    check(
        "Challenge latched while disabled",
        o.challenge_button.is_active() and not o.challenge_button.isEnabled(),
    )
    color = fill(o.challenge_button)
    check("Challenge shows blue while disabled", color == "#1f6feb", color)
    check(
        "Referee transport enabled in challenge",
        r.half_speed_button.isEnabled() and ref.jog_wheel.is_wheel_enabled(),
    )
    shot(op, "03_challenge_operator")
    shot(ref, "03_challenge_referee")

    # Referee transport
    rate = lambda: ref._controller.get_state().playback_overlay.playback_rate
    check("Paused at play start", rate() == 0.0, rate())
    check("Pause button reads Play", "Play" in r.pause_button.text(), r.pause_button.text())
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
    check(
        "Step advanced replay position",
        t0 is not None and t1 is not None and t1 > t0,
        f"{t0} -> {t1}",
    )
    press(r.step_back_button, "step_back", ref)
    press(r.rewind_button, "rewind", ref)
    wait(200)

    # Jog wheel
    wheel = ref.jog_wheel
    def amber_pixels(image):
        return sum(
            image.pixelColor(x, y).name() == PRESS_FEEDBACK_COLOR
            for x in range(image.width())
            for y in range(image.height())
        )

    t0 = ref._controller.get_playback_session_time_ns()
    idle = amber_pixels(wheel.grab().toImage())
    engaged_pixmap = jog(wheel, deg=-90)
    engaged_pixmap.save("shots/05_jog_engaged.png")
    engaged = amber_pixels(engaged_pixmap.toImage())
    wait(400)
    t1 = ref._controller.get_playback_session_time_ns()
    check("Jog rim amber while engaged", idle == 0 and engaged > 100, f"{idle} -> {engaged} amber px")
    check("Jog moved replay position", t0 != t1, f"{t0} -> {t1}")
    check("Rim returns after gesture", amber_pixels(wheel.grab().toImage()) == 0)

    # Camera show/hide (per window)
    ribbon, panel = ref.camera_ribbon, ref.video_panel
    press(ribbon._buttons_by_feed["cam_4"], "cam4_hide", ref)
    press(ribbon._buttons_by_feed["cam_3"], "cam3_hide", ref)
    wait(200)
    check("Referee refit to 2 feeds", panel._arrangement[0] == ["cam_1", "cam_2"], panel._arrangement)
    check("Operator unaffected", len(op.video_panel._arrangement[0]) == 4)
    color = fill(ribbon._buttons_by_feed["cam_4"])
    check("Hidden camera button not blue", color != "#1f6feb", color)
    shot(ref, "06_two_feeds_referee")
    press(ribbon._buttons_by_feed["cam_2"], "cam2_hide", ref)
    press(ribbon._buttons_by_feed["cam_3"], "cam3_show")
    press(ribbon._buttons_by_feed["cam_4"], "cam4_show")
    press(ribbon._buttons_by_feed["cam_2"], "cam2_show")
    wait(200)
    check("All cameras restored", len(panel._arrangement[0]) == 4)
    check(
        "Reflow did not resize the window",
        (ref.width(), ref.height()) == (1280, 860),
        f"{ref.width()}x{ref.height()}",
    )

    # Maximize mid-replay, then restore: pixmaps must not pin the tile size
    for w, n in ((ref, "referee"), (op, "operator")):
        w.showMaximized()
        wait(500)
        host = next(iter(w.video_panel.widgets_by_feed_id.values()))._surface_stack_host
        print(f"   {n} maximized {w.width()}x{w.height()} tile {host.width()}x{host.height()}", flush=True)
        shot(w, f"07_max_{n}")
        w.showNormal()
        wait(400)
        check(
            f"{n}: restores to 1280x860 after maximize",
            (w.width(), w.height()) == (1280, 860),
            f"{w.width()}x{w.height()}",
        )

    # Challenge ends
    press(o.next_play_button, "next_play_end_challenge")
    wait(500)
    check("Challenge latch cleared", not o.challenge_button.is_active())
    check(
        "Referee transport greyed again",
        not r.half_speed_button.isEnabled() and not ref.jog_wheel.is_wheel_enabled(),
    )
    check("No speed button lit after challenge", not any(b.is_active() for b, _ in r._rate_buttons))

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


def wrapper():
    try:
        run()
    except Exception:
        traceback.print_exc()
        results.append((False, "driver exception", traceback.format_exc()))
    finally:
        fails = [item for item in results if not item[0]]
        print(f"\n{len(results) - len(fails)}/{len(results)} checks passed", flush=True)
        for item in fails:
            print("  FAIL:", item[1], item[2], flush=True)
        QApplication.quit()


qt_app, coordinator, windows = app_main.build_application()
for window in windows:
    window.show()
QTimer.singleShot(0, wrapper)
code = qt_app.exec()
print("exit", code, flush=True)
watchdog.cancel()
sys.exit(code)
