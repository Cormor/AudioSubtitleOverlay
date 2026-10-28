"""真实本地 HTTP 下的下载队列、等待取消与同 key 复用验证。"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from download_manager import (
    ComponentFile,
    ComponentSpec,
    DownloadManager,
    DownloadWaitCancelled,
)


FILE_BYTES = bytes(range(256)) * 4
WAIT_TIMEOUT = 8.0


class _Gate:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()


class _HTTPFixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str | None]] = []
        self.gates: dict[str, _Gate] = {}


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        range_header = self.headers.get("Range")
        fixture: _HTTPFixture = self.server.fixture
        with fixture.lock:
            fixture.requests.append((path, range_header))
            gate = fixture.gates.get(path)

        start = 0
        if range_header:
            try:
                start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
            except ValueError:
                self.send_error(400)
                return
            if start >= len(FILE_BYTES):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(FILE_BYTES)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        body = FILE_BYTES[start:]
        self.send_response(206 if range_header else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{len(FILE_BYTES) - 1}/{len(FILE_BYTES)}")
        self.end_headers()

        chunk_size = 64
        for offset in range(0, len(body), chunk_size):
            self.wfile.write(body[offset:offset + chunk_size])
            self.wfile.flush()
            if offset == 0 and gate is not None:
                gate.started.set()
                if not gate.release.wait(WAIT_TIMEOUT):
                    return


class DownloadQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = _HTTPFixture()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.server.fixture = self.fixture
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.manager = DownloadManager(cache_dir=str(self.root / "cache"), max_concurrent=1)

    def tearDown(self):
        with self.fixture.lock:
            gates = tuple(self.fixture.gates.values())
        for gate in gates:
            gate.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=WAIT_TIMEOUT)
        self.temp.cleanup()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{name}"

    def spec(self, key: str, name: str | None = None) -> ComponentSpec:
        name = name or key
        return ComponentSpec(
            key=key,
            version="test-v1",
            target_dir=self.root / "components" / key,
            files=(ComponentFile(
                path="model.bin",
                url=self.url(name),
                size=len(FILE_BYTES),
                sha256=hashlib.sha256(FILE_BYTES).hexdigest(),
            ),),
        )

    def gate(self, name: str) -> _Gate:
        gate = _Gate()
        with self.fixture.lock:
            self.fixture.gates[f"/{name}"] = gate
        return gate

    def request_paths(self) -> list[str]:
        with self.fixture.lock:
            return [path for path, _range in self.fixture.requests]

    def wait_for(self, predicate, message: str, timeout: float = WAIT_TIMEOUT):
        deadline = time.monotonic() + timeout
        wake = threading.Event()
        while True:
            if predicate():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(message)
            wake.wait(min(0.01, remaining))

    @staticmethod
    def state_of(status):
        state = status.get("state") if isinstance(status, dict) else getattr(status, "state", status)
        return state.value if isinstance(state, Enum) else state

    @staticmethod
    def collect_strings(value):
        found = []
        if isinstance(value, Enum):
            found.append(str(value.value))
        elif isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                found.extend(DownloadQueueTests.collect_strings(key))
                found.extend(DownloadQueueTests.collect_strings(child))
        elif isinstance(value, (tuple, list, set)):
            for child in value:
                found.extend(DownloadQueueTests.collect_strings(child))
        else:
            state = getattr(value, "state", None)
            if state is not None:
                found.extend(DownloadQueueTests.collect_strings(state))
        return found

    def test_max_concurrent_one_runs_fifo_and_reports_queued_callback_state(self):
        gate_a = self.gate("a")
        callback_states = []
        callback_lock = threading.Lock()

        def capture(*args, **kwargs):
            states = self.collect_strings((args, kwargs))
            with callback_lock:
                callback_states.extend(states)

        spec_a = self.spec("A", "a")
        spec_b = self.spec("B", "b")
        self.manager.start(spec_a)
        self.assertTrue(gate_a.started.wait(WAIT_TIMEOUT), "A 未进入本地 HTTP 服务")
        self.manager.start(spec_b, on_progress=capture)

        self.wait_for(lambda: self.state_of(self.manager.status("B")) == "排队", "B 未进入排队状态")
        self.wait_for(
            lambda: self.callback_has_queued_state(callback_states, callback_lock),
            "B 的进度 callback 未报告排队状态",
        )

        gate_a.release.set()
        self.wait_for(lambda: self.state_of(self.manager.status("A")) == "完成", "A 未完成")
        self.wait_for(lambda: self.state_of(self.manager.status("B")) == "完成", "B 未完成")
        self.assertEqual(self.request_paths(), ["/a", "/b"])

    def test_cancelled_ensure_wait_keeps_download_running_and_can_be_reused(self):
        gate = self.gate("cancel")
        cancel = threading.Event()
        result = {}
        started_call = threading.Event()

        def wait_for_component():
            started_call.set()
            try:
                result["value"] = self.manager.ensure(
                    self.spec("cancel"), cancel_wait=cancel.is_set
                )
            except BaseException as exc:
                result["error"] = exc

        waiter = threading.Thread(target=wait_for_component)
        waiter.start()
        self.assertTrue(started_call.wait(WAIT_TIMEOUT), "ensure 等待线程未启动")
        self.assertTrue(gate.started.wait(WAIT_TIMEOUT), "下载未开始")
        cancel_at = time.monotonic()
        cancel.set()
        waiter.join(timeout=0.5)
        self.assertFalse(waiter.is_alive(), "cancel_wait 后超过 0.5 秒仍未退出")
        self.assertLessEqual(time.monotonic() - cancel_at, 0.5)
        self.assertIsInstance(result.get("error"), DownloadWaitCancelled)
        self.assertTrue(self.manager.is_active("cancel"), "取消等待不应取消实际下载")

        gate.release.set()
        self.wait_for(lambda: self.state_of(self.manager.status("cancel")) == "完成", "下载未继续完成")
        self.manager.ensure(self.spec("cancel"))
        self.assertEqual(self.request_paths(), ["/cancel"])

    def test_two_ensure_waiters_for_same_key_share_one_http_download(self):
        gate = self.gate("shared")
        barrier = threading.Barrier(3)
        outcomes = []
        outcomes_lock = threading.Lock()
        spec = self.spec("shared")

        def ensure_worker():
            barrier.wait(timeout=WAIT_TIMEOUT)
            try:
                value = self.manager.ensure(spec)
                outcome = ("ok", value)
            except BaseException as exc:
                outcome = ("error", exc)
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=ensure_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=WAIT_TIMEOUT)
        self.assertTrue(gate.started.wait(WAIT_TIMEOUT), "共享下载未开始")
        gate.release.set()
        for thread in threads:
            thread.join(timeout=WAIT_TIMEOUT)
            self.assertFalse(thread.is_alive(), "ensure 等待线程未结束")

        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(kind == "ok" for kind, _value in outcomes), outcomes)
        self.assertEqual(self.request_paths(), ["/shared"])

    def test_factory_failure_does_not_poison_later_download(self):
        gate_b = self.gate("good")
        factory_called = threading.Event()
        allow_factory_failure = threading.Event()
        outcomes = {}

        def broken_factory():
            factory_called.set()
            if not allow_factory_failure.wait(WAIT_TIMEOUT):
                raise TimeoutError("test did not release factory gate")
            raise RuntimeError("factory failed")

        def call_broken_factory():
            try:
                self.manager.ensure_factory("factory-A", broken_factory)
            except BaseException as exc:
                outcomes["A"] = exc

        thread_a = threading.Thread(target=call_broken_factory)
        thread_a.start()
        self.assertTrue(factory_called.wait(WAIT_TIMEOUT), "A 工厂未启动")

        thread_b = threading.Thread(
            target=lambda: outcomes.setdefault(
                "B", self.manager.ensure_factory("factory-B", lambda: self.spec("factory-B", "good"))
            )
        )
        thread_b.start()
        self.wait_for(
            lambda: self.state_of(self.manager.status("factory-B")) == "排队",
            "A 工厂等待期间 B 未进入队列",
        )
        allow_factory_failure.set()
        thread_a.join(timeout=WAIT_TIMEOUT)
        self.assertFalse(thread_a.is_alive(), "A 工厂失败没有返回")
        self.assertIsInstance(outcomes.get("A"), Exception)

        self.assertTrue(gate_b.started.wait(WAIT_TIMEOUT), "A 工厂失败后 B 未开始下载")
        gate_b.release.set()
        thread_b.join(timeout=WAIT_TIMEOUT)
        self.assertFalse(thread_b.is_alive(), "A 工厂失败后 B 未完成")
        self.assertEqual(self.state_of(self.manager.status("factory-B")), "完成")
        self.assertEqual(self.request_paths(), ["/good"])

    @staticmethod
    def callback_has_queued_state(callback_states, callback_lock):
        with callback_lock:
            return "排队" in callback_states


if __name__ == "__main__":
    unittest.main()
