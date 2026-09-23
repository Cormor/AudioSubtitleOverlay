"""Windows 进程音频回环采集与活动应用选择。"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Callable

import numpy as np


PROCESS_AUDIO_DEVICE = "Beta：自动跟踪有声音的应用"

_AUDIBLE_PEAK = 0.000001
_SESSION_SCAN_INTERVAL = 0.25
_SESSION_HOLD_SECONDS = 1.5
_ACTIVATION_TIMEOUT_SECONDS = 8.0
_PROCESS_RETRY_SECONDS = 5.0
_activation_callbacks_lock = threading.Lock()
_pending_activation_callbacks: set[object] = set()


@dataclass(frozen=True)
class AudioProcessCandidate:
    """一个处于活动状态的播放进程及其最近音量峰值。"""

    process_id: int
    name: str
    peak: float | None


class _WaveFormatEx(ctypes.Structure):
    """WASAPI 输入格式。"""

    _fields_ = [
        ("format_tag", ctypes.c_ushort),
        ("channels", ctypes.c_ushort),
        ("samples_per_sec", ctypes.c_uint32),
        ("avg_bytes_per_sec", ctypes.c_uint32),
        ("block_align", ctypes.c_ushort),
        ("bits_per_sample", ctypes.c_ushort),
        ("extra_size", ctypes.c_ushort),
    ]


class _Guid(ctypes.Structure):
    """Windows GUID 结构。"""

    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> "_Guid":
        parsed = uuid.UUID(value)
        tail = (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:])
        return cls(parsed.time_low, parsed.time_mid, parsed.time_hi_version, tail)


class _AudioClientActivationParams(ctypes.Structure):
    """指定进程树回环的 Windows 激活参数。"""

    _fields_ = [
        ("activation_type", ctypes.c_uint32),
        ("target_process_id", ctypes.c_uint32),
        ("process_loopback_mode", ctypes.c_uint32),
    ]


class _Blob(ctypes.Structure):
    """PROPVARIANT 中的 BLOB 成员。"""

    _fields_ = [
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _PropVariantUnion(ctypes.Union):
    """满足 Windows x64 PROPVARIANT 对齐要求的联合体。"""

    _fields_ = [
        ("blob", _Blob),
        ("padding", ctypes.c_ubyte * 16),
    ]


class _PropVariant(ctypes.Structure):
    """ActivateAudioInterfaceAsync 使用的 PROPVARIANT。"""

    _fields_ = [
        ("variant_type", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("value", _PropVariantUnion),
    ]


class _ComPointer(ctypes.Structure):
    """带有 COM 虚表指针的最小结构。"""

    _fields_ = [("vtable", ctypes.POINTER(ctypes.c_void_p))]


class _ActivationCallback:
    """实现可跨线程调用的 WASAPI 异步激活完成回调。"""

    _IID_IUNKNOWN = "00000000-0000-0000-C000-000000000046"
    _IID_COMPLETION_HANDLER = "41D949AB-9862-444A-80F6-C261334DA5EB"

    def __init__(self) -> None:
        self.event = threading.Event()
        self.ref_count = 1
        self._lock = threading.Lock()
        self._retired = False
        self._ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self._stdcall = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        self._query_interface = self._stdcall(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )(self._query_interface_impl)
        self._add_ref = self._stdcall(ctypes.c_ulong, ctypes.c_void_p)(self._add_ref_impl)
        self._release = self._stdcall(ctypes.c_ulong, ctypes.c_void_p)(self._release_impl)
        self._activate_completed = self._stdcall(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(self._activate_completed_impl)
        self._vtable = (ctypes.c_void_p * 4)(
            ctypes.cast(self._query_interface, ctypes.c_void_p).value,
            ctypes.cast(self._add_ref, ctypes.c_void_p).value,
            ctypes.cast(self._release, ctypes.c_void_p).value,
            ctypes.cast(self._activate_completed, ctypes.c_void_p).value,
        )
        self._object = _ComPointer(ctypes.cast(self._vtable, ctypes.POINTER(ctypes.c_void_p)))
        self.pointer = ctypes.cast(ctypes.pointer(self._object), ctypes.c_void_p)

        self._ole32.CoCreateFreeThreadedMarshaler.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateFreeThreadedMarshaler.restype = ctypes.c_long
        self._free_threaded_marshaler = ctypes.c_void_p()
        result = self._ole32.CoCreateFreeThreadedMarshaler(
            self.pointer,
            ctypes.byref(self._free_threaded_marshaler),
        )
        _check_hresult(result, "创建线程间音频激活回调")

    def _query_interface_impl(self, _this, iid, output) -> int:
        if not iid or not output:
            return _hresult(0x80004003)
        output[0] = None
        requested = uuid.UUID(bytes_le=ctypes.string_at(iid, 16))
        own_iids = {
            uuid.UUID(self._IID_IUNKNOWN),
            uuid.UUID(self._IID_COMPLETION_HANDLER),
        }
        if requested in own_iids:
            output[0] = self.pointer
            self._add_ref_impl(self.pointer)
            return 0
        if not self._free_threaded_marshaler:
            return _hresult(0x80004002)
        try:
            return int(
                _com_method(
                    self._free_threaded_marshaler,
                    0,
                    ctypes.c_long,
                    ctypes.POINTER(_Guid),
                    ctypes.POINTER(ctypes.c_void_p),
                )(
                    self._free_threaded_marshaler,
                    iid,
                    output,
                )
            )
        except Exception:
            logging.exception("转发音频激活回调的 COM 查询失败")
            return _hresult(0x80004002)

    def _add_ref_impl(self, _this) -> int:
        with self._lock:
            self.ref_count += 1
            return self.ref_count

    def _release_impl(self, _this) -> int:
        with self._lock:
            self.ref_count = max(0, self.ref_count - 1)
            return self.ref_count

    def _activate_completed_impl(self, _this, _operation) -> int:
        self.event.set()
        # 异步回调返回后再释放对象，避免系统仍在调用时销毁 ctypes 回调。
        threading.Timer(0.1, self.retire).start()
        return 0

    def retain_for_async_call(self) -> None:
        """在 Windows 完成异步调用前保持回调对象存活。"""
        with _activation_callbacks_lock:
            _pending_activation_callbacks.add(self)

    def retire(self) -> None:
        """异步调用完成后移除保活引用并释放线程间封送器。"""
        with self._lock:
            if self._retired:
                return
            self._retired = True
        with _activation_callbacks_lock:
            _pending_activation_callbacks.discard(self)
        self.release_marshaler()

    def release_marshaler(self) -> None:
        pointer = self._free_threaded_marshaler
        self._free_threaded_marshaler = ctypes.c_void_p()
        if pointer:
            _release_com(pointer)


class _ProcessLoopbackClient:
    """打开一个指定进程树的 16 kHz 单声道采集流。"""

    _IID_AUDIO_CLIENT = _Guid.parse("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
    _IID_AUDIO_CAPTURE_CLIENT = _Guid.parse("C8ADBD64-E71E-48A0-A4DE-185C395CD317")

    def __init__(self, process_id: int, stop_event: threading.Event) -> None:
        self.process_id = process_id
        self.audio_client = ctypes.c_void_p()
        self.capture_client = ctypes.c_void_p()
        self.sample_event = ctypes.c_void_p()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._mmdevapi = ctypes.WinDLL("mmdevapi", use_last_error=True)
        try:
            self._activate(stop_event)
        except Exception:
            self.close()
            raise

    def _activate(self, stop_event: threading.Event) -> None:
        activation = _AudioClientActivationParams(1, self.process_id, 0)
        activation_pointer = ctypes.cast(ctypes.pointer(activation), ctypes.POINTER(ctypes.c_ubyte))
        parameters = _PropVariant()
        parameters.variant_type = 65  # 使用 VT_BLOB 传递进程回环参数。
        parameters.value.blob = _Blob(ctypes.sizeof(activation), activation_pointer)

        callback = _ActivationCallback()
        callback.retain_for_async_call()
        operation = ctypes.c_void_p()
        async_call_pending = False
        try:
            activate = self._mmdevapi.ActivateAudioInterfaceAsync
            activate.argtypes = [
                ctypes.c_wchar_p,
                ctypes.POINTER(_Guid),
                ctypes.POINTER(_PropVariant),
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            activate.restype = ctypes.c_long
            result = activate(
                "VAD\\Process_Loopback",
                ctypes.byref(self._IID_AUDIO_CLIENT),
                ctypes.byref(parameters),
                callback.pointer,
                ctypes.byref(operation),
            )
            async_call_pending = result >= 0
            if not async_call_pending:
                callback.retire()
            _check_hresult(result, "请求进程音频回环")
            deadline = time.monotonic() + _ACTIVATION_TIMEOUT_SECONDS
            while not callback.event.wait(0.1):
                if stop_event.is_set():
                    raise InterruptedError("已停止等待进程音频回环激活。")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Windows 音频接口激活超时。")
            async_call_pending = False
            if stop_event.is_set():
                raise InterruptedError("已停止等待进程音频回环激活。")

            if not operation:
                raise OSError("Windows 未返回进程音频回环操作句柄。")
            activation_result = ctypes.c_long()
            activated_interface = ctypes.c_void_p()
            _check_hresult(
                _com_method(
                    operation,
                    3,
                    ctypes.c_long,
                    ctypes.POINTER(ctypes.c_long),
                    ctypes.POINTER(ctypes.c_void_p),
                )(
                    operation,
                    ctypes.byref(activation_result),
                    ctypes.byref(activated_interface),
                ),
                "读取进程音频回环激活结果",
            )
            _check_hresult(activation_result.value, "打开进程音频回环")
            self.audio_client = activated_interface
            self._initialize_stream()
        finally:
            if operation:
                _release_com(operation)
            if not async_call_pending:
                callback.retire()

    def _initialize_stream(self) -> None:
        self.sample_event = self._kernel32.CreateEventW(None, False, False, None)
        if not self.sample_event:
            raise ctypes.WinError(ctypes.get_last_error())

        audio_format = _WaveFormatEx(
            1,
            1,
            16_000,
            32_000,
            2,
            16,
            0,
        )
        stream_flags = 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000
        _check_hresult(
            _com_method(
                self.audio_client,
                3,
                ctypes.c_long,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_int64,
                ctypes.c_int64,
                ctypes.POINTER(_WaveFormatEx),
                ctypes.c_void_p,
            )(
                self.audio_client,
                0,
                stream_flags,
                0,
                0,
                ctypes.byref(audio_format),
                None,
            ),
            "初始化进程音频回环",
        )
        _check_hresult(
            _com_method(self.audio_client, 13, ctypes.c_long, ctypes.c_void_p)(
                self.audio_client,
                self.sample_event,
            ),
            "设置进程音频回环事件",
        )
        _check_hresult(
            _com_method(
                self.audio_client,
                14,
                ctypes.c_long,
                ctypes.POINTER(_Guid),
                ctypes.POINTER(ctypes.c_void_p),
            )(
                self.audio_client,
                ctypes.byref(self._IID_AUDIO_CAPTURE_CLIENT),
                ctypes.byref(self.capture_client),
            ),
            "取得进程音频采集接口",
        )
        _check_hresult(
            _com_method(self.audio_client, 10, ctypes.c_long)(self.audio_client),
            "启动进程音频回环",
        )

    def read(self, stop_event: threading.Event) -> np.ndarray | None:
        """等待并读取当前缓冲区中的音频；超时或停止时返回空值。"""
        result = self._kernel32.WaitForSingleObject(self.sample_event, 100)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if result != 0:
            return None

        output: list[np.ndarray] = []
        while True:
            if stop_event.is_set():
                return None
            available = ctypes.c_uint32()
            _check_hresult(
                _com_method(
                    self.capture_client,
                    5,
                    ctypes.c_long,
                    ctypes.POINTER(ctypes.c_uint32),
                )(
                    self.capture_client,
                    ctypes.byref(available),
                ),
                "查询进程音频数据包",
            )
            if available.value == 0:
                break

            data = ctypes.c_void_p()
            frame_count = ctypes.c_uint32()
            flags = ctypes.c_uint32()
            device_position = ctypes.c_uint64()
            qpc_position = ctypes.c_uint64()
            _check_hresult(
                _com_method(
                    self.capture_client,
                    3,
                    ctypes.c_long,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_uint32),
                    ctypes.POINTER(ctypes.c_uint32),
                    ctypes.POINTER(ctypes.c_uint64),
                    ctypes.POINTER(ctypes.c_uint64),
                )(
                    self.capture_client,
                    ctypes.byref(data),
                    ctypes.byref(frame_count),
                    ctypes.byref(flags),
                    ctypes.byref(device_position),
                    ctypes.byref(qpc_position),
                ),
                "读取进程音频数据包",
            )
            try:
                if frame_count.value:
                    if flags.value & 0x2 or not data:
                        samples = np.zeros(frame_count.value, dtype=np.float32)
                    else:
                        raw = ctypes.cast(data, ctypes.POINTER(ctypes.c_int16))
                        samples = np.ctypeslib.as_array(raw, shape=(frame_count.value,)).copy()
                        samples = samples.astype(np.float32) / 32768.0
                    output.append(samples)
            finally:
                _check_hresult(
                    _com_method(
                        self.capture_client,
                        4,
                        ctypes.c_long,
                        ctypes.c_uint32,
                    )(
                        self.capture_client,
                        frame_count.value,
                    ),
                    "释放进程音频数据包",
                )

        if not output:
            return None
        return np.concatenate(output)

    def close(self) -> None:
        """停止采集并释放本进程的 Windows 音频对象。"""
        if self.audio_client:
            try:
                _com_method(self.audio_client, 11, ctypes.c_long)(self.audio_client)
            except Exception:
                logging.debug("停止进程音频回环时返回异常", exc_info=True)
        if self.capture_client:
            _release_com(self.capture_client)
            self.capture_client = ctypes.c_void_p()
        if self.audio_client:
            _release_com(self.audio_client)
            self.audio_client = ctypes.c_void_p()
        if self.sample_event:
            self._kernel32.CloseHandle(self.sample_event)
            self.sample_event = ctypes.c_void_p()


class ProcessAudioCapture:
    """自动寻找正在发声的应用，并使用 Windows 进程回环采集。"""

    sample_rate = 16_000
    block_size = 320

    def __init__(self, _device_name: str = PROCESS_AUDIO_DEVICE) -> None:
        self.device_name = PROCESS_AUDIO_DEVICE
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_process_id: int | None = None

    def start(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None],
        on_ready: Callable[[str], None] | None = None,
    ) -> None:
        """在后台线程开始自动查找和采集。"""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("进程音频采集已经运行")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(on_audio, on_error, on_ready),
            name="Beta 进程音频采集",
            daemon=True,
        )
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        """请求采集线程停止。"""
        self._stop_event.set()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _capture_loop(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None],
        on_ready: Callable[[str], None] | None,
    ) -> None:
        com_initialized = False
        loopback: _ProcessLoopbackClient | None = None
        last_status = ""
        last_scan = 0.0
        last_signal_at = 0.0
        current_process_id: int | None = None
        pending_process_id: int | None = None
        pending_count = 0
        failed_until: dict[int, float] = {}
        candidates: list[AudioProcessCandidate] = []

        def report_status(value: str) -> None:
            nonlocal last_status
            if value != last_status:
                last_status = value
                if on_ready:
                    on_ready(value)

        try:
            if os.name != "nt":
                raise RuntimeError("Beta 进程音频采集仅支持 Windows。")
            ole32 = ctypes.WinDLL("ole32", use_last_error=True)
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            ole32.CoInitializeEx.restype = ctypes.c_long
            ole32.CoUninitialize.argtypes = []
            ole32.CoUninitialize.restype = None
            result = ole32.CoInitializeEx(None, 0)
            if result < 0:
                _check_hresult(result, "初始化 Windows 音频会话接口")
            com_initialized = True
            report_status("Beta：等待应用播放声音")

            while not self._stop_event.is_set():
                now = time.monotonic()
                if now - last_scan >= _SESSION_SCAN_INTERVAL:
                    candidates = _enumerate_audio_processes(os.getpid())
                    foreground_process_id = _foreground_process_id(os.getpid())
                    for process_id, expiry in tuple(failed_until.items()):
                        if now >= expiry:
                            failed_until.pop(process_id, None)
                    available = [item for item in candidates if item.process_id not in failed_until]
                    target = self._choose_process(
                        available,
                        foreground_process_id,
                        current_process_id,
                        now,
                        last_signal_at,
                    )
                    if target and target.process_id != pending_process_id:
                        pending_process_id = target.process_id
                        pending_count = 1
                    elif target:
                        pending_count += 1
                    else:
                        pending_process_id = None
                        pending_count = 0

                    if target and pending_count >= 2 and target.process_id != current_process_id:
                        if loopback:
                            loopback.close()
                            loopback = None
                        current_process_id = None
                        self._active_process_id = None
                        try:
                            loopback = _ProcessLoopbackClient(target.process_id, self._stop_event)
                            current_process_id = target.process_id
                            self._active_process_id = target.process_id
                            last_signal_at = now
                            report_status(f"Beta：正在识别 {target.name}")
                            logging.info(
                                "Beta 进程回环已切换：进程=%s，名称=%s，峰值=%s",
                                target.process_id,
                                target.name,
                                target.peak,
                            )
                        except Exception as error:
                            failed_until[target.process_id] = now + _PROCESS_RETRY_SECONDS
                            logging.warning(
                                "Beta 进程回环无法打开：进程=%s，名称=%s，原因=%s",
                                target.process_id,
                                target.name,
                                error,
                            )
                            report_status(f"Beta：无法捕获 {target.name}，正在寻找其他应用")
                            loopback = None
                            current_process_id = None
                            self._active_process_id = None
                            pending_process_id = None
                            pending_count = 0

                    if current_process_id is not None:
                        current = next(
                            (item for item in candidates if item.process_id == current_process_id),
                            None,
                        )
                        if current and current.peak is not None and current.peak >= _AUDIBLE_PEAK:
                            last_signal_at = now
                        elif now - last_signal_at >= _SESSION_HOLD_SECONDS:
                            if target is None or target.process_id == current_process_id:
                                if loopback:
                                    loopback.close()
                                    loopback = None
                                current_process_id = None
                                self._active_process_id = None
                                pending_process_id = None
                                pending_count = 0
                                report_status("Beta：等待应用播放声音")

                    if current_process_id is None and target is None:
                        report_status("Beta：等待应用播放声音")
                    last_scan = now

                if loopback:
                    try:
                        frames = loopback.read(self._stop_event)
                    except Exception as error:
                        logging.warning("Beta 进程音频采集发生错误：%s", error)
                        failed_until[current_process_id or 0] = time.monotonic() + _PROCESS_RETRY_SECONDS
                        loopback.close()
                        loopback = None
                        current_process_id = None
                        self._active_process_id = None
                        report_status("Beta：采集暂时中断，正在重新寻找应用")
                        continue
                    if frames is not None and frames.size:
                        on_audio(frames)
                else:
                    self._stop_event.wait(0.05)
        except Exception as error:
            if not self._stop_event.is_set():
                logging.exception("Beta 进程音频采集初始化失败")
                on_error(error)
        finally:
            if loopback:
                loopback.close()
            self._active_process_id = None
            if com_initialized:
                ole32.CoUninitialize()

    @staticmethod
    def _choose_process(
        candidates: list[AudioProcessCandidate],
        foreground_process_id: int | None,
        current_process_id: int | None,
        now: float,
        last_signal_at: float,
    ) -> AudioProcessCandidate | None:
        """优先选有声音的前台应用，其次选峰值最高的活动应用。"""
        audible = [
            item for item in candidates
            if item.peak is not None and item.peak >= _AUDIBLE_PEAK
        ]
        foreground = next(
            (item for item in audible if item.process_id == foreground_process_id),
            None,
        )
        if foreground:
            return foreground

        current = next(
            (item for item in candidates if item.process_id == current_process_id),
            None,
        )
        if current and current.peak is not None and current.peak >= _AUDIBLE_PEAK:
            return current
        if current and now - last_signal_at < _SESSION_HOLD_SECONDS:
            return current
        if audible:
            return max(audible, key=lambda item: item.peak or 0.0)

        unmetered = [item for item in candidates if item.peak is None]
        if foreground_process_id is not None:
            foreground = next(
                (item for item in unmetered if item.process_id == foreground_process_id),
                None,
            )
            if foreground:
                return foreground
        if len(unmetered) == 1:
            return unmetered[0]
        return None


def _enumerate_audio_processes(excluded_process_id: int) -> list[AudioProcessCandidate]:
    """枚举所有活动播放端点上的进程会话和峰值。"""
    if os.name != "nt":
        raise RuntimeError("音频会话枚举仅支持 Windows。")

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    clsid_enumerator = _Guid.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
    iid_enumerator = _Guid.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
    iid_session_manager2 = _Guid.parse("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
    iid_session_control2 = _Guid.parse("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
    iid_meter = _Guid.parse("C02216F6-8C67-4B5B-9D00-D008E73E0064")

    enumerator = ctypes.c_void_p()
    collection = ctypes.c_void_p()
    sessions_by_process: dict[int, tuple[str, float | None]] = {}
    try:
        _check_hresult(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_enumerator),
                None,
                23,
                ctypes.byref(iid_enumerator),
                ctypes.byref(enumerator),
            ),
            "创建播放设备枚举器",
        )
        _check_hresult(
            _com_method(
                enumerator,
                3,
                ctypes.c_long,
                ctypes.c_int,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            )(
                enumerator,
                0,
                1,
                ctypes.byref(collection),
            ),
            "枚举活动播放设备",
        )
        device_count = ctypes.c_uint32()
        _check_hresult(
            _com_method(collection, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))(
                collection,
                ctypes.byref(device_count),
            ),
            "读取活动播放设备数量",
        )

        process_names: dict[int, str] = {}
        for device_index in range(device_count.value):
            device = ctypes.c_void_p()
            manager = ctypes.c_void_p()
            session_enumerator = ctypes.c_void_p()
            try:
                _check_hresult(
                    _com_method(
                        collection,
                        4,
                        ctypes.c_long,
                        ctypes.c_uint32,
                        ctypes.POINTER(ctypes.c_void_p),
                    )(
                        collection,
                        device_index,
                        ctypes.byref(device),
                    ),
                    "读取播放设备",
                )
                _check_hresult(
                    _com_method(
                        device,
                        3,
                        ctypes.c_long,
                        ctypes.POINTER(_Guid),
                        ctypes.c_uint32,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p),
                    )(
                        device,
                        ctypes.byref(iid_session_manager2),
                        23,
                        None,
                        ctypes.byref(manager),
                    ),
                    "打开播放设备会话管理器",
                )
                _check_hresult(
                    _com_method(manager, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                        manager,
                        ctypes.byref(session_enumerator),
                    ),
                    "枚举播放会话",
                )
                session_count = ctypes.c_int()
                _check_hresult(
                    _com_method(session_enumerator, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                        session_enumerator,
                        ctypes.byref(session_count),
                    ),
                    "读取播放会话数量",
                )

                for session_index in range(max(0, session_count.value)):
                    session = ctypes.c_void_p()
                    session2 = ctypes.c_void_p()
                    meter = ctypes.c_void_p()
                    try:
                        _check_hresult(
                            _com_method(
                                session_enumerator,
                                4,
                                ctypes.c_long,
                                ctypes.c_int,
                                ctypes.POINTER(ctypes.c_void_p),
                            )(
                                session_enumerator,
                                session_index,
                                ctypes.byref(session),
                            ),
                            "读取播放会话",
                        )
                        state = ctypes.c_int()
                        _check_hresult(
                            _com_method(session, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                                session,
                                ctypes.byref(state),
                            ),
                            "读取播放会话状态",
                        )
                        if state.value != 1:
                            continue

                        _check_hresult(
                            _com_method(
                                session,
                                0,
                                ctypes.c_long,
                                ctypes.POINTER(_Guid),
                                ctypes.POINTER(ctypes.c_void_p),
                            )(
                                session,
                                ctypes.byref(iid_session_control2),
                                ctypes.byref(session2),
                            ),
                            "读取播放会话进程信息",
                        )
                        process_id = ctypes.c_uint32()
                        _check_hresult(
                            _com_method(
                                session2,
                                14,
                                ctypes.c_long,
                                ctypes.POINTER(ctypes.c_uint32),
                            )(
                                session2,
                                ctypes.byref(process_id),
                            ),
                            "读取播放进程编号",
                        )
                        pid = int(process_id.value)
                        if pid <= 4 or pid == excluded_process_id:
                            continue

                        peak: float | None = None
                        query_result = _com_method(
                            session,
                            0,
                            ctypes.c_long,
                            ctypes.POINTER(_Guid),
                            ctypes.POINTER(ctypes.c_void_p),
                        )(
                            session,
                            ctypes.byref(iid_meter),
                            ctypes.byref(meter),
                        )
                        if query_result >= 0 and meter:
                            value = ctypes.c_float()
                            meter_result = _com_method(
                                meter,
                                3,
                                ctypes.c_long,
                                ctypes.POINTER(ctypes.c_float),
                            )(
                                meter,
                                ctypes.byref(value),
                            )
                            if meter_result >= 0:
                                peak = max(0.0, float(value.value))

                        process_name = process_names.get(pid)
                        if process_name is None:
                            process_name = _process_name(pid)
                            process_names[pid] = process_name
                        prior = sessions_by_process.get(pid)
                        if prior is None or (peak or 0.0) > (prior[1] or 0.0):
                            sessions_by_process[pid] = (process_name, peak)
                    finally:
                        if meter:
                            _release_com(meter)
                        if session2:
                            _release_com(session2)
                        if session:
                            _release_com(session)
            except Exception as error:
                logging.debug("读取一个播放设备的活动会话失败：%s", error)
            finally:
                if session_enumerator:
                    _release_com(session_enumerator)
                if manager:
                    _release_com(manager)
                if device:
                    _release_com(device)
    finally:
        if collection:
            _release_com(collection)
        if enumerator:
            _release_com(enumerator)

    return [
        AudioProcessCandidate(process_id, name, peak)
        for process_id, (name, peak) in sessions_by_process.items()
    ]


def _process_name(process_id: int) -> str:
    """取得进程可执行文件名；读取失败时保留进程编号。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    process = kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return f"进程 {process_id}"
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.c_uint32(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
            return f"进程 {process_id}"
        return PureWindowsPath(buffer.value).name or f"进程 {process_id}"
    finally:
        kernel32.CloseHandle(process)


def _foreground_process_id(excluded_process_id: int) -> int | None:
    """返回当前前台窗口的进程编号。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    window = user32.GetForegroundWindow()
    if not window:
        return None
    process_id = ctypes.c_uint32()
    if not user32.GetWindowThreadProcessId(window, ctypes.byref(process_id)):
        return None
    if process_id.value in (0, excluded_process_id):
        return None
    return int(process_id.value)


def _com_method(interface, index: int, result_type, *argument_types):
    """按虚表序号建立一次 COM 方法调用。"""
    stdcall = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = stdcall(result_type, ctypes.c_void_p, *argument_types)(table[index])
    return method


def _release_com(interface) -> None:
    """释放一个 COM 接口引用。"""
    if interface:
        _com_method(interface, 2, ctypes.c_ulong)(interface)


def _hresult(value: int) -> int:
    """把无符号错误码转换成 HRESULT 有符号值。"""
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _check_hresult(value: int, operation: str) -> None:
    """将 Windows HRESULT 转成包含步骤和代码的异常。"""
    result = ctypes.c_long(value).value
    if result < 0:
        raise OSError(f"{operation}失败（HRESULT 0x{result & 0xFFFFFFFF:08X}）")
