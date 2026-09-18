"""组件下载器的本地HTTP、断点和压缩包安全测试。"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile

from download_manager import ComponentFile, ComponentSpec, DownloadManager


class _RangeHandler(BaseHTTPRequestHandler):
    payload = b""
    ranges: list[str] = []
    delay = 0.002

    def do_GET(self):
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.end_headers()
            return
        requested = self.headers.get("Range", "")
        type(self).ranges.append(requested)
        start = 0
        if requested.startswith("bytes=") and requested.endswith("-"):
            try:
                start = int(requested[6:-1])
            except ValueError:
                start = 0
        if start >= len(self.payload):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(self.payload)}")
            self.end_headers()
            return
        body = self.payload[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
        self.end_headers()
        for offset in range(0, len(body), 32 * 1024):
            try:
                self.wfile.write(body[offset:offset + 32 * 1024])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError):
                return
            time.sleep(self.delay)

    def log_message(self, _format, *_args):
        return


class DownloadManagerTests(unittest.TestCase):
    def setUp(self):
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.payload = bytes((index * 17) % 251 for index in range(2 * 1024 * 1024))
        _RangeHandler.payload = self.payload
        _RangeHandler.ranges = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/payload.bin"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _spec(self, target: Path | None = None) -> ComponentSpec:
        return ComponentSpec(
            key="test.payload",
            version="1",
            target_dir=target or self.root / "install",
            files=(
                ComponentFile(
                    path="payload.bin",
                    url=self.url,
                    size=len(self.payload),
                    sha256=hashlib.sha256(self.payload).hexdigest(),
                ),
            ),
        )

    def _wait_state(self, manager: DownloadManager, key: str, states: set[str], timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = manager.status(key)
            if status and status.state in states:
                return status
            time.sleep(0.02)
        self.fail(f"在限定时间内没有等到状态{states}，最后状态={manager.status(key)}")

    def test_pause_resume_uses_http_range_and_writes_verified_component(self):
        manager = DownloadManager(self.root / "cache", chunk_size=32 * 1024)
        finished = threading.Event()
        manager.start(self._spec(), on_finished=lambda *_args: finished.set())
        self._wait_state(manager, "test.payload", {"下载中"})
        time.sleep(0.04)
        self.assertTrue(manager.pause("test.payload"))
        paused = self._wait_state(manager, "test.payload", {"已暂停"})
        partial = self.root / "install" / "payload.bin.part"
        self.assertTrue(partial.is_file())
        self.assertGreater(partial.stat().st_size, 0)
        self.assertLess(partial.stat().st_size, len(self.payload))
        self.assertTrue(paused.resumable)
        self.assertTrue(manager.resume("test.payload"))
        self.assertTrue(finished.wait(10))
        self.assertEqual(manager.status("test.payload").state, "完成")
        result = self.root / "install" / "payload.bin"
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertTrue((self.root / "install" / ".audio-subtitle-overlay-component.json").is_file())
        self.assertTrue(any(value.startswith("bytes=") for value in _RangeHandler.ranges[1:]))

    def test_cancel_keeps_partial_and_next_start_continues(self):
        manager = DownloadManager(self.root / "cache", chunk_size=32 * 1024)
        manager.start(self._spec())
        self._wait_state(manager, "test.payload", {"下载中"})
        time.sleep(0.04)
        self.assertTrue(manager.cancel("test.payload"))
        self._wait_state(manager, "test.payload", {"已取消"})
        partial = self.root / "install" / "payload.bin.part"
        self.assertTrue(partial.is_file())
        self.assertFalse((self.root / "install" / "payload.bin").exists())
        finished = threading.Event()
        manager.start(self._spec(), on_finished=lambda *_args: finished.set())
        self.assertTrue(finished.wait(10))
        self.assertEqual((self.root / "install" / "payload.bin").read_bytes(), self.payload)
        self.assertTrue(any(value.startswith("bytes=") for value in _RangeHandler.ranges[1:]))

    def test_archive_path_traversal_is_rejected(self):
        archive = self.root / "unsafe.whl"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../outside.txt", "不应写出目标目录")
        manager = DownloadManager(self.root / "cache")
        spec = ComponentSpec(
            key="test.archive",
            version="1",
            target_dir=self.root / "archive-install",
            files=(
                ComponentFile(
                    path="unsafe.whl",
                    url=archive.as_uri(),
                    size=archive.stat().st_size,
                    sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                    archive=True,
                ),
            ),
        )
        finished = threading.Event()
        manager.start(spec, on_finished=lambda *_args: finished.set())
        self.assertTrue(finished.wait(10))
        self.assertEqual(manager.status("test.archive").state, "失败")
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertFalse((self.root / "archive-install" / ".audio-subtitle-overlay-component.json").exists())

    def test_archive_install_records_extracted_files(self):
        archive = self.root / "safe.whl"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("nvidia/cublas/bin/cublas64_12.dll", b"dll")
        manager = DownloadManager(self.root / "cache")
        spec = ComponentSpec(
            key="test.safe-archive",
            version="1",
            target_dir=self.root / "safe-install",
            files=(
                ComponentFile(
                    path="safe.whl",
                    url=archive.as_uri(),
                    size=archive.stat().st_size,
                    sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                    archive=True,
                ),
            ),
        )
        manager.ensure(spec)
        extracted = self.root / "safe-install" / "nvidia" / "cublas" / "bin" / "cublas64_12.dll"
        self.assertTrue(extracted.is_file())
        self.assertTrue(manager.component_is_ready(spec))
        extracted.unlink()
        self.assertFalse(manager.component_is_ready(spec))

    def test_alternate_url_is_used_after_primary_http_failure(self):
        primary = f"http://127.0.0.1:{self.server.server_port}/missing/payload.bin"
        manager = DownloadManager(self.root / "cache", chunk_size=32 * 1024, max_retries=1)
        spec = ComponentSpec(
            key="test.alternate",
            version="1",
            target_dir=self.root / "alternate-install",
            files=(
                ComponentFile(
                    path="payload.bin",
                    url=primary,
                    urls=(primary, self.url),
                    size=len(self.payload),
                    sha256=hashlib.sha256(self.payload).hexdigest(),
                ),
            ),
        )
        finished = threading.Event()
        manager.start(spec, on_finished=lambda *_args: finished.set())
        self.assertTrue(finished.wait(10))
        self.assertEqual(manager.status("test.alternate").state, "完成")
        self.assertEqual((self.root / "alternate-install" / "payload.bin").read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
