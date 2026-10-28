"""Bounded, cancellable spawn workers for local recognition and translation.

Only one RPC per service is in flight. Fixed shared slots carry control/results;
large audio lives in separately owned shared memory, never in a pipe feeder.
No source audio, transcript or translation is logged by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import multiprocessing as mp
import os
import math
import uuid
from multiprocessing import shared_memory
import pickle
import threading
import time
from types import SimpleNamespace


class WorkerError(RuntimeError):
    """A managed service could not complete its request."""


class WorkerClosed(WorkerError):
    pass


class WorkerCancelled(WorkerError):
    pass


class WorkerTimeout(WorkerError):
    pass


class WorkerCrashed(WorkerError):
    pass


class RemoteWorkerError(WorkerError):
    def __init__(self, kind, message):
        self.remote_type = kind
        super().__init__(f"{kind}: {message}")


class _WindowsEvent:
    """Manual-reset kernel event: killing a waiter cannot strand a shared lock.

    Spawn transfers only the unguessable local name. Each process owns its
    handle; the local lock prevents CloseHandle racing a local Win32 wait.
    """
    def __init__(self):
        self.name = "Local\\Shengmu-" + uuid.uuid4().hex
        self._bind()
        self._handle = self._api.CreateEventW(None, True, False, self.name)
        if not self._handle:
            raise self._ctypes.WinError()
        if self._ctypes.get_last_error() == 183:
            self.close()
            raise self._ctypes.WinError(183)

    def _bind(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self._api.CreateEventW.restype = wintypes.HANDLE
        self._api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self._api.OpenEventW.restype = wintypes.HANDLE
        for name in ("SetEvent", "ResetEvent", "CloseHandle"):
            method = getattr(self._api, name)
            method.argtypes = [wintypes.HANDLE]
            method.restype = wintypes.BOOL
        self._api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._api.WaitForSingleObject.restype = wintypes.DWORD
        self._local_lock = threading.Lock()
        self._handle = None

    def __getstate__(self):
        return self.name

    def __setstate__(self, name):
        self.name = name
        self._bind()
        self._handle = self._api.OpenEventW(0x100002, False, name)
        if not self._handle:
            raise self._ctypes.WinError()

    def _change(self, method):
        with self._local_lock:
            if self._handle and not method(self._handle):
                raise self._ctypes.WinError()

    def set(self):
        self._change(self._api.SetEvent)

    def clear(self):
        self._change(self._api.ResetEvent)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            remaining = .05 if deadline is None else max(0.0, deadline - time.monotonic())
            milliseconds = min(50, math.ceil(remaining * 1000))
            with self._local_lock:
                if not self._handle:
                    return False
                result = self._api.WaitForSingleObject(self._handle, milliseconds)
                if result == 0xffffffff:
                    raise self._ctypes.WinError()
                if result == 0:
                    return True
                if result != 0x102:
                    raise WorkerError(f"Unexpected event wait result: {result}")
            if deadline is not None and time.monotonic() >= deadline:
                return False
            # Even an unbounded wait must let local set/close acquire the lock.
            time.sleep(0)

    def is_set(self):
        return self.wait(0)

    def close(self):
        with self._local_lock:
            if self._handle:
                if not self._api.CloseHandle(self._handle):
                    raise self._ctypes.WinError()
                self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _event(ctx):
    return _WindowsEvent() if os.name == "nt" else ctx.Event()


@dataclass
class _Slot:
    data: object
    size: object
    ready: object
    consumed: object


def _slot(ctx, capacity):
    slot = _Slot(ctx.RawArray("B", capacity), ctx.RawValue("I", 0), _event(ctx), _event(ctx))
    slot.consumed.set()
    return slot


def _write(slot, message):
    payload = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(payload) > len(slot.data):
        raise ValueError(f"RPC message exceeds {len(slot.data)} byte limit")
    memoryview(slot.data).cast("B")[:len(payload)] = payload
    slot.size.value = len(payload)
    slot.consumed.clear()
    slot.ready.set()


def _read(slot):
    # Copy before acknowledging: the sender may immediately reuse this slot.
    payload = bytes(memoryview(slot.data).cast("B")[:slot.size.value])
    slot.ready.clear()
    slot.consumed.set()
    return pickle.loads(payload)


def _metadata(service):
    return {key: getattr(service, key) for key in
            ("capabilities", "actual_device", "actual_compute_type", "loaded")
            if hasattr(service, key)}


def _worker_main(factory, args, kwargs, request, response, stop):
    service = None
    request_id = 0

    def send(kind, payload, metadata=None):
        while not stop.is_set():
            if response.consumed.wait(.05):
                if stop.is_set():
                    return False
                _write(response, (request_id, kind, payload, metadata or {}))
                return True
        return False

    def status(message):
        # Backpressure is bounded to one status, and stop always interrupts it.
        send("status", str(message)[:8192])

    try:
        service = factory(*args, on_status=status, cancel_wait=stop.wait, **kwargs)
        if not send("ready", None, _metadata(service)):
            return
        while not stop.is_set():
            if not request.ready.wait(.05):
                continue
            request_id, method, args, kwargs = _read(request)
            try:
                result = getattr(service, method)(*args, **kwargs)
                # Some recognizers return self from load; native models cannot
                # cross a process boundary, and callers only need metadata.
                if result is service:
                    result = None
                send("result", result, _metadata(service))
            except Exception as error:
                send("error", (type(error).__name__, str(error)))
    except BaseException as error:
        # os._exit/native crashes instead surface via the process exit code.
        try:
            send("error", (type(error).__name__, str(error)))
        except Exception:
            pass
    finally:
        if service is not None:
            close = getattr(service, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass


class ManagedWorker:
    """One spawn process and a bounded, serial RPC channel.

    factory must be a spawn-importable top-level callable accepting on_status
    and cancel_wait keyword arguments. call() runs on the caller's background
    thread; close() can interrupt it from another thread. After cancellation,
    timeout or process failure this instance is closed and must be replaced.
    """
    def __init__(self, factory, *factory_args, on_status=None, cancel_wait=None,
                 rpc_timeout=300.0, graceful_timeout=5.0, force_timeout=2.0,
                 factory_kwargs=None, name="subtitle-service"):
        self._ctx = mp.get_context("spawn")
        self._request = _slot(self._ctx, 256 * 1024)
        self._response = _slot(self._ctx, 1024 * 1024)
        self._stop = _event(self._ctx)
        self._closed = threading.Event()
        self._call_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._delivery_lock = threading.RLock()
        self._on_status = on_status
        self.cancel_wait = cancel_wait
        self.rpc_timeout = rpc_timeout
        self._graceful_timeout = max(0.0, graceful_timeout)
        self._force_timeout = max(0.0, force_timeout)
        self._request_id = 0
        self.metadata = {}
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(factory, factory_args, factory_kwargs or {}, self._request,
                  self._response, self._stop), name=name, daemon=True)
        self._pid = None
        self._exitcode = None
        try:
            self._process.start()
            self._pid = self._process.pid
        except BaseException:
            self._closed.set()
            self._stop.set()
            self._process.close()
            self._close_events()
            raise

    @property
    def pid(self):
        return self._pid

    @property
    def closed(self):
        return self._closed.is_set()

    @property
    def alive(self):
        with self._close_lock:
            return self._process is not None and self._process.is_alive()

    @property
    def exitcode(self):
        return self._exitcode

    def _check(self, deadline):
        if self.closed:
            raise WorkerClosed("Worker is closed")
        if self.cancel_wait is not None:
            try:
                cancelled = self.cancel_wait(0)
            except TypeError:
                # Existing pipeline preparation callbacks accept no arguments.
                cancelled = self.cancel_wait()
            if cancelled:
                self.close()
                raise WorkerCancelled("Worker request cancelled")
        if time.monotonic() >= deadline:
            self.close()
            raise WorkerTimeout("Worker request timed out")

    def call(self, method, *args, rpc_timeout=None, **kwargs):
        timeout = self.rpc_timeout if rpc_timeout is None else rpc_timeout
        if timeout <= 0:
            raise ValueError("rpc_timeout must be positive")
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            while not acquired:
                self._check(deadline)
                acquired = self._call_lock.acquire(timeout=.05)
            self._check(deadline)
            self._request_id += 1
            request_id = self._request_id
            _write(self._request, (request_id, method, args, kwargs))
            while True:
                self._check(deadline)
                if self._response.ready.wait(.05):
                    response_id, kind, value, metadata = _read(self._response)
                    with self._delivery_lock:
                        self._check(deadline)
                        self.metadata.update(metadata)
                        if kind == "status":
                            if self._on_status is not None:
                                self._on_status(value)
                            continue
                        if kind == "error" and response_id in (0, request_id):
                            raise RemoteWorkerError(*value)
                        if kind == "result" and response_id == request_id:
                            return value
                # Read any fully published reply before reporting an exit.
                with self._close_lock:
                    crashed = self._process is not None and not self._process.is_alive()
                    code = self._process.exitcode if crashed else None
                if crashed and not self._response.ready.is_set():
                    self.close()
                    raise WorkerCrashed(f"Worker exited unexpectedly (exitcode={code})")
        finally:
            if acquired:
                self._call_lock.release()

    def _close_events(self):
        for event in (self._stop, self._request.ready, self._request.consumed,
                      self._response.ready, self._response.consumed):
            close = getattr(event, "close", None)
            if close is not None:
                close()

    def close(self):
        # Mark closed before waiting so in-flight calls cannot deliver statuses
        # or results, even when a native operation finishes during shutdown.
        with self._delivery_lock:
            self._closed.set()
            self._stop.set()
        with self._close_lock:
            process = self._process
            if process is None:
                return
            process.join(self._graceful_timeout)
            if process.is_alive():
                process.terminate()
                process.join(self._force_timeout / 2)
            if process.is_alive():
                process.kill()
                process.join(self._force_timeout / 2)
            if process.is_alive():
                raise WorkerError(f"Worker process {self._pid} did not exit after kill")
            self._exitcode = process.exitcode
            process.close()
            self._process = None
            self._close_events()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def create_local_recognizer(options, on_status=None, *, cancel_wait=None):
    """Lazy local factory shared by worker and optional in-process tooling."""
    from pathlib import Path
    from model_manager import find_local_model, ensure_model_available, validate_model_language
    if isinstance(options, dict):
        options = SimpleNamespace(**options)
    on_status = on_status or (lambda _: None)
    validate_model_language(options.model_size, options.source_language)
    if options.model_size != "sensevoice-int8" and not options.model_size.startswith("zipformer-"):
        from transcription import WhisperRecognizer
        recognizer = WhisperRecognizer(options.model_size, options.device_mode,
                                       options.model_path, on_status=on_status)
        recognizer.cancel_wait = cancel_wait
        return recognizer
    from asr_backends import SenseVoiceRecognizer, ZipformerRecognizer
    path = Path(options.model_path) if options.model_path else find_local_model(options.model_size)
    if path is None:
        on_status(f"正在准备 {options.model_size} 本地模型……")
        path = ensure_model_available(options.model_size, on_progress=lambda p: on_status(
            f"{options.model_size}：{p.state}，已下载 {p.completed / 1024**2:.1f} MiB"),
            **({"cancel_wait": cancel_wait} if cancel_wait is not None else {}))
    cls = SenseVoiceRecognizer if options.model_size == "sensevoice-int8" else ZipformerRecognizer
    return cls(str(path), language=options.source_language, cpu_threads=2)


class _RecognizerService:
    def __init__(self, options, on_status=None, cancel_wait=None):
        self.recognizer = create_local_recognizer(options, on_status, cancel_wait=cancel_wait)

    def __getattr__(self, key):
        return getattr(self.recognizer, key)

    def audio_call(self, method, descriptor, args, kwargs):
        import numpy as np
        name, shape, dtype = descriptor
        memory = shared_memory.SharedMemory(name=name)
        try:
            audio = np.ndarray(shape, dtype=np.dtype(dtype), buffer=memory.buf)
            try:
                return getattr(self.recognizer, method)(audio, *args, **kwargs)
            finally:
                del audio
        finally:
            memory.close()

    def load(self):
        self.recognizer.load()

    def reset(self):
        reset = getattr(self.recognizer, "reset", None)
        if reset is not None:
            reset()

    def close(self):
        self.recognizer.close()


class ManagedRecognizer:
    def __init__(self, options, on_status=None, cancel_wait=None):
        options = dict(options) if isinstance(options, dict) else vars(options).copy()
        self.model_size = options["model_size"]
        self.device_mode = options["device_mode"]
        self.model_path = options["model_path"]
        self._worker = ManagedWorker(_RecognizerService, options, on_status=on_status,
                                     cancel_wait=cancel_wait, rpc_timeout=600,
                                     name="subtitle-recognition")

    @property
    def actual_device(self):
        return self._worker.metadata.get("actual_device", "")

    @property
    def actual_compute_type(self):
        return self._worker.metadata.get("actual_compute_type", "")

    @property
    def capabilities(self):
        return self._worker.metadata.get("capabilities")

    @property
    def loaded(self):
        return not self._worker.closed and self._worker.metadata.get("loaded", False)

    def load(self):
        self._worker.call("load")
        # sherpa recognizers expose no loaded property.
        self._worker.metadata["loaded"] = True
        return self

    def _audio_call(self, method, frames, *args, **kwargs):
        import numpy as np
        audio = np.ascontiguousarray(frames, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError("Expected one-dimensional mono audio")
        if audio.nbytes > 64 * 1024 * 1024:
            raise ValueError("Audio request exceeds 64 MiB limit")
        memory = shared_memory.SharedMemory(create=True, size=max(1, audio.nbytes))
        try:
            target = np.ndarray(audio.shape, dtype=audio.dtype, buffer=memory.buf)
            target[:] = audio
            del target
            return self._worker.call("audio_call", method,
                                     (memory.name, audio.shape, audio.dtype.str), args, kwargs)
        finally:
            memory.close()
            memory.unlink()

    def transcribe(self, audio, source_language=None, **kwargs):
        return self._audio_call("transcribe", audio, source_language, **kwargs)

    def accept_audio(self, frames, final=False):
        return self._audio_call("accept_audio", frames, final=final)

    def reset(self):
        return self._worker.call("reset")

    def set_cancel_wait(self, callback):
        self._worker.cancel_wait = callback

    def close(self):
        self._worker.close()


def _translator_factory(backend, on_status=None, cancel_wait=None):
    from translation import TranslationService
    return TranslationService(backend, on_status=on_status)


class ManagedTranslator:
    def __init__(self, backend, on_status=None, cancel_wait=None):
        self.backend = backend
        self._worker = ManagedWorker(_translator_factory, backend, on_status=on_status,
                                     cancel_wait=cancel_wait, rpc_timeout=300,
                                     name="subtitle-translation")

    def translate(self, text, source_language, target_language):
        return self._worker.call("translate", text, source_language, target_language)

    def set_cancel_wait(self, callback):
        self._worker.cancel_wait = callback

    def close(self):
        self._worker.close()
