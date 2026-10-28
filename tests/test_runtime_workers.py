"""Spawn/lifecycle tests: deliberately use no application or model imports."""
import multiprocessing as mp
import os
import threading
import time
import unittest
from multiprocessing import shared_memory

from runtime_workers import (
    ManagedWorker, ManagedRecognizer, RemoteWorkerError, WorkerCancelled,
    WorkerClosed, WorkerCrashed, WorkerTimeout,
)


class LightweightService:
    actual_device = "test"

    def __init__(self, on_status=None, cancel_wait=None, close_count=None):
        self.status = on_status
        self.cancel_wait = cancel_wait
        self.close_count = close_count
        self.count = 0
        self.status("starting")

    def echo(self, value):
        self.count += 1
        self.status("working")
        return value, self.count, os.getpid()

    def fail(self):
        raise ValueError("controlled error")

    def crash(self):
        os._exit(17)

    def hang(self):
        time.sleep(60)

    def cooperative(self):
        self.cancel_wait(60)
        self.status("late status")
        return "late result"

    def large_result(self):
        return b"x" * (1024 * 1024 + 1)

    def audio_call(self, method, descriptor, args, kwargs):
        import numpy as np
        memory = shared_memory.SharedMemory(name=descriptor[0])
        if method == "hang_audio":
            self.status(memory.name)
            time.sleep(60)
        try:
            audio = np.ndarray(descriptor[1], dtype=descriptor[2], buffer=memory.buf)
            try:
                return float(audio.sum()), len(audio), args, kwargs, memory.name
            finally:
                del audio
        finally:
            memory.close()

    def close(self):
        if self.close_count is not None:
            with self.close_count.get_lock():
                self.close_count.value += 1


def lightweight_factory(on_status=None, cancel_wait=None, close_count=None):
    return LightweightService(on_status, cancel_wait, close_count)


def failed_factory(on_status=None, cancel_wait=None):
    raise LookupError("construction failed")


def hanging_factory(on_status=None, cancel_wait=None):
    on_status("constructing")
    time.sleep(60)


def die_after_event_publication(event, entered):
    event.set()
    # Reproduce the old Event's abandoned Condition lock deterministically.
    condition = getattr(event, "_cond", None)
    if condition is not None:
        condition.acquire()
    entered.set()
    os._exit(17)


def probe_event_after_owner_death(event):
    event.clear()
    event.set()
    if not event.wait(.05):
        raise RuntimeError("Published event was not observed")


@unittest.skipUnless(os.name == "nt", "Windows kernel event contract")
class CrashSafeEventTests(unittest.TestCase):
    def test_owner_death_cannot_abandon_native_event_notification(self):
        from runtime_workers import _WindowsEvent
        context = mp.get_context("spawn")
        for factory, completes in [(context.Event, False), (_WindowsEvent, True)]:
            with self.subTest(native=completes):
                event = factory()
                entered = context.Event()
                owner = context.Process(target=die_after_event_publication, args=(event, entered))
                owner.start()
                self.assertTrue(entered.wait(5))
                owner.join(5)
                self.assertEqual(owner.exitcode, 17)
                owner.close()
                probe = context.Process(target=probe_event_after_owner_death, args=(event,))
                probe.start();probe.join(1.5)
                finished = not probe.is_alive()
                if not finished:
                    probe.kill();probe.join(5)
                exitcode = probe.exitcode
                probe.close()
                close = getattr(event, "close", None)
                if close:close()
                self.assertEqual(finished, completes)
                if completes:self.assertEqual(exitcode, 0)

    def test_close_does_not_release_handle_during_local_wait(self):
        from runtime_workers import _WindowsEvent
        event = _WindowsEvent();outcome=[]
        thread = threading.Thread(target=lambda: outcome.append(event.wait()))
        thread.start()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if not event._local_lock.acquire(blocking=False):break
            event._local_lock.release();time.sleep(.001)
        event.close();thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome,[False])
        event.set();event.clear()
        self.assertFalse(event.wait(0))

    def test_explicit_close_releases_kernel_handles(self):
        import ctypes
        from ctypes import wintypes
        from runtime_workers import _WindowsEvent
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        def handles():
            count=wintypes.DWORD()
            self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(count)))
            return count.value
        before=handles();events=[_WindowsEvent() for _ in range(30)]
        self.assertGreaterEqual(handles(),before+30)
        for event in events:event.close()
        self.assertLessEqual(handles(),before+2)


class ManagedWorkerTests(unittest.TestCase):
    def make_worker(self, factory=lightweight_factory, **kwargs):
        kwargs.setdefault("graceful_timeout", .15)
        kwargs.setdefault("force_timeout", 1)
        worker = ManagedWorker(factory, **kwargs)
        self.addCleanup(worker.close)
        return worker

    def warm(self, worker):
        return worker.call("echo", "warm")

    def test_rpc_reuses_one_spawn_process_and_delivers_status(self):
        statuses = []
        worker = self.make_worker(on_status=statuses.append)
        first = worker.call("echo", "hello")
        second = worker.call("echo", "again")
        self.assertEqual(first[:2], ("hello", 1))
        self.assertEqual(second[:2], ("again", 2))
        self.assertEqual(first[2], worker.pid)
        self.assertEqual(first[2], second[2])
        self.assertNotEqual(first[2], os.getpid())
        self.assertEqual(statuses, ["starting", "working", "working"])
        self.assertEqual(worker.metadata["actual_device"], "test")
        self.assertEqual(worker._process._start_method, "spawn")

    def test_remote_exception_leaves_service_usable(self):
        worker = self.make_worker()
        with self.assertRaisesRegex(RemoteWorkerError, "ValueError: controlled error"):
            worker.call("fail")
        self.assertEqual(worker.call("echo", 3)[0], 3)

    def test_constructor_exception_is_reported(self):
        worker = self.make_worker(failed_factory)
        with self.assertRaisesRegex(RemoteWorkerError, "LookupError: construction failed"):
            worker.call("echo", 1)

    def test_cancel_native_hang_is_bounded_and_reaped(self):
        cancel = threading.Event()
        worker = self.make_worker(cancel_wait=cancel.wait)
        self.warm(worker)
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(WorkerCancelled):
                worker.call("hang")
        finally:
            timer.cancel()
            timer.join()
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(worker.alive)
        self.assertTrue(worker.closed)

    def test_cancel_during_factory_construction(self):
        cancel = threading.Event()
        worker = self.make_worker(hanging_factory, cancel_wait=cancel.wait,
                                 on_status=lambda _: cancel.set())
        started = time.monotonic()
        with self.assertRaises(WorkerCancelled):
            worker.call("echo", 1)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(worker.alive)

    def test_zero_argument_cancel_callback(self):
        worker = self.make_worker(cancel_wait=lambda: True)
        with self.assertRaises(WorkerCancelled):
            worker.call("echo", 1)
        self.assertFalse(worker.alive)

    def test_timeout_terminates_hung_process(self):
        worker = self.make_worker()
        self.warm(worker)
        with self.assertRaises(WorkerTimeout):
            worker.call("hang", rpc_timeout=.1)
        self.assertFalse(worker.alive)

    def test_crash_reports_exitcode_and_reaps(self):
        worker = self.make_worker()
        self.warm(worker)
        with self.assertRaisesRegex(WorkerCrashed, "exitcode=17"):
            worker.call("crash")
        self.assertFalse(worker.alive)
        self.assertEqual(worker.exitcode, 17)

    def test_close_idempotent_runs_service_close_once(self):
        count = mp.get_context("spawn").Value("i", 0)
        worker = self.make_worker(factory_kwargs={"close_count": count})
        self.warm(worker)
        worker.close()
        worker.close()
        self.assertEqual(count.value, 1)
        with self.assertRaises(WorkerClosed):
            worker.call("echo", 1)
        self.assertFalse(worker.alive)

    def test_concurrent_close_suppresses_late_status_and_result(self):
        statuses, outcome = [], []
        worker = self.make_worker(on_status=statuses.append)
        self.warm(worker)
        statuses.clear()

        def invoke():
            try:
                outcome.append(worker.call("cooperative"))
            except WorkerClosed:
                outcome.append("closed")

        thread = threading.Thread(target=invoke)
        thread.start()
        time.sleep(.1)
        worker.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["closed"])
        self.assertEqual(statuses, [])
        self.assertFalse(worker.alive)

    def test_requests_are_serialized(self):
        worker = self.make_worker()
        self.warm(worker)
        results = []
        threads = [threading.Thread(target=lambda: results.append(worker.call("echo", 1)))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(result[1] for result in results), [2, 3, 4, 5])

    def test_oversized_request_rejected_without_poisoning_worker(self):
        worker = self.make_worker()
        with self.assertRaisesRegex(ValueError, "RPC message exceeds"):
            worker.call("echo", b"x" * (256 * 1024))
        self.assertEqual(worker.call("echo", "ok")[0], "ok")

    def test_oversized_response_reports_error_without_poisoning_worker(self):
        worker = self.make_worker()
        with self.assertRaisesRegex(RemoteWorkerError, "RPC message exceeds"):
            worker.call("large_result")
        self.assertEqual(worker.call("echo", "ok")[0], "ok")

    def test_audio_uses_shared_memory_and_unlinks_after_result(self):
        import numpy as np
        worker = self.make_worker()
        proxy = object.__new__(ManagedRecognizer)
        proxy._worker = worker
        total, size, args, kwargs, name = proxy.transcribe(np.arange(4096), "en", beam_size=2)
        self.assertEqual(total, float(np.arange(4096).sum()))
        self.assertEqual(size, 4096)
        self.assertEqual(args, ("en",))
        self.assertEqual(kwargs, {"beam_size": 2})
        with self.assertRaises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)

    def test_cancelled_audio_unlinks_shared_memory(self):
        import numpy as np
        cancel, names = threading.Event(), []

        def on_status(message):
            if message != "starting":
                names.append(message)
                cancel.set()

        worker = self.make_worker(on_status=on_status, cancel_wait=cancel.wait)
        proxy = object.__new__(ManagedRecognizer)
        proxy._worker = worker
        with self.assertRaises(WorkerCancelled):
            proxy._audio_call("hang_audio", np.arange(160000))
        self.assertEqual(len(names), 1)
        self.assertFalse(worker.alive)
        with self.assertRaises(FileNotFoundError):
            shared_memory.SharedMemory(name=names[0])

    def test_repeated_spawn_close_leaves_no_active_children(self):
        baseline = {child.pid for child in mp.active_children()}
        for _ in range(3):
            worker = self.make_worker()
            self.warm(worker)
            worker.close()
        self.assertEqual({child.pid for child in mp.active_children()}, baseline)


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main()
