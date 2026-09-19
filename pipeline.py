"""系统音频、语音识别和翻译的后台流水线。"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable

import numpy as np

from audio_capture import SystemAudioCapture
from transcription import WhisperRecognizer
from rolling_transcript import RollingTranscript
from translation import TranslationService
from subtitle_events import SourceUpdate
from text_processing import remove_subtitle_credit


@dataclass(frozen=True)
class PipelineOptions:
    """一组识别配置。"""

    source_language: str | None
    target_language: str
    model_size: str
    device_mode: str
    model_path: str
    audio_device: str
    translation_backend: str


_GPU_DEVICE_MODES = frozenset(("自动（优先GPU）", "NVIDIA GPU（float16）"))


def _model_source_key(options: PipelineOptions) -> tuple[str, str]:
    """返回实际模型来源；指定目录时模型大小只用于界面显示。"""
    model_path = options.model_path.strip()
    if model_path:
        return "path", model_path
    return "model", options.model_size


def _device_requires_reload(
    active_options: PipelineOptions,
    options: PipelineOptions,
    recognizer: WhisperRecognizer | None,
) -> bool:
    """只有实际计算后端会变化时才重新加载识别模型。"""
    if recognizer is None:
        return True
    if active_options.device_mode == options.device_mode:
        return False
    if recognizer.actual_device == "cuda":
        return options.device_mode not in _GPU_DEVICE_MODES
    if recognizer.actual_device == "cpu":
        return options.device_mode != "CPU（int8）"
    return True


class LivePipeline:
    """按有界重叠窗口识别，向独立显示模块提交段落版本。"""

    window_samples = 128_000
    # GPU 最小触发间隔为一百毫秒；推理期间到达的连续音频合并处理。
    gpu_quality_update_samples = 1_600
    # CPU 单次推理可能超过 500 ms，不启动即时通道，避免队列持续积压。
    cpu_quality_update_samples = 8_000

    def __init__(
        self,
        options: PipelineOptions,
        on_status: Callable[[str], None],
        on_source: Callable[[int, str, str | None, int], None],
        on_translation: Callable[[int, str, str], None],
        on_error: Callable[[str], None],
        on_stopped: Callable[[], None],
        on_preview: Callable[[int, str, str | None, int], None] | None = None,
        on_update: Callable[[SourceUpdate], None] | None = None,
        on_reset: Callable[[], None] | None = None,
    ) -> None:
        self.options = options
        self._on_status = on_status
        self._on_source = on_source
        self._on_update = on_update
        self._on_reset = on_reset
        self._on_translation = on_translation
        self._on_error = on_error
        self._on_stopped = on_stopped
        self._queue: queue.Queue[tuple[np.ndarray | None, Exception | None, float]] = queue.Queue()
        self._configuration_queue: queue.Queue[PipelineOptions] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: SystemAudioCapture | None = None
        self._lock = threading.Lock()
        self._stopped_callback_lock = threading.Lock()
        self._stopped_callback_sent = False
        self._configuration_generation = 0
        self._source_language = options.source_language
        self._target_language = options.target_language
        self._translation_sequence = 0
        self._pending_translations = {}
        self._translation_running = False
        self._translation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="翻译")

    def start(self) -> None:
        """启动后台流水线。"""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("识别流水线已经运行")
        self._stop_event.clear()
        with self._stopped_callback_lock:
            self._stopped_callback_sent = False
        self._thread = threading.Thread(
            target=self._run,
            name="音频识别流水线",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """发出停止请求并立即通知界面，不等待模型或设备线程。"""
        self._stop_event.set()
        capture = self._capture
        if capture:
            capture.stop(wait=False)
        self._translation_pool.shutdown(wait=False, cancel_futures=True)
        # 即使 CTranslate2 当前仍在 transcribe，界面也立即恢复可操作状态。
        self._notify_stopped()

    def set_languages(self, source_language: str | None, target_language: str) -> None:
        """兼容旧调用方，更新后续音频使用的语言。"""
        with self._lock:
            options = replace(
                self.options,
                source_language=source_language,
                target_language=target_language,
            )
        self.update_options(options)

    def update_options(self, options: PipelineOptions) -> bool:
        """在运行中提交新配置，后台线程会合并并应用最新的一组设置。"""
        with self._lock:
            if options == self.options:
                return False
            self.options = options
            self._source_language = options.source_language
            self._target_language = options.target_language
            self._configuration_generation += 1
            self._pending_translations.clear()
        self._configuration_queue.put(options)
        return True

    def _get_configuration(self) -> tuple[PipelineOptions, int]:
        """取得配置快照和版本号，保证一次识别使用同一组设置。"""
        with self._lock:
            return self.options, self._configuration_generation

    def _is_configuration_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._configuration_generation

    def _take_latest_configuration(self) -> PipelineOptions | None:
        """合并短时间内连续修改的设置，只应用最后一组。"""
        try:
            latest = self._configuration_queue.get_nowait()
        except queue.Empty:
            return None
        while True:
            try:
                latest = self._configuration_queue.get_nowait()
            except queue.Empty:
                return latest

    def _clear_audio_queue(self) -> None:
        """丢弃切换前已经排队的旧音频，避免新旧来源混在一起。"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        capture: SystemAudioCapture | None = None
        recognizer: WhisperRecognizer | None = None
        active_options: PipelineOptions | None = None
        active_generation = -1
        failed_generation = -1
        buffer = np.empty(0, dtype=np.float32)
        buffer_start = 0
        received_samples = 0
        quality_decoded_until = 0
        transcript = RollingTranscript()
        last_audio_status_at = time.monotonic()
        try:
            while not self._stop_event.is_set():
                requested = self._take_latest_configuration()
                options, generation = self._get_configuration()
                configuration_changed = requested is not None or generation != active_generation
                model_changed = (
                    active_options is None
                    or _model_source_key(active_options) != _model_source_key(options)
                    or _device_requires_reload(active_options, options, recognizer)
                )
                audio_changed = (
                    active_options is None
                    or active_options.audio_device != options.audio_device
                )

                if recognizer is None or configuration_changed:
                    # 当前配置加载失败时保持等待，直到用户再次修改配置，不重复下载同一组件。
                    if recognizer is None and failed_generation == generation:
                        time.sleep(0.1)
                        continue

                    if active_options is not None and configuration_changed:
                        if capture is not None and (model_changed or audio_changed):
                            capture.stop(wait=True)
                            capture = None
                            self._capture = None
                        self._clear_audio_queue()
                        buffer = np.empty(0, dtype=np.float32)
                        buffer_start = 0
                        received_samples = 0
                        quality_decoded_until = 0
                        transcript = RollingTranscript()
                        last_audio_status_at = time.monotonic()
                        if self._on_reset:
                            self._on_reset()

                    if recognizer is None or model_changed:
                        if capture is not None:
                            capture.stop(wait=True)
                            capture = None
                            self._capture = None
                        self._on_status(
                            "正在准备识别……"
                            if active_options is None
                            else "正在应用新的识别设置……"
                        )
                        # 新模型开始加载后不再继续使用旧识别器；失败时等待配置变化，避免重复加载。
                        recognizer = None
                        candidate = WhisperRecognizer(
                            options.model_size,
                            options.device_mode,
                            options.model_path,
                            on_status=self._on_status,
                        )
                        try:
                            candidate.load()
                        except Exception as error:
                            failed_generation = generation
                            logging.exception("识别设置加载失败")
                            if not self._stop_event.is_set():
                                self._on_error(f"新的识别设置无法使用：{error}")
                            continue
                        if self._stop_event.is_set():
                            break
                        if not self._is_configuration_current(generation):
                            continue
                        recognizer = candidate
                        failed_generation = -1

                    active_options = options
                    active_generation = generation
                    quality_update_samples = (
                        self.gpu_quality_update_samples
                        if recognizer.actual_device == "cuda"
                        else self.cpu_quality_update_samples
                    )
                    last_audio_status_at = time.monotonic()
                    if capture is None:
                        self._on_status("正在连接音频设备……")
                        capture = SystemAudioCapture(options.audio_device)
                        self._capture = capture
                        capture.start(
                            self._on_audio,
                            self._on_capture_error,
                            on_ready=lambda name: self._on_status(
                                f"正在识别；已打开采集设备：{name}"
                            ),
                        )
                    continue

                try:
                    frames, error, received_at = self._queue.get(timeout=0.2)
                except queue.Empty:
                    now = time.monotonic()
                    if received_samples == 0 and now - last_audio_status_at >= 3:
                        self._on_status("尚未收到输入音频")
                        last_audio_status_at = now
                    continue
                # 配置在取到音频后发生变化时，先丢弃这一块，下一轮应用新设置。
                if not self._is_configuration_current(active_generation):
                    continue
                if error:
                    logging.error("系统音频采集失败：%s", error)
                    self._on_error("无法采集音频，请检查所选设备后重新开始。")
                    self._stop_event.wait()
                    break
                new_parts = [frames] if frames is not None else []
                # 合并推理期间积累的音频，不逐个补做过期的触发点。
                # 每轮最多推进两秒，八秒窗口保留至少六秒重叠，音频不被跳过。
                merge_limit = max(1, 2 * SystemAudioCapture.sample_rate // SystemAudioCapture.block_size)
                for _ in range(merge_limit - 1):
                    try:
                        more, more_error, more_received_at = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if more_error:
                        logging.error("系统音频采集失败：%s", more_error)
                        self._on_error("无法采集音频，请检查所选设备后重新开始。")
                        self._stop_event.wait()
                        break
                    if more is not None:
                        new_parts.append(more)
                        received_at = more_received_at
                if self._stop_event.is_set():
                    break
                new_samples = sum(item.size for item in new_parts)
                if new_samples:
                    buffer = np.concatenate([buffer, *new_parts])
                    received_samples += new_samples
                if not new_samples:
                    continue

                options, generation = self._get_configuration()
                if generation != active_generation:
                    continue
                source_language = options.source_language
                target_language = options.target_language
                if received_samples - quality_decoded_until < quality_update_samples:
                    continue
                desired_start = max(0, received_samples - self.window_samples)
                # 音频按采集顺序处理；窗口长度不再依赖文字确认进度。
                trim_to = max(buffer_start, desired_start)
                if trim_to > buffer_start:
                    buffer = buffer[trim_to - buffer_start:]
                    buffer_start = trim_to
                quality_decoded_until = received_samples
                inference_started = time.perf_counter()
                try:
                    result = recognizer.transcribe(buffer, source_language)
                except Exception:
                    logging.exception("滚动窗口识别失败，设备=%s", recognizer.actual_device)
                    if not self._stop_event.is_set():
                        self._on_error(
                            "GPU 识别失败，可改选 CPU 后重试。"
                            if recognizer.actual_device == "cuda"
                            else "识别失败，请检查模型与源语言设置。"
                        )
                    continue
                if self._stop_event.is_set() or not self._is_configuration_current(generation):
                    continue
                inference_finished = time.perf_counter()
                inference_seconds = inference_finished - inference_started
                logging.info(
                    "滚动识别：设备=%s/%s，触发周期=%.2f秒，音频区间=%.2f～%.2f秒，推理=%.3f秒，待处理约=%.2f秒",
                    recognizer.actual_device, recognizer.actual_compute_type,
                    quality_update_samples / SystemAudioCapture.sample_rate,
                    buffer_start / SystemAudioCapture.sample_rate, received_samples / SystemAudioCapture.sample_rate,
                    inference_seconds, self._queue.qsize() * SystemAudioCapture.block_size / SystemAudioCapture.sample_rate,
                )
                detected_language = result.language or source_language
                updates = transcript.update(
                    result.words,
                    buffer_start / SystemAudioCapture.sample_rate,
                    received_samples / SystemAudioCapture.sample_rate,
                )
                for block_id, text in updates:
                    with self._lock:
                        self._translation_sequence += 1
                        sequence = self._translation_sequence
                    if self._on_update:
                        last_word_end = max((word.end for word in result.words), default=None)
                        last_word_received_at = (
                            None if last_word_end is None else
                            received_at - (buffer.size / SystemAudioCapture.sample_rate - last_word_end)
                        )
                        self._on_update(SourceUpdate(
                            sequence, block_id, text, detected_language,
                            received_samples / SystemAudioCapture.sample_rate,
                            received_at, inference_started, inference_finished, last_word_received_at,
                        ))
                    else:
                        self._on_source(sequence, text, detected_language, block_id)
                    if detected_language != target_language:
                        self._submit_translation(
                            sequence,
                            text,
                            detected_language,
                            target_language,
                            block_id,
                            generation,
                            options.translation_backend,
                        )
        except Exception as error:  # 交给界面展示具体失败原因
            if not self._stop_event.is_set():
                logging.exception("识别会话初始化失败")
                self._on_error("无法开始识别，请检查模型与计算设备后重试。")
                self._stop_event.wait()
        finally:
            if capture:
                capture.stop(wait=False)
            if self._capture is capture:
                self._capture = None
            self._translation_pool.shutdown(wait=False, cancel_futures=True)
            self._notify_stopped()

    def _on_audio(self, frames: np.ndarray) -> None:
        if not self._stop_event.is_set():
            self._queue.put((frames, None, time.perf_counter()))

    def _on_capture_error(self, error: Exception) -> None:
        self._queue.put((None, error, time.perf_counter()))

    def _notify_stopped(self) -> None:
        """只向界面发送一次停止通知，避免后台线程结束时重复刷新状态。"""
        with self._stopped_callback_lock:
            if self._stopped_callback_sent:
                return
            self._stopped_callback_sent = True
        self._on_stopped()

    def _get_languages(self) -> tuple[str | None, str]:
        with self._lock:
            return self._source_language, self._target_language

    def _submit_translation(
        self,
        sequence,
        text,
        source_language,
        target_language,
        block_id,
        generation,
        translation_backend,
    ) -> None:
        """同一字幕段只保留最新待翻译版本，已完成段落不被后续段落覆盖。"""
        text = remove_subtitle_credit(text).strip()
        if not text:
            return
        with self._lock:
            self._pending_translations[block_id] = (
                sequence,
                text,
                source_language,
                target_language,
                generation,
                translation_backend,
            )
            if self._translation_running or self._stop_event.is_set():
                return
            self._translation_running = True
        try:
            self._translation_pool.submit(self._translate_pending)
        except RuntimeError:
            if not self._stop_event.is_set():
                raise

    def _translate_pending(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                if not self._pending_translations:
                    self._translation_running = False
                    return
                block_id = next(iter(self._pending_translations))
                (
                    sequence,
                    text,
                    source_language,
                    target_language,
                    generation,
                    translation_backend,
                ) = self._pending_translations.pop(block_id)
            try:
                service = TranslationService(translation_backend)
                translated = service.translate(text, source_language, target_language)
            except Exception as error:
                if not self._stop_event.is_set():
                    logging.exception("字幕翻译失败")
                    self._on_error("翻译暂不可用；原文识别仍在继续。")
                continue
            if not self._stop_event.is_set() and self._is_configuration_current(generation):
                self._on_translation(sequence, translated, target_language)
