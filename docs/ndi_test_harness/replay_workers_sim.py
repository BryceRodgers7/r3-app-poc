"""Free-running replay with real parts: 2 segments, real decoders, real tiles, a Qt event loop.

Usage: python replay_workers_sim.py SEGMENT_FEED_A.mkv SEGMENT_FEED_B.mkv [seconds] [busy_processes]

Drives the controller's `_submit_replay_frames` from a 33 ms QTimer the way the replay clock does,
over the first ~3.5 s of each segment (looped), and reports per feed: frames shown per second, frames
dropped, decode+scale time on the worker, and how much of the UI thread the whole thing took (tick
handler + showing frames). `busy_processes` starts that many CPU-burning processes first, to
imitate the live pipeline competing for cores. Read-only; no app, no recording needed.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from app.core.app_state import UiState
from app.core.models import PlaybackMode
from app.core.playback_controller import PlaybackController, _FramesReadyBridge
from app.media.output_renderer import MultiFeedOutputRenderer
from app.media.segment_decoder import SegmentDecoder
from app.ui.video_widget import VideoWidget

seg_a, seg_b = sys.argv[1], sys.argv[2]
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
busy = int(sys.argv[4]) if len(sys.argv) > 4 else 0
FPS = 29.97

app = QApplication([])
burners = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(busy)]
time.sleep(1.0 if busy else 0.0)

tiles = {}
renderer = MultiFeedOutputRenderer()
for feed_id in ("a", "b"):
    tile = VideoWidget()
    tile.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    tile.setMinimumHeight(10)
    tile.resize(960, 540)
    tile.show()
    renderer.bind_feed_widget(feed_id, tile)
    tiles[feed_id] = tile
app.processEvents()


class Store:
    def __init__(self, paths):
        self.paths = paths

    def nearest_frame_location(self, *, feed_id, session_time_ns, recording_state):
        t_ms = (session_time_ns / 1e6) % 3500.0  # loop inside the segment
        return SimpleNamespace(
            segment=SimpleNamespace(file_path=self.paths[feed_id]),
            offset_in_segment_ns=int(t_ms * 1e6),
            frame_index=int(t_ms * FPS / 1000.0),
            is_freeze=False,
        )


pc = PlaybackController.__new__(PlaybackController)
pc._output_renderer = renderer
pc._segment_decoders = {"a": SegmentDecoder("a", "a"), "b": SegmentDecoder("b", "b")}
pc._feed_runtimes = tuple(SimpleNamespace(feed=SimpleNamespace(feed_id=f)) for f in ("a", "b"))
pc._recording_manager = SimpleNamespace(recording_state=SimpleNamespace(state="recording"))
pc._replay_store = Store({"a": seg_a, "b": seg_b})
pc._lock = threading.RLock()
pc._state = UiState()
pc._state.current_playback_mode = PlaybackMode.REPLAY
pc._replay_epoch = 0
pc._replay_workers = {}
pc._decode_pool = None
pc._shutting_down = False
pc._replay_stats_logged_at = time.monotonic() + 1e9  # no periodic log lines here
pc._frames_bridge = _FramesReadyBridge(pc._deliver_replay_frames)

ui_busy = 0.0
shown_by_feed = {"a": 0, "b": 0}
real_show = renderer.show_frame


def timed_show(frame):
    global ui_busy
    started = time.perf_counter()
    real_show(frame)
    ui_busy += time.perf_counter() - started
    shown_by_feed[frame.feed_id] += 1


renderer.show_frame = timed_show
start = time.monotonic()


def tick():
    global ui_busy
    started = time.perf_counter()
    pc._submit_replay_frames(int((time.monotonic() - start) * 1e9))
    ui_busy += time.perf_counter() - started


timer = QTimer()
timer.setTimerType(Qt.TimerType.PreciseTimer)
timer.setInterval(33)
timer.timeout.connect(tick)
timer.start()
QTimer.singleShot(int(seconds * 1000), app.quit)
began = time.monotonic()
app.exec()
wall = time.monotonic() - began
timer.stop()
stats = pc.replay_stats()
for worker in pc._replay_workers.values():
    worker.stop()
for burner in burners:
    burner.kill()
print(f"{wall:.1f} s, {busy} busy processes")
for feed_id, row in sorted(stats.items()):
    dropped = row["dropped_before_decode"] + row["dropped_before_show"]
    decode_ms = row["work_seconds"] / row["decoded"] * 1000 if row["decoded"] else 0.0
    print(
        f"  feed {feed_id}: shown {shown_by_feed[feed_id] / wall:4.1f}/s  dropped {dropped / wall:4.1f}/s  "
        f"decode+scale on worker {decode_ms:5.1f} ms"
    )
print(f"  UI thread busy with replay (ticks + showing frames): {ui_busy / wall * 100:4.1f}% of one core")
