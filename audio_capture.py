"""Windows 系统播放回环与麦克风音频采集。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import numpy as np


class SystemAudioCapture:
    """使用 SoundCard 采集所选系统播放源或麦克风。"""

    sample_rate = 16_000
    # 二十毫秒采集块，减少首字在输入缓冲中的等待。
    block_size = 320

    def __init__(self, device_name: str = "") -> None:
        self.device_name = device_name.strip()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.resolved_device_name = ""
        self.resolved_is_loopback = False

    @staticmethod
    def list_loopback_devices() -> list[str]:
        """列出可用的扬声器回环设备。"""
        import soundcard as sc

        devices = sc.all_microphones(include_loopback=True)
        result: list[str] = []
        for device in devices:
            name = str(getattr(device, "name", "")).strip()
            if not name:
                continue
            if bool(getattr(device, "isloopback", False)):
                result.append(name)
        if not result:
            default_name = SystemAudioCapture.default_loopback_device()
            if default_name:
                result.append(default_name)
        return result

    @staticmethod
    def list_audio_devices() -> list[str]:
        """区分播放回环与麦克风，避免同名设备导致输入来源混淆。"""
        import soundcard as sc
        devices = sc.all_microphones(include_loopback=True)
        outputs = [f"系统播放：{device.name}" for device in devices if device.isloopback]
        inputs = [f"麦克风：{device.name}" for device in devices if not device.isloopback]
        return outputs + inputs

    @staticmethod
    def default_loopback_device() -> str:
        """取得默认扬声器对应的回环设备名称。"""
        import soundcard as sc

        speaker = sc.default_speaker()
        return str(getattr(speaker, "name", "")).strip()

    def start(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None],
        on_ready: Callable[[str], None] | None = None,
    ) -> None:
        """在后台线程开始采集。"""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("系统音频采集已经运行")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(on_audio, on_error, on_ready),
            name="系统音频采集",
            daemon=True,
        )
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        """请求后台采集线程停止；界面线程默认不等待设备调用返回。"""
        self._stop_event.set()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _capture_loop(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None],
        on_ready: Callable[[str], None] | None = None,
    ) -> None:
        try:
            import soundcard as sc

            microphone = self._resolve_microphone(sc)
            self.resolved_device_name = str(getattr(microphone, "name", "")).strip()
            self.resolved_is_loopback = bool(getattr(microphone, "isloopback", False))
            try:
                default_name = str(getattr(sc.default_speaker(), "name", "")).strip()
            except Exception:
                default_name = ""
            logging.info(
                "音频端点解析：默认播放=%s，配置选择=%s，采集目标=%s，回环=%s",
                default_name or "未读取",
                self.device_name or "未指定",
                self.resolved_device_name or "未读取",
                self.resolved_is_loopback,
            )
            with microphone.recorder(
                samplerate=self.sample_rate,
                # 按设备原生通道采集，再使用既有混音逻辑转换为模型需要的单声道。
                channels=None,
                blocksize=self.block_size,
                exclusive_mode=False,
            ) as recorder:
                # 设备成功打开后才向界面确认实际采集来源。
                logging.info("音频设备已打开：%s，回环=%s", self.resolved_device_name, self.resolved_is_loopback)
                if on_ready:
                    on_ready(self.resolved_device_name)
                while not self._stop_event.is_set():
                    frames = recorder.record(numframes=self.block_size)
                    if frames is None:
                        continue
                    audio = np.asarray(frames, dtype=np.float32)
                    if audio.ndim == 2:
                        audio = audio.mean(axis=1)
                    audio = np.ravel(audio)
                    if audio.size:
                        on_audio(audio)
        except Exception as error:  # 设备异常需要交给界面显示
            if not self._stop_event.is_set():
                on_error(error)

    def _resolve_microphone(self, soundcard_module):
        """按输入类型与完整名称解析设备；旧配置中的裸名称仍指播放回环。"""
        microphone_input = self.device_name.startswith("麦克风：")
        name = self.device_name
        for prefix in ("系统播放：", "麦克风："):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        if not name:
            name = str(soundcard_module.default_speaker().name)
        devices = soundcard_module.all_microphones(include_loopback=True)
        selected = next((device for device in devices if str(device.name) == name
                         and bool(device.isloopback) != microphone_input), None)
        if selected is None:
            raise RuntimeError(f"所选音频设备不可用：{self.device_name or name}。请刷新设备列表。")
        return selected
