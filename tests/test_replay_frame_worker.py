"""`ReplayFrameWorker`: newest request wins, results wake the UI thread once."""

from __future__ import annotations

import threading
import time
import unittest

from app.media.replay_frame_worker import ReplayFrameWorker


def _wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


class _Recorder:
    """A `work` function that records what it was asked for, optionally slowly."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.requests: list[object] = []
        self.threads: list[str] = []
        self.gate: threading.Event | None = None

    def __call__(self, request):
        self.requests.append(request)
        self.threads.append(threading.current_thread().name)
        if self.gate is not None:
            self.gate.wait(3)
        time.sleep(self.delay)
        return f"frame-{request}"


class ReplayFrameWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wakeups = 0
        self.worker: ReplayFrameWorker | None = None

    def tearDown(self) -> None:
        if self.worker is not None:
            self.worker.stop()

    def _make(self, work) -> ReplayFrameWorker:
        def on_ready() -> None:
            self.wakeups += 1

        self.worker = ReplayFrameWorker("test-worker", work, on_ready)
        return self.worker

    def test_a_request_is_worked_on_off_the_calling_thread_and_returned(self) -> None:
        work = _Recorder()
        worker = self._make(work)
        worker.submit(1, epoch=7)
        self.assertTrue(_wait_for(lambda: self.wakeups == 1))
        self.assertEqual(worker.take(), (7, "frame-1"))
        self.assertEqual(work.threads, ["test-worker"])
        self.assertNotEqual(work.threads[0], threading.current_thread().name)

    def test_submit_returns_without_waiting_for_the_work(self) -> None:
        work = _Recorder(delay=0.3)
        worker = self._make(work)
        started = time.perf_counter()
        worker.submit(1, epoch=0)
        self.assertLess(time.perf_counter() - started, 0.1)

    def test_a_newer_request_replaces_one_still_waiting(self) -> None:
        work = _Recorder()
        work.gate = threading.Event()
        worker = self._make(work)
        worker.submit(1, epoch=0)
        self.assertTrue(_wait_for(lambda: work.requests == [1]))  # 1 is being worked on
        worker.submit(2, epoch=0)
        worker.submit(3, epoch=0)  # replaces 2 before it started
        work.gate.set()
        self.assertTrue(_wait_for(lambda: len(work.requests) == 2 and worker.stats()["decoded"] == 2))
        self.assertEqual(work.requests, [1, 3])
        self.assertEqual(worker.stats()["dropped_before_decode"], 1)
        self.assertEqual(worker.stats()["submitted"], 3)

    def test_the_ui_thread_is_woken_once_for_a_burst_of_results(self) -> None:
        work = _Recorder()
        worker = self._make(work)
        for request in range(5):
            worker.submit(request, epoch=0)
            self.assertTrue(_wait_for(lambda r=request: worker.stats()["decoded"] > r or worker.stats()["dropped_before_decode"] > 0))
        self.assertTrue(_wait_for(lambda: worker.stats()["decoded"] + worker.stats()["dropped_before_decode"] >= 5))
        self.assertEqual(self.wakeups, 1)  # nobody called take() yet
        self.assertIsNotNone(worker.take())
        worker.submit(99, epoch=0)
        self.assertTrue(_wait_for(lambda: self.wakeups == 2))

    def test_take_returns_the_newest_result_and_counts_the_older_one_dropped(self) -> None:
        work = _Recorder()
        worker = self._make(work)
        worker.submit(1, epoch=0)
        self.assertTrue(_wait_for(lambda: worker.stats()["decoded"] == 1))
        worker.submit(2, epoch=0)
        self.assertTrue(_wait_for(lambda: worker.stats()["decoded"] == 2))
        self.assertEqual(worker.take(), (0, "frame-2"))
        self.assertIsNone(worker.take())
        stats = worker.stats()
        self.assertEqual(stats["dropped_before_show"], 1)
        self.assertEqual(stats["shown"], 1)

    def test_the_result_carries_the_epoch_it_was_requested_under(self) -> None:
        work = _Recorder()
        worker = self._make(work)
        worker.submit("a", epoch=3)
        self.assertTrue(_wait_for(lambda: self.wakeups == 1))
        self.assertEqual(worker.take()[0], 3)

    def test_cancel_forgets_waiting_and_finished_work(self) -> None:
        work = _Recorder()
        work.gate = threading.Event()
        worker = self._make(work)
        worker.submit(1, epoch=0)
        self.assertTrue(_wait_for(lambda: work.requests == [1]))
        worker.submit(2, epoch=0)  # waiting
        worker.cancel()
        work.gate.set()
        self.assertTrue(_wait_for(lambda: worker.stats()["decoded"] == 1))
        self.assertEqual(work.requests, [1])  # 2 never ran
        # The one already in flight finished, and its result is still there to
        # be recognised as stale by its epoch.
        self.assertEqual(worker.take(), (0, "frame-1"))

    def test_a_failing_request_is_counted_and_the_worker_carries_on(self) -> None:
        calls: list[int] = []

        def work(request):
            calls.append(request)
            if request == 1:
                raise RuntimeError("bad frame")
            return None if request == 2 else f"ok-{request}"

        worker = self._make(work)
        with self.assertLogs("app.media.replay_frame_worker", level="ERROR"):
            worker.submit(1, epoch=0)
            self.assertTrue(_wait_for(lambda: worker.stats()["failed"] == 1))
        worker.submit(2, epoch=0)  # decoder had nothing: None
        self.assertTrue(_wait_for(lambda: worker.stats()["failed"] == 2))
        worker.submit(3, epoch=0)
        self.assertTrue(_wait_for(lambda: self.wakeups == 1))
        self.assertEqual(worker.take(), (0, "ok-3"))
        self.assertEqual(self.wakeups, 1)  # failures don't wake the UI thread

    def test_stop_ends_the_thread_and_ignores_later_requests(self) -> None:
        work = _Recorder()
        worker = self._make(work)
        worker.submit(1, epoch=0)
        self.assertTrue(_wait_for(lambda: self.wakeups == 1))
        thread = worker._thread
        assert thread is not None
        worker.stop()
        self.assertFalse(thread.is_alive())
        worker.submit(2, epoch=0)
        time.sleep(0.05)
        self.assertEqual(work.requests, [1])

    def test_stop_waits_for_work_in_progress(self) -> None:
        work = _Recorder(delay=0.15)
        worker = self._make(work)
        worker.submit(1, epoch=0)
        self.assertTrue(_wait_for(lambda: work.requests == [1]))
        started = time.perf_counter()
        worker.stop()
        self.assertGreaterEqual(time.perf_counter() - started, 0.05)
        assert worker._thread is not None
        self.assertFalse(worker._thread.is_alive())

    def test_no_thread_is_started_until_the_first_request(self) -> None:
        worker = self._make(_Recorder())
        self.assertIsNone(worker._thread)
        worker.stop()  # fine with nothing started


if __name__ == "__main__":
    unittest.main()
