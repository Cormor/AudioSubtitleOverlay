"""系统音频、语音识别和翻译的后台流水线。"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable

import numpy as np

from audio_capture import SystemAudioCapture
from process_audio_capture import PROCESS_AUDIO_DEVICE, ProcessAudioCapture
from transcription import WhisperRecognizer
from rolling_transcript import RollingTranscript
from runtime_workers import ManagedRecognizer, ManagedTranslator as TranslationService, WorkerCancelled, WorkerClosed, WorkerCrashed, WorkerTimeout
from subtitle_events import SourceUpdate, TranslationContext, TranslationUpdate, source_text_extends
from runtime_trace import get_runtime_trace
from text_processing import remove_subtitle_credit
from model_manager import find_local_model, ensure_model_available, validate_model_language
from download_manager import DownloadWaitCancelled


class _AudioQueue(queue.Queue):
    """Bound captured samples even when different sources use different blocks."""
    def __init__(self, maximum_samples):
        super().__init__(maxsize=2048)
        self.maximum_samples = maximum_samples
        self._samples = 0
        self._epoch = 0
        self._dropped = 0

    def _put(self, item):
        super()._put(item)
        if item[0] is not None:
            self._samples += item[0].size

    def _get(self):
        item = super()._get()
        if item[0] is not None:
            self._samples -= item[0].size
        return item

    @property
    def pending_samples(self):
        with self.mutex:
            return self._samples

    @property
    def dropped_samples(self):
        with self.mutex:
            return self._dropped

    def put_audio(self, frames, received_at):
        discarded_samples = max(0, frames.size - self.maximum_samples)
        frames = frames[-self.maximum_samples:].copy() if discarded_samples else frames
        with self.not_full:
            overloaded = self._samples + frames.size > self.maximum_samples or self._qsize() >= self.maxsize
            if overloaded:
                while self._qsize():
                    old = self._get()[0]
                    if old is not None:
                        discarded_samples += old.size
            if discarded_samples:
                self._epoch += 1
                self._dropped += discarded_samples
            self._put((frames, None, received_at, self._epoch))
            self.unfinished_tasks += 1
            self.not_empty.notify()
        return discarded_samples


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
    beam_size: int = 1
    condition_on_previous_text: bool = False
    temperature_schedule: str = "0"
    translation_enabled: bool = True
    window_seconds: int = 8
    update_interval: float = 0.1
    repetition_penalty: float = 1.0
    no_repeat_ngram_size: int = 0
    no_speech_threshold: float = 0.6
    vad_silence_ms: int = 400


def recognition_parameters(options):
    return {name:getattr(options,name) for name in (
        "beam_size","condition_on_previous_text","temperature_schedule",
        "window_seconds","update_interval","repetition_penalty",
        "no_repeat_ngram_size","no_speech_threshold","vad_silence_ms")}


def translation_required(options: PipelineOptions, detected_language: str | None) -> bool:
    source = options.source_language or detected_language
    return bool(options.translation_enabled and source and options.target_language
                and source != options.target_language)


def translation_only_change(previous: PipelineOptions, current: PipelineOptions) -> bool:
    return replace(current, target_language=previous.target_language,
                   translation_enabled=previous.translation_enabled,
                   translation_backend=previous.translation_backend) == previous


_GPU_DEVICE_MODES = frozenset(("自动（优先GPU）", "NVIDIA GPU（float16）", "NVIDIA GPU（int8_float16）"))


def _text_backend(model_key):
    return model_key == "sensevoice-int8" or model_key.startswith("zipformer-")


def _new_recognizer(options, on_status, *, cancel_wait=None):
    return ManagedRecognizer(options, on_status, cancel_wait=cancel_wait)


def recognition_window_start(received_samples, buffer_start, silence_boundary, *,
                             sample_rate=16_000, maximum_samples=128_000):
    """Only advance at a confirmed silence boundary, never inside continuous speech.

    If no safe pause has been observed, preserve the original 8-second window.
    The boundary comes from the merger's committed word timing, not from the
    most recent confirmed character (which may be in the middle of a sentence).
    """
    maximum_start = max(0, received_samples - maximum_samples)
    return max(buffer_start, maximum_start, int(silence_boundary * sample_rate))


def _model_source_key(options: PipelineOptions) -> tuple[str, str]:
    """返回实际模型来源；指定目录时模型大小只用于界面显示。"""
    model_path = options.model_path.strip()
    if _text_backend(options.model_size):
        return options.model_size, model_path + ":" + str(options.source_language)
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
    if _text_backend(options.model_size):
        return False
    if active_options.device_mode == options.device_mode:
        return False
    if recognizer.actual_device == "cuda":
        if options.device_mode in {"NVIDIA GPU（float16）", "NVIDIA GPU（int8_float16）"}:
            desired = "int8_float16" if "int8_float16" in options.device_mode else "float16"
            return getattr(recognizer, "actual_compute_type", "float16") != desired
        return options.device_mode not in _GPU_DEVICE_MODES
    if recognizer.actual_device == "cpu":
        return options.device_mode != "CPU（int8）"
    return True


@dataclass(frozen=True)
class _TranslationRequest:
    source: SourceUpdate
    target_language: str
    backend: str
    queued_at: float

    @property
    def cache_key(self):
        return (self.source.translation_epoch, self.source.language,
                self.target_language, self.backend, self.source.text)


class LivePipeline:
    """按有界重叠窗口识别，向独立显示模块提交段落版本。"""

    window_samples = 128_000
    # GPU 最小触发间隔为一百毫秒；推理期间到达的连续音频合并处理。
    gpu_quality_update_samples = 1_600
    # CPU 单次推理可能超过 500 ms，不启动即时通道，避免队列持续积压。
    cpu_quality_update_samples = 8_000
    # 相同翻译错误的上报间隔；连续失败不刷屏，也不冲垮日志。
    translation_error_interval = 60.0
    translation_partial_min_characters = 4
    translation_local_interval = 0.5
    translation_online_interval = 1.5
    translation_cache_characters = 65536

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
        on_metrics: Callable[[dict], None] | None = None,
        on_translation_context: Callable[[TranslationContext], None] | None = None,
        on_translation_update: Callable[[TranslationUpdate], None] | None = None,
    ) -> None:
        self.options = options
        self._on_status = on_status
        self._on_source = on_source
        self._on_update = on_update
        self._on_reset = on_reset
        self._on_metrics = on_metrics
        self._last_metrics_at = 0.0
        self._last_audio_meter_at = 0.0
        self._last_signal_at = 0.0
        self._phase = None
        self._pipeline_id = uuid.uuid4().hex[:12]
        self._active_capture_id = ""
        self._capture_times = OrderedDict()
        self._trace = get_runtime_trace()
        self._last_inference_seconds = None
        self._on_translation = on_translation
        self._on_translation_context = on_translation_context
        self._on_translation_update = on_translation_update
        # Callback publication is serialized; this lock never spans model RPC.
        self._publication_lock = threading.RLock()
        self._source_session_id = 0
        self._translation_sources = OrderedDict()
        self._translation_lineages = {}
        self._translation_results = OrderedDict()
        self._translation_result_characters = 0
        self._translation_wakeup = threading.Event()
        self._translation_next_partial_at = 0.0
        self._translation_final_streak = 0
        self._translation_overloaded = False
        self._on_error = on_error
        self._on_stopped = on_stopped
        self._queue = _AudioQueue(15 * SystemAudioCapture.sample_rate)
        self._capture_error = None
        self._configuration_queue: queue.Queue[PipelineOptions] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: SystemAudioCapture | ProcessAudioCapture | None = None
        self._lock = threading.Lock()
        self._stopped_callback_lock = threading.Lock()
        self._stopped_callback_sent = False
        self._configuration_generation = 0
        self._source_language = options.source_language
        self._target_language = options.target_language
        self._latest_source_language = options.source_language
        self._translation_sequence = 0
        self._pending_translations = OrderedDict()
        self._translation_running = False
        self._translation_failures = 0
        self._last_translation_error_at = 0.0
        self._translation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="翻译")
        self._translation_service = None
        self._translation_service_backend = None

    def start(self) -> None:
        """启动后台流水线。"""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("识别流水线已经运行")
        self._stop_event.clear()
        self._publish_translation_context()
        self._emit_phase("preparing")
        self._trace.record("pipeline_started", pipeline_id=self._pipeline_id,
                           model=self.options.model_size, language=self.options.source_language)
        with self._stopped_callback_lock:
            self._stopped_callback_sent = False
        self._thread = threading.Thread(
            target=self._run,
            name="音频识别流水线",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止；工作线程释放模型后才通知界面，避免新旧模型重叠。"""
        self._stop_event.set()
        self._translation_wakeup.set()
        capture = self._capture
        if capture:
            capture.stop(wait=False)
        self._translation_pool.shutdown(wait=False, cancel_futures=True)
        if self._thread is None:
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

    def update_options(self, options: PipelineOptions, *, force_translation_refresh=False) -> bool:
        """在运行中提交新配置，后台线程会合并并应用最新的一组设置。"""
        with self._publication_lock:
            with self._lock:
                unchanged = options == self.options
                previous_options = self.options
            if unchanged and not force_translation_refresh:
                self._publish_translation_context()
                return False
            with self._lock:
                self.options = options
                self._source_language = options.source_language
                self._target_language = options.target_language
                self._configuration_generation += 1
                generation = self._configuration_generation
                self._pending_translations.clear()
                if unchanged:
                    # Identical policy content can be reused, with the new
                    # epoch carried by a newly built TranslationUpdate.
                    self._translation_results = OrderedDict(
                        ((generation, *key[1:]), value)
                        for key, value in self._translation_results.items())
                else:
                    self._translation_results.clear()
                    self._translation_result_characters = 0
                self._translation_next_partial_at = 0.0
                refresh_sources = (list(reversed(self._translation_sources.values()))
                                   if translation_only_change(previous_options, options) else [])
            self._publish_translation_context()
            for source in refresh_sources:
                # This remains inside the publication transaction: an ASR
                # final cannot be overwritten by an older refresh snapshot.
                refreshed = replace(source, translation_epoch=generation)
                self._publish_update(refreshed)
                self._submit_translation(refreshed.sequence, refreshed.text, refreshed.language,
                                         options.target_language, refreshed.block_id,
                                         generation, options.translation_backend)
        self._translation_wakeup.set()
        logging.info("识别参数已提交：代次=%s，模型=%s，参数=%s",
                     generation,options.model_size,recognition_parameters(options))
        self._configuration_queue.put(options)
        return True

    def refresh_translation_context(self) -> bool:
        """Renew an invalidated UI context even when debounced options match."""
        with self._publication_lock:
            options, _ = self._get_configuration()
            return self.update_options(options, force_translation_refresh=True)

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
        capture: SystemAudioCapture | ProcessAudioCapture | None = None
        recognizer: WhisperRecognizer | None = None
        active_options: PipelineOptions | None = None
        active_generation = -1
        failed_generation = -1
        buffer = np.empty(0, dtype=np.float32)
        buffer_start = 0
        received_samples = 0
        quality_decoded_until = 0
        transcript = RollingTranscript()
        text_session = None
        seen_dropped = 0
        active_audio_epoch = 0
        candidate = None
        last_audio_status_at = time.monotonic()
        worker_recoveries = 0

        def recover_recognizer(error):
            nonlocal recognizer, text_session, capture, active_options, active_generation
            nonlocal buffer, buffer_start, received_samples, quality_decoded_until, transcript, candidate, worker_recoveries
            if not isinstance(error, (WorkerClosed, WorkerCrashed, WorkerTimeout)):
                return False
            if self._stop_event.is_set():
                return True
            if worker_recoveries >= 1:
                self._on_error("识别服务连续中断，字幕已停止；请点击重新开始。")
                self._stop_event.set()
                return True
            worker_recoveries += 1
            if capture is not None:
                capture.stop(wait=True)
                capture = self._capture = None
            if recognizer is not None:
                recognizer.close()
            recognizer = candidate = text_session = None
            active_options = None
            active_generation = -1
            self._clear_audio_queue()
            buffer = np.empty(0, dtype=np.float32)
            buffer_start = received_samples = quality_decoded_until = 0
            transcript = RollingTranscript()
            self._reset_source_session(advance_configuration=True)
            self._on_status("识别服务已中断，正在重新准备；原配置保持不变。")
            self._emit_phase("preparing")
            return True

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

                    if active_options is not None and configuration_changed and not translation_only_change(active_options, options):
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
                        if text_session is not None:
                            text_session.reset()
                        last_audio_status_at = time.monotonic()
                        self._reset_source_session()

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
                        self._emit_phase("preparing")
                        # 新模型开始加载后不再继续使用旧识别器；失败时等待配置变化，避免重复加载。
                        if recognizer is not None and hasattr(recognizer, "close"):
                            recognizer.close()
                        recognizer = None
                        text_session = None
                        candidate = None
                        try:
                            def abandon_preparation():
                                current_options, _ = self._get_configuration()
                                return (self._stop_event.is_set()
                                        or not translation_only_change(options, current_options))

                            candidate = _new_recognizer(options, self._on_status,
                                                        cancel_wait=abandon_preparation)
                            candidate.load()
                            # Preparation can be abandoned for a different model;
                            # normal decoder/translation updates must not poison
                            # an already loaded service's cancellation callback.
                            if hasattr(candidate, "set_cancel_wait"):
                                candidate.set_cancel_wait(self._stop_event.wait)
                        except (DownloadWaitCancelled, WorkerCancelled):
                            if candidate is not None and hasattr(candidate, "close"):
                                candidate.close()
                            candidate = None
                            continue
                        except Exception as error:
                            if candidate is not None and hasattr(candidate, "close"):
                                candidate.close()
                            candidate = None
                            failed_generation = generation
                            logging.exception("识别设置加载失败")
                            if not self._stop_event.is_set() and self._is_configuration_current(generation):
                                self._on_error(f"新的识别设置无法使用：{error}")
                            continue
                        if self._stop_event.is_set():
                            if hasattr(candidate, "close"):
                                candidate.close()
                            candidate = None
                            break
                        if not self._is_configuration_current(generation):
                            current_options, current_generation = self._get_configuration()
                            if translation_only_change(options, current_options):
                                options, generation = current_options, current_generation
                            else:
                                if hasattr(candidate, "close"):
                                    candidate.close()
                                candidate = None
                                continue
                        recognizer = candidate
                        if options.model_size == "sensevoice-int8":
                            from recognition_sessions import SegmentedTextSession
                            text_session = SegmentedTextSession(recognizer, options.source_language,
                                                                silence_ms=options.vad_silence_ms)
                        elif options.model_size.startswith("zipformer-"):
                            text_session = recognizer
                        failed_generation = -1

                    if text_session is not None and options.model_size == "sensevoice-int8" and configuration_changed and not model_changed and not translation_only_change(active_options, options):
                        from recognition_sessions import SegmentedTextSession
                        text_session = SegmentedTextSession(recognizer, options.source_language,
                                                            silence_ms=options.vad_silence_ms)
                    active_options = options
                    active_generation = generation
                    requested_samples = int(options.update_interval * SystemAudioCapture.sample_rate)
                    quality_update_samples = max(
                        requested_samples,
                        self.gpu_quality_update_samples if recognizer.actual_device == "cuda"
                        else self.cpu_quality_update_samples,
                    )
                    last_audio_status_at = time.monotonic()
                    logging.info("识别配置已生效：代次=%s，模型=%s，源语言=%s，目标语言=%s，翻译开关=%s，实际触发间隔=%s秒，参数=%s",
                                 generation,options.model_size,options.source_language or "auto",options.target_language,
                                 options.translation_enabled,quality_update_samples/SystemAudioCapture.sample_rate,
                                 recognition_parameters(options))
                    if capture is None:
                        self._on_status("正在连接音频设备……")
                        capture = (
                            ProcessAudioCapture(options.audio_device)
                            if options.audio_device == PROCESS_AUDIO_DEVICE
                            else SystemAudioCapture(options.audio_device)
                        )
                        self._capture = capture
                        capture_id = uuid.uuid4().hex[:12]
                        self._active_capture_id = capture_id
                        capture.start(
                            lambda frames, c=capture, key=capture_id: self._on_audio(
                                frames, capture=c, capture_id=key),
                            self._on_capture_error,
                            on_ready=lambda name, active_capture=capture: self._on_capture_ready(
                                name, active_capture),
                        )
                    elif self._is_configuration_current(generation):
                        # The existing capture does not repeat on_ready when only
                        # decoder parameters change. Acknowledge actual application.
                        if text_session is not None:
                            self._on_status("设置已生效；继续使用 CPU 本地识别与当前音频来源。")
                        else:
                            self._on_status(
                                f"设置已生效；搜索 {options.beam_size}，窗口 {options.window_seconds} 秒，"
                                f"更新间隔 {quality_update_samples/SystemAudioCapture.sample_rate:g} 秒；继续监听当前音频来源。")
                    continue

                try:
                    frames, error, received_at, audio_epoch = self._queue.get(timeout=0.2)
                except queue.Empty:
                    now = time.monotonic()
                    if (
                        received_samples == 0
                        and not isinstance(capture, ProcessAudioCapture)
                        and now - last_audio_status_at >= 3
                    ):
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
                if self._capture_error is not None:
                    self._on_error("音频来源已断开，请重新选择设备后停止并重新开始。")
                    self._stop_event.wait()
                    break
                new_parts = [frames] if frames is not None else []
                # 把推理期间收到的全部音频合并到下一次窗口，不逐个重放过期触发点。
                while True:
                    try:
                        more, more_error, more_received_at, more_epoch = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if more_error:
                        logging.error("系统音频采集失败：%s", more_error)
                        self._on_error("无法采集音频，请检查所选设备后重新开始。")
                        self._stop_event.wait()
                        break
                    if more is not None:
                        if more_epoch != audio_epoch:
                            # A producer overflow occurred inside this drain.
                            # Never concatenate speech from opposite sides of it.
                            received_samples += sum(part.size for part in new_parts)
                            new_parts = []
                            audio_epoch = more_epoch
                        new_parts.append(more)
                        received_at = more_received_at
                if self._stop_event.is_set():
                    break
                new_samples = sum(item.size for item in new_parts)
                if audio_epoch != active_audio_epoch:
                    lost = self._dropped_samples - seen_dropped
                    seen_dropped = self._dropped_samples
                    active_audio_epoch = audio_epoch
                    received_samples += lost
                    buffer = np.empty(0, dtype=np.float32)
                    buffer_start = received_samples
                    transcript = RollingTranscript()
                    if text_session is not None:
                        text_session.reset()
                    self._reset_source_session()
                    self._on_status(f"识别暂时跟不上音频，已跳过 {lost / 16000:.1f} 秒积压；可选择更轻的模型。")
                if new_samples:
                    if text_session is None:
                        buffer = np.concatenate([buffer, *new_parts])
                    received_samples += new_samples
                if not new_samples:
                    continue

                options, generation = self._get_configuration()
                if generation != active_generation:
                    if text_session is None or active_options is None or not translation_only_change(active_options, options):
                        continue
                source_language = options.source_language
                target_language = options.target_language
                if text_session is not None:
                    began = time.perf_counter()
                    try:
                        updates = text_session.accept_audio(np.concatenate(new_parts))
                    except (WorkerClosed, WorkerCrashed, WorkerTimeout) as error:
                        recover_recognizer(error)
                        continue
                    finished = time.perf_counter()
                    if self._stop_event.is_set():
                        continue
                    current_options, current_generation = self._get_configuration()
                    if not translation_only_change(options, current_options):
                        continue
                    # Text sessions have already consumed/committed this audio.
                    # Translation preferences do not invalidate that recognition.
                    options, generation = current_options, current_generation
                    target_language = options.target_language
                    for hypothesis in updates:
                        with self._lock:
                            if not translation_only_change(options, self.options):
                                break
                            options = self.options
                            generation = self._configuration_generation
                            target_language = options.target_language
                            detected = options.source_language or hypothesis.language
                            self._latest_source_language = detected
                            if not translation_required(options, detected):
                                self._pending_translations.clear()
                            self._translation_sequence += 1
                            sequence = self._translation_sequence
                        self._publish_update(SourceUpdate(
                            sequence, hypothesis.segment_id, hypothesis.text, detected,
                            received_samples / 16000, received_at, began, finished, None,
                            session_id=self._source_session_id, revision=hypothesis.revision,
                            final=hypothesis.final, pipeline_id=self._pipeline_id,
                            translation_epoch=generation,
                            capture_id=self._capture_times.get(received_at, self._active_capture_id)))
                        if translation_required(options, detected):
                            self._submit_translation(sequence, hypothesis.text, detected, target_language,
                                                     hypothesis.segment_id, generation, options.translation_backend)
                    self._report_metrics(new_parts, recognizer, finished - began)
                    continue
                if received_samples - quality_decoded_until < quality_update_samples:
                    continue
                desired_start = recognition_window_start(
                    received_samples, buffer_start, transcript.safe_decode_start,
                    sample_rate=SystemAudioCapture.sample_rate,
                    maximum_samples=int(options.window_seconds * SystemAudioCapture.sample_rate),
                )
                # 按采集顺序推进；仅在确认的停顿中裁去已结束语音，连续句子不提前裁窗。
                trim_to = max(buffer_start, desired_start)
                if trim_to > buffer_start:
                    buffer = buffer[trim_to - buffer_start:]
                    buffer_start = trim_to
                quality_decoded_until = received_samples
                inference_started = time.perf_counter()
                try:
                    result = recognizer.transcribe(
                        buffer,
                        source_language,
                        beam_size=options.beam_size,
                        condition_on_previous_text=options.condition_on_previous_text,
                        temperature_schedule=options.temperature_schedule,
                        repetition_penalty=options.repetition_penalty,
                        no_repeat_ngram_size=options.no_repeat_ngram_size,
                        no_speech_threshold=options.no_speech_threshold,
                        vad_silence_ms=options.vad_silence_ms,
                    )
                except (WorkerClosed, WorkerCrashed, WorkerTimeout) as error:
                    recover_recognizer(error)
                    continue
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
                self._report_metrics(new_parts, recognizer, inference_seconds)
                logging.info(
                    "滚动识别：设备=%s/%s，触发周期=%.2f秒，音频区间=%.2f～%.2f秒，推理=%.3f秒，待处理约=%.2f秒",
                    recognizer.actual_device, recognizer.actual_compute_type,
                    quality_update_samples / SystemAudioCapture.sample_rate,
                    buffer_start / SystemAudioCapture.sample_rate, received_samples / SystemAudioCapture.sample_rate,
                    inference_seconds, self._queue.pending_samples / SystemAudioCapture.sample_rate,
                )
                detected_language = source_language or result.language
                word_spans = (tuple((word.text, buffer_start / 16000 + word.start,
                                     buffer_start / 16000 + word.end) for word in result.words)
                              if self._trace.enabled and self._trace.include_text else ())
                self._trace.record("recognition", pipeline_id=self._pipeline_id,
                                   capture_id=self._capture_times.get(received_at, self._active_capture_id),
                                   audio_start=buffer_start / 16000, audio_end=received_samples / 16000,
                                   received_at=received_at, inference_started_at=inference_started,
                                   inference_finished_at=inference_finished, word_spans=word_spans)
                with self._lock:
                    self._latest_source_language = detected_language
                    if not translation_required(options, detected_language):
                        self._pending_translations.clear()
                updates = transcript.update(
                    result.words,
                    buffer_start / SystemAudioCapture.sample_rate,
                    received_samples / SystemAudioCapture.sample_rate,
                )
                # Closing a block may not change its already published text.
                # Publish that state transition so final translation can flush.
                changed_ids = {block_id for block_id, _ in updates}
                with self._lock:
                    closed_unchanged = [(block_id, source.text)
                                        for block_id, source in self._translation_sources.items()
                                        if block_id < transcript.block_id and not source.final
                                        and block_id not in changed_ids]
                for block_id, text in [*updates, *closed_unchanged]:
                    with self._lock:
                        self._translation_sequence += 1
                        sequence = self._translation_sequence
                    last_word_end = max((word.end for word in result.words), default=None)
                    last_word_received_at = (
                        None if last_word_end is None else
                        received_at - (buffer.size / SystemAudioCapture.sample_rate - last_word_end)
                    )
                    self._publish_update(SourceUpdate(
                        sequence, block_id, text, detected_language,
                        received_samples / SystemAudioCapture.sample_rate,
                        received_at, inference_started, inference_finished, last_word_received_at,
                        session_id=self._source_session_id, pipeline_id=self._pipeline_id,
                        capture_id=self._capture_times.get(received_at, self._active_capture_id),
                        word_spans=word_spans, revision=sequence,
                        final=block_id < transcript.block_id, translation_epoch=generation,
                    ))
                    if translation_required(options, detected_language):
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
                capture.stop(wait=True)
            if self._capture is capture:
                self._capture = None
            # Only the translation executor changes service ownership. Stop its
            # producer first; its worker RPC observes this cancellation event.
            self._stop_event.set()
            self._translation_wakeup.set()
            self._translation_pool.shutdown(wait=True, cancel_futures=True)
            translator = self._translation_service
            self._translation_service = None
            if translator is not None and hasattr(translator, "close"):
                translator.close()
            if recognizer is not None and hasattr(recognizer, "close"):
                recognizer.close()
            if candidate is not None and candidate is not recognizer and hasattr(candidate, "close"):
                candidate.close()
            candidate = None
            recognizer = None
            text_session = None
            self._trace.record("pipeline_stopped", pipeline_id=self._pipeline_id)
            self._notify_stopped()

    def _translation_context_locked(self):
        return TranslationContext(self._pipeline_id, self._source_session_id,
                                  self._configuration_generation,
                                  self.options.target_language,
                                  self.options.translation_enabled,
                                  self.options.source_language)

    def _publish_translation_context(self):
        with self._publication_lock:
            with self._lock:
                context = self._translation_context_locked()
            if self._on_translation_context:
                self._on_translation_context(context)

    def _reset_source_session(self, *, advance_configuration=False):
        with self._publication_lock:
            with self._lock:
                if advance_configuration:
                    self._configuration_generation += 1
                self._source_session_id += 1
                self._pending_translations.clear()
                self._translation_sources.clear()
                self._translation_lineages.clear()
                self._translation_results.clear()
                self._translation_result_characters = 0
                self._translation_next_partial_at = 0.0
            if self._on_reset:
                self._on_reset()
            self._publish_translation_context()
        self._translation_wakeup.set()

    def _remember_source_locked(self, update):
        previous = self._translation_sources.get(update.block_id)
        if previous is not None and previous.sequence > update.sequence:
            return False
        if (previous is None or previous.language != update.language
                or previous.source_id != update.source_id
                or previous.session_id != update.session_id
                or not source_text_extends(previous.text, update.text, previous_final=previous.final)):
            self._translation_lineages[update.block_id] = update.sequence
            self._pending_translations.pop(update.block_id, None)
        self._translation_sources[update.block_id] = update
        while len(self._translation_sources) > 64:
            block_id, _ = self._translation_sources.popitem(last=False)
            self._translation_lineages.pop(block_id, None)
            if self._pending_translations.pop(block_id, None) is not None:
                self._translation_overloaded = True
        return True

    def _publish_update(self, update):
        with self._publication_lock:
            with self._lock:
                if (self._stop_event.is_set()
                        or update.translation_epoch != self._configuration_generation
                        or update.session_id != self._source_session_id):
                    return
                if not self._remember_source_locked(update):
                    return
            self._trace.record_source(update)
            if self._on_update:
                self._on_update(update)
            else:
                self._on_source(update.sequence, update.text, update.language, update.block_id)

    def _emit_phase(self, phase):
        if phase != self._phase:
            self._phase = phase
            if self._on_metrics:
                self._on_metrics({"phase": phase})

    def _on_capture_ready(self, name, capture):
        self._on_status(name if isinstance(capture, ProcessAudioCapture)
                        else f"正在识别；已打开采集设备：{name}")
        self._emit_phase("waiting_audio")

    def _report_metrics(self, parts, recognizer, inference_seconds):
        if self._on_metrics:
            if inference_seconds >= .001:
                self._last_inference_seconds = inference_seconds
            now = time.monotonic()
            if now - self._last_metrics_at < .2:
                return
            self._last_metrics_at = now
            samples = sum(a.size for a in parts)
            rms = float(np.sqrt(sum(float(np.dot(a, a)) for a in parts) / max(1, samples)))
            phase = "recognizing" if time.monotonic() - self._last_signal_at < 2 else "waiting_audio"
            self._on_metrics(dict(input_rms=rms, inference_seconds=self._last_inference_seconds,
                                  backlog_seconds=self._queue.pending_samples / 16000,
                                  actual_device=recognizer.actual_device,
                                  actual_compute_type=recognizer.actual_compute_type,
                                  dropped_seconds=self._dropped_samples / 16000, phase=phase))

    @property
    def _dropped_samples(self):
        return self._queue.dropped_samples

    def _on_audio(self, frames: np.ndarray, *, capture=None, capture_id="") -> None:
        if not self._stop_event.is_set():
            received_at = time.perf_counter()
            if self._trace.enabled:
                self._capture_times[received_at] = capture_id
                while len(self._capture_times) > 4096:
                    self._capture_times.popitem(last=False)
                self._trace.record_audio(frames, received_at, capture_id,
                                         getattr(capture, "latest_timing", None))
            self._queue.put_audio(frames, received_at)
            now = time.monotonic()
            if self._on_metrics and now - self._last_audio_meter_at >= .2:
                self._last_audio_meter_at = now
                rms = float(np.sqrt(np.mean(frames * frames))) if frames.size else 0.0
                if rms > .00001:
                    self._last_signal_at = now
                phase = "recognizing" if now - self._last_signal_at < 2 else "waiting_audio"
                self._on_metrics(dict(input_rms=rms, phase=phase,
                                      backlog_seconds=self._queue.pending_samples / 16000))

    def _on_capture_error(self, error: Exception) -> None:
        self._capture_error = error
        try:
            self._queue.put_nowait((None, error, time.perf_counter(), self._queue._epoch))
        except queue.Full:
            pass

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
        self, sequence, text, source_language, target_language, block_id,
        generation, translation_backend,
    ) -> None:
        """Coalesce each block, without debouncing a continuously growing draft."""
        if not remove_subtitle_credit(text).strip():
            return
        with self._lock:
            if (self._stop_event.is_set() or generation != self._configuration_generation
                    or not translation_required(self.options, self._latest_source_language or source_language)
                    or not translation_required(self.options, source_language)):
                return
            source = self._translation_sources.get(block_id)
            if source is not None and source.sequence > sequence:
                return
            if source is None or source.sequence != sequence:
                # Compatibility for callers that submit translations directly.
                source = SourceUpdate(sequence, block_id, text, source_language,
                                      0, 0, 0, 0, None,
                                      session_id=self._source_session_id,
                                      revision=sequence, pipeline_id=self._pipeline_id,
                                      translation_epoch=generation)
                self._remember_source_locked(source)
            request = _TranslationRequest(source, target_language,
                                          translation_backend, time.perf_counter())
            if not source.final and sum(not char.isspace() for char in text) < self.translation_partial_min_characters:
                return
            self._pending_translations[block_id] = request
            overflow = self._translation_overloaded or len(self._pending_translations) > 64
            self._translation_overloaded = False
            if len(self._pending_translations) > 64:
                self._pending_translations.popitem(last=False)
            start = not self._translation_running
            if start:
                self._translation_running = True
        self._translation_wakeup.set()
        if self._on_metrics:
            self._on_metrics({"translation_phase": "overloaded" if overflow else "translating"})
        if start:
            try:
                self._translation_pool.submit(self._translate_pending)
            except RuntimeError:
                with self._lock:
                    self._translation_running = False
                if not self._stop_event.is_set():
                    raise

    def _translation_allowed(self, source_language):
        with self._lock:
            return (translation_required(self.options, self._latest_source_language or source_language)
                    and translation_required(self.options, source_language))

    def _translation_cancelled(self, request, timeout=0):
        if self._stop_event.wait(timeout):
            return True
        with self._lock:
            return (request.source.translation_epoch != self._configuration_generation
                    or request.source.session_id != self._source_session_id)

    def _request_current_locked(self, request):
        source = request.source
        current = self._translation_sources.get(source.block_id)
        return (not self._stop_event.is_set()
                and source.pipeline_id == self._pipeline_id
                and source.session_id == self._source_session_id
                and source.translation_epoch == self._configuration_generation
                and request.target_language == self.options.target_language
                and request.backend == self.options.translation_backend
                and current is not None
                and source.source_id == current.source_id
                and source.language == current.language
                and source.sequence >= self._translation_lineages[source.block_id]
                and source.sequence <= current.sequence
                and bool(source.text)
                and source_text_extends(source.text, current.text, previous_final=source.final)
                and (not source.final or source.text == current.text)
                and translation_required(self.options, self._latest_source_language or source.language)
                and translation_required(self.options, source.language))

    def _next_translation_locked(self):
        now = time.monotonic()
        final = [(key, value) for key, value in self._pending_translations.items() if value.source.final]
        partial = [(key, value) for key, value in self._pending_translations.items() if not value.source.final]
        partial_ready = bool(partial) and now >= self._translation_next_partial_at
        if final and (self._translation_final_streak < 2 or not partial_ready):
            key, request = final[0]
            self._translation_final_streak += 1
        elif partial_ready:
            key, request = partial[0]
            self._translation_final_streak = 0
        else:
            return None, max(0.0, self._translation_next_partial_at - now)
        del self._pending_translations[key]
        return request, 0.0

    def _cache_translation_locked(self, request, translated):
        key = request.cache_key
        size = len(key[-1]) + len(translated)
        if size > self.translation_cache_characters:
            return
        previous = self._translation_results.pop(key, None)
        if previous is not None:
            self._translation_result_characters -= len(key[-1]) + len(previous)
        self._translation_results[key] = translated
        self._translation_result_characters += size
        while (len(self._translation_results) > 64
               or self._translation_result_characters > self.translation_cache_characters):
            old_key, old_value = self._translation_results.popitem(last=False)
            self._translation_result_characters -= len(old_key[-1]) + len(old_value)

    def _deliver_translation(self, request, translated, started, finished):
        with self._publication_lock:
            with self._lock:
                if not self._request_current_locked(request):
                    return
                self._cache_translation_locked(request, translated)
            source = request.source
            update = TranslationUpdate(
                sequence=source.sequence, block_id=source.block_id,
                source_text=source.text, source_language=source.language,
                target_language=request.target_language, text=translated,
                source_id=source.source_id, session_id=source.session_id,
                revision=source.revision, final=source.final,
                pipeline_id=source.pipeline_id, translation_epoch=source.translation_epoch,
                queued_at=request.queued_at, started_at=started, finished_at=finished)
            if self._on_translation_update:
                self._on_translation_update(update)
            else:
                self._on_translation(source.sequence, translated, request.target_language)

    def _trace_translation(self, kind, request, **fields):
        source = request.source
        self._trace.record(kind, pipeline_id=source.pipeline_id,
                           session_id=source.session_id,
                           translation_epoch=source.translation_epoch,
                           source_id=source.source_id, block_id=source.block_id,
                           sequence=source.sequence, revision=source.revision,
                           final=source.final, source_language=source.language,
                           target_language=request.target_language,
                           backend=request.backend, input_characters=len(source.text),
                           queued_at=request.queued_at, **fields)

    def _translate_pending(self) -> None:
        released = False
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    if not self._pending_translations:
                        self._translation_running = False
                        released = True
                        return
                    request, wait = self._next_translation_locked()
                    self._translation_wakeup.clear()
                    if request is not None:
                        current = self._request_current_locked(request)
                        cached = self._translation_results.get(request.cache_key)
                if request is None:
                    self._translation_wakeup.wait(wait)
                    continue
                if not current:
                    continue
                started = time.perf_counter()
                if cached is not None:
                    self._trace_translation("translation_cached", request,
                                            started_at=started, finished_at=started)
                    self._deliver_translation(request, cached, started, started)
                    continue
                interval = (self.translation_local_interval
                            if request.backend in {"仅使用本地 Argos", "仅使用本地翻译"}
                            else self.translation_online_interval)
                if not request.source.final:
                    with self._lock:
                        self._translation_next_partial_at = time.monotonic() + interval
                try:
                    if (self._translation_service is None
                            or self._translation_service_backend != request.backend):
                        previous_service = self._translation_service
                        if previous_service is not None and hasattr(previous_service, "close"):
                            previous_service.close()
                        service = TranslationService(request.backend, on_status=self._on_status)
                        if hasattr(service, "set_cancel_wait"):
                            service.set_cancel_wait(self._stop_event.wait)
                        self._translation_service = service
                        self._translation_service_backend = request.backend
                    else:
                        service = self._translation_service
                    if hasattr(service, "set_cancel_wait"):
                        # Configuration changes interrupt the obsolete RPC;
                        # normal source revisions must not restart slow MT.
                        service.set_cancel_wait(lambda timeout=0, owned=request:
                                                self._translation_cancelled(owned, timeout))
                    started = time.perf_counter()
                    self._trace_translation("translation_call", request, started_at=started)
                    translated = service.translate(remove_subtitle_credit(request.source.text).strip(),
                                                   request.source.language, request.target_language)
                except Exception as error:
                    self._trace_translation("translation_call_finished", request,
                                            started_at=started, finished_at=time.perf_counter(),
                                            success=False, error_type=type(error).__name__)
                    if isinstance(error, (WorkerCancelled, WorkerClosed, WorkerCrashed, WorkerTimeout)):
                        failed_service = self._translation_service
                        self._translation_service = None
                        self._translation_service_backend = None
                        if failed_service is not None and hasattr(failed_service, "close"):
                            failed_service.close()
                    with self._lock:
                        current = self._request_current_locked(request)
                    if not current:
                        continue
                    self._translation_failures += 1
                    if self._on_metrics and not self._stop_event.is_set():
                        self._on_metrics({"translation_phase": "error"})
                    now = time.monotonic()
                    if not self._stop_event.is_set():
                        if now - self._last_translation_error_at >= self.translation_error_interval:
                            self._last_translation_error_at = now
                            logging.exception("字幕翻译失败（连续第 %s 次；同类错误每 %s 秒至多上报一次）",
                                              self._translation_failures, int(self.translation_error_interval))
                            self._on_error(f"翻译暂不可用：{error}；原文识别仍在继续。")
                        else:
                            logging.debug("字幕翻译失败（节流中）：%s", error)
                    continue
                self._translation_failures = 0
                finished = time.perf_counter()
                self._trace_translation("translation_call_finished", request,
                                        started_at=started, finished_at=finished, success=True)
                self._deliver_translation(request, translated, started, finished)
        finally:
            if not released:
                with self._lock:
                    self._translation_running = False
