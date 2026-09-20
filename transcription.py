"""本地 faster-whisper 语音识别适配层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import logging
from typing import Callable

import numpy as np

from download_manager import DownloadProgress
from gpu_runtime import configure_gpu_runtime, gpu_runtime_available
from lexicon import build_hotwords
from model_manager import (
    ensure_model_available,
    find_local_model,
    format_bytes,
    model_cache_hint as managed_model_cache_hint,
)
from runtime_components import nvidia_driver_available, prepare_component
from settings import RECOGNITION_BEAM_SIZES


@dataclass(frozen=True)
class RecognizedWord:
    """用于合并重叠识别窗口的词级时间信息。"""
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class RecognitionResult:
    """一次音频窗口的识别结果。"""

    text: str
    language: str | None
    words: list[RecognizedWord] = field(default_factory=list)


def _temperature_values(value: str | tuple[float, ...] | list[float]) -> tuple[float, ...]:
    """把界面保存的温度序列转换为 faster-whisper 可接受的数值。"""
    if isinstance(value, (tuple, list)):
        raw_values = value
    else:
        raw_values = value.split(",")
    temperatures = []
    for raw_value in raw_values:
        try:
            temperature = min(1.0, max(0.0, float(raw_value)))
        except (TypeError, ValueError):
            continue
        if temperature not in temperatures:
            temperatures.append(temperature)
    return tuple(temperatures) or (0.0,)


class WhisperRecognizer:
    """封装 faster-whisper 的模型加载和识别调用。"""

    def __init__(
        self,
        model_size: str,
        device_mode: str,
        model_path: str = "",
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.model_size = model_size
        self.device_mode = device_mode
        self.model_path = model_path.strip()
        self._model = None
        self.actual_device = ""
        self.actual_compute_type = ""
        self._on_status = on_status or (lambda _text: None)

    def load(self) -> None:
        """准备组件并加载本地模型。"""
        if self.model_path:
            model_source = Path(self.model_path)
            if not model_source.is_dir():
                raise RuntimeError(f"指定的模型目录不存在：{model_source}")
        else:
            model_source = find_local_model(self.model_size)
            if model_source is None:
                self._on_status(f"正在准备语音模型 {self.model_size}……")
                model_source = ensure_model_available(
                    self.model_size,
                    on_progress=self._download_progress,
                )
        if self.device_mode == "CPU（int8）":
            candidates = [("cpu", "int8")]
        elif self.device_mode == "NVIDIA GPU（float16）":
            candidates = [("cuda", "float16")]
        else:
            candidates = [("cuda", "float16"), ("cpu", "int8")]

        if candidates and candidates[0][0] == "cuda":
            if not nvidia_driver_available():
                if self.device_mode == "NVIDIA GPU（float16）":
                    raise RuntimeError("未检测到NVIDIA显卡驱动，无法使用GPU识别。")
                candidates = [("cpu", "int8")]
            else:
                try:
                    if gpu_runtime_available():
                        self._on_status("正在使用本机NVIDIA GPU运行库……")
                    else:
                        self._on_status("正在准备NVIDIA GPU运行库……")
                        prepare_component("runtime.cuda12", on_progress=self._download_progress)
                        if not gpu_runtime_available(refresh=True):
                            raise RuntimeError("已准备的NVIDIA运行库无法加载。")
                except Exception as error:
                    if self.device_mode == "NVIDIA GPU（float16）":
                        raise RuntimeError(f"GPU运行库准备失败：{error}") from error
                    logging.warning("GPU运行库准备失败，自动模式回退CPU：%s", error)
                    candidates = [("cpu", "int8")]

        configure_gpu_runtime()
        from faster_whisper import WhisperModel

        errors: list[str] = []
        for device, compute_type in candidates:
            try:
                self._model = WhisperModel(
                    str(model_source),
                    device=device,
                    compute_type=compute_type,
                    local_files_only=True,
                )
                if device == "cuda":
                    # 先完成首次 GPU 推理初始化；初始化失败仍按既有自动模式尝试 CPU。
                    warm_segments, _ = self._model.transcribe(
                        np.zeros(16000, dtype=np.float32), language="en", vad_filter=False,
                        beam_size=1, without_timestamps=True,
                    )
                    list(warm_segments)
                logging.info("识别模型已加载：名称=%s，路径=%s，设备=%s/%s", self.model_size, model_source, device, compute_type)
                self.actual_device = device
                self.actual_compute_type = compute_type
                return
            except Exception as error:  # 自动模式需要保留两种设备的失败原因
                errors.append(f"{device}/{compute_type}: {error}")
        joined = "；".join(errors)
        raise RuntimeError(f"语音模型加载失败：{joined}")

    def _download_progress(self, progress: DownloadProgress) -> None:
        """把组件下载状态转换为主窗口状态栏文字。"""
        percentage = ""
        if progress.fraction is not None:
            percentage = f"，{progress.fraction * 100:.1f}%"
        speed = ""
        if progress.speed:
            speed = f"，{format_bytes(int(progress.speed))}/秒"
        file_name = f"，文件={progress.file_name}" if progress.file_name else ""
        total = f" / {format_bytes(progress.total)}" if progress.total else ""
        self._on_status(
            f"{progress.component_key}：{progress.state}{percentage}，"
            f"已下载{format_bytes(progress.completed)}{total}{speed}{file_name}"
        )

    @property
    def loaded(self) -> bool:
        """返回模型是否已加载。"""
        return self._model is not None

    def transcribe(
        self,
        audio: np.ndarray,
        source_language: str | None,
        realtime: bool = False,
        beam_size: int = 1,
        condition_on_previous_text: bool = False,
        temperature_schedule: str | tuple[float, ...] | list[float] = "0",
        lexicon_mode: str = "关闭",
        custom_hotwords: str = "",
    ) -> RecognitionResult:
        """识别一段 16 kHz 单声道浮点音频。"""
        if self._model is None:
            raise RuntimeError("语音模型尚未加载")
        language = source_language or None
        selected_beam_size = (
            beam_size if beam_size in RECOGNITION_BEAM_SIZES else RECOGNITION_BEAM_SIZES[0]
        )
        temperatures = _temperature_values(temperature_schedule)
        hotwords = build_hotwords(lexicon_mode, custom_hotwords)
        if realtime:
            # 即时草稿只负责尽快显示当前文字；完整识别负责词时间、标点和更准确的结果。
            transcribe_options = {
                "beam_size": selected_beam_size,
                "best_of": selected_beam_size,
                "temperature": temperatures,
                "condition_on_previous_text": condition_on_previous_text,
                "vad_filter": False,
                "word_timestamps": False,
                "without_timestamps": True,
            }
        else:
            transcribe_options = {
                # 搜索范围、前文使用方式和温度序列由界面配置，仍保留词时间与标点。
                "beam_size": selected_beam_size,
                "best_of": selected_beam_size,
                "temperature": temperatures,
                "condition_on_previous_text": condition_on_previous_text,
                "vad_filter": True,
                "word_timestamps": True,
                "vad_parameters": {"min_silence_duration_ms": 400},
            }
        if hotwords:
            transcribe_options["hotwords"] = hotwords
        segments, info = self._model.transcribe(
            audio,
            language=language,
            **transcribe_options,
        )
        decoded = list(segments)
        detected_language = getattr(info, "language", None)
        # 识别适配层只返回模型原文；段落、标点和显示规范化由独立显示模块处理。
        text = "".join(segment.text for segment in decoded)
        words = []
        for segment in decoded:
            timed_words = getattr(segment, "words", None)
            if timed_words:
                words.extend(RecognizedWord(word.start, word.end, word.word) for word in timed_words)
            elif segment.text:
                words.append(RecognizedWord(segment.start, segment.end, segment.text))
        return RecognitionResult(text=text, language=detected_language, words=words)


def model_cache_hint() -> str:
    """返回模型管理位置提示，用于界面和日志。"""
    return managed_model_cache_hint()
