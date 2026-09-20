"""识别运行中切换配置的隔离测试。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

import pipeline
from pipeline import LivePipeline, PipelineOptions
from transcription import RecognizedWord


class _FakeCapture:
    sample_rate = 16_000
    block_size = 8_000
    instances: list["_FakeCapture"] = []

    def __init__(self, device_name: str = "") -> None:
        self.device_name = device_name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_audio = None
        self.__class__.instances.append(self)

    def start(self, on_audio, on_error, on_ready=None) -> None:
        self._on_audio = on_audio

        def feed() -> None:
            if on_ready:
                on_ready(self.device_name)
            while not self._stop_event.is_set():
                on_audio(np.ones(self.block_size, dtype=np.float32))
                time.sleep(0.01)

        self._thread = threading.Thread(target=feed, daemon=True)
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop_event.set()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


class _FakeRecognizer:
    instances: list["_FakeRecognizer"] = []

    def __init__(self, model_size, device_mode, model_path="", on_status=None) -> None:
        self.model_size = model_size
        self.device_mode = device_mode
        self.model_path = model_path
        self.actual_device = (
            "cuda"
            if device_mode in {"自动（优先GPU）", "NVIDIA GPU（float16）"}
            else "cpu"
        )
        self.actual_compute_type = "float16" if self.actual_device == "cuda" else "int8"
        self.languages: list[str | None] = []
        self.decode_options: list[dict] = []
        self.loaded = False
        self.__class__.instances.append(self)

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, _audio, source_language, **kwargs) -> SimpleNamespace:
        self.languages.append(source_language)
        self.decode_options.append(kwargs)
        return SimpleNamespace(
            language=source_language or "en",
            words=[RecognizedWord(0.0, 0.8, "hello")],
        )


class _BurstCapture:
    sample_rate = 16_000
    block_size = 320

    def __init__(self, device_name: str = "") -> None:
        self.device_name = device_name

    def start(self, on_audio, on_error, on_ready=None) -> None:
        if on_ready:
            on_ready(self.device_name)
        # 在识别线程开始取数据前一次性放入六秒音频，模拟推理期间形成的积压。
        for _ in range(300):
            on_audio(np.ones(self.block_size, dtype=np.float32))

    def stop(self, wait: bool = False) -> None:
        return None


class _RecordingRecognizer(_FakeRecognizer):
    audio_sizes: list[int] = []

    def transcribe(self, audio, source_language, **kwargs) -> SimpleNamespace:
        self.__class__.audio_sizes.append(audio.size)
        return super().transcribe(audio, source_language, **kwargs)


class _FakeTranslationService:
    calls: list[tuple[str, str | None, str]] = []

    def __init__(self, _backend: str, on_status=None) -> None:
        pass

    def translate(self, text: str, _source: str | None, _target: str) -> str:
        self.__class__.calls.append((text, _source, _target))
        return text


class PipelineReconfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeCapture.instances.clear()
        _FakeRecognizer.instances.clear()
        _FakeTranslationService.calls.clear()

    @staticmethod
    def _options(**changes) -> PipelineOptions:
        options = PipelineOptions(
            source_language="en",
            target_language="en",
            model_size="tiny",
            device_mode="CPU（int8）",
            model_path="",
            audio_device="设备A",
            translation_backend="仅使用本地 Argos",
        )
        return replace(options, **changes)

    def test_backlogged_audio_is_processed_as_one_latest_window(self):
        _RecordingRecognizer.audio_sizes.clear()

        with patch.object(pipeline, "SystemAudioCapture", _BurstCapture), patch.object(
            pipeline, "WhisperRecognizer", _RecordingRecognizer
        ):
            runner = LivePipeline(
                self._options(),
                lambda _text: None,
                lambda *_args: None,
                lambda *_args: None,
                lambda _text: None,
                lambda: None,
            )
            runner.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not _RecordingRecognizer.audio_sizes:
                time.sleep(0.02)
            runner.stop()

        self.assertEqual(len(_RecordingRecognizer.audio_sizes), 1)
        self.assertEqual(_RecordingRecognizer.audio_sizes[0], 96_000)

    def test_update_options_keeps_only_latest_queued_configuration(self):
        runner = LivePipeline(
            self._options(),
            lambda _text: None,
            lambda *_args: None,
            lambda *_args: None,
            lambda _text: None,
            lambda: None,
        )
        runner.update_options(self._options(source_language="fr"))
        runner.update_options(self._options(source_language="de", model_size="base"))
        latest = runner._take_latest_configuration()
        self.assertEqual(latest.source_language, "de")
        self.assertEqual(latest.model_size, "base")
        runner._translation_pool.shutdown(wait=False, cancel_futures=True)

    def test_running_pipeline_applies_language_audio_and_model_changes(self):
        resets = []
        errors = []
        recognizer_ready = threading.Event()
        reset_ready = threading.Event()

        def on_reset() -> None:
            resets.append(True)
            reset_ready.set()

        with patch.object(pipeline, "SystemAudioCapture", _FakeCapture), patch.object(
            pipeline, "WhisperRecognizer", _FakeRecognizer
        ), patch.object(pipeline, "TranslationService", _FakeTranslationService):
            runner = LivePipeline(
                self._options(),
                lambda _text: None,
                lambda *_args: None,
                lambda *_args: None,
                errors.append,
                lambda: None,
                on_reset=on_reset,
            )
            runner.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _FakeRecognizer.instances and _FakeRecognizer.instances[0].languages:
                    recognizer_ready.set()
                    break
                time.sleep(0.02)
            self.assertTrue(recognizer_ready.is_set())

            runner.update_options(self._options(source_language="fr"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _FakeRecognizer.instances[0].languages[-1:] == ["fr"]:
                    break
                time.sleep(0.02)
            self.assertEqual(_FakeRecognizer.instances[0].languages[-1], "fr")

            runner.update_options(
                self._options(
                    source_language="de",
                    target_language="zh",
                    model_size="base",
                    device_mode="NVIDIA GPU（float16）",
                    audio_device="设备B",
                )
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if len(_FakeRecognizer.instances) >= 2 and any(
                    item.device_name == "设备B" for item in _FakeCapture.instances
                ):
                    break
                time.sleep(0.02)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any(
                target == "zh" for _, _, target in _FakeTranslationService.calls
            ):
                time.sleep(0.02)
            runner.stop()

        self.assertGreaterEqual(len(_FakeRecognizer.instances), 2)
        self.assertEqual(_FakeRecognizer.instances[-1].model_size, "base")
        self.assertEqual(_FakeRecognizer.instances[-1].device_mode, "NVIDIA GPU（float16）")
        self.assertTrue(any(item.device_name == "设备B" for item in _FakeCapture.instances))
        self.assertTrue(any(target == "zh" for _, _, target in _FakeTranslationService.calls))
        self.assertTrue(resets)
        self.assertEqual(errors, [])

    def test_equivalent_gpu_device_modes_do_not_reload_model(self):
        resets = []
        errors = []

        with patch.object(pipeline, "SystemAudioCapture", _FakeCapture), patch.object(
            pipeline, "WhisperRecognizer", _FakeRecognizer
        ):
            runner = LivePipeline(
                self._options(device_mode="NVIDIA GPU（float16）"),
                lambda _text: None,
                lambda *_args: None,
                lambda *_args: None,
                errors.append,
                lambda: None,
                on_reset=lambda: resets.append(True),
            )
            runner.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _FakeRecognizer.instances and _FakeRecognizer.instances[0].languages:
                    break
                time.sleep(0.02)
            self.assertTrue(_FakeRecognizer.instances)

            runner.update_options(self._options(device_mode="自动（优先GPU）"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not resets:
                time.sleep(0.02)
            time.sleep(0.2)
            runner.stop()

        self.assertEqual(len(_FakeRecognizer.instances), 1)
        self.assertEqual(len(_FakeCapture.instances), 1)
        self.assertTrue(resets)
        self.assertEqual(errors, [])

    def test_quality_options_apply_without_reloading_model(self):
        errors = []

        with patch.object(pipeline, "SystemAudioCapture", _FakeCapture), patch.object(
            pipeline, "WhisperRecognizer", _FakeRecognizer
        ):
            runner = LivePipeline(
                self._options(),
                lambda _text: None,
                lambda *_args: None,
                lambda *_args: None,
                errors.append,
                lambda: None,
            )
            runner.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _FakeRecognizer.instances and _FakeRecognizer.instances[0].decode_options:
                    break
                time.sleep(0.02)
            self.assertTrue(_FakeRecognizer.instances)

            runner.update_options(
                self._options(
                    beam_size=5,
                    condition_on_previous_text=True,
                    temperature_schedule="0,0.2,0.4",
                )
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _FakeRecognizer.instances[0].decode_options[-1].get("beam_size") == 5:
                    break
                time.sleep(0.02)
            runner.stop()

        self.assertEqual(len(_FakeRecognizer.instances), 1)
        self.assertEqual(len(_FakeCapture.instances), 1)
        self.assertEqual(_FakeRecognizer.instances[0].decode_options[-1]["beam_size"], 5)
        self.assertTrue(_FakeRecognizer.instances[0].decode_options[-1]["condition_on_previous_text"])
        self.assertEqual(
            _FakeRecognizer.instances[0].decode_options[-1]["temperature_schedule"],
            "0,0.2,0.4",
        )
        self.assertEqual(errors, [])

    def test_failed_model_change_waits_for_another_configuration(self):
        errors = []
        load_attempts = []

        class _FailingRecognizer(_FakeRecognizer):
            def load(self) -> None:
                load_attempts.append(self.model_size)
                if self.model_size == "base":
                    raise RuntimeError("测试模型不可用")
                super().load()

        with patch.object(pipeline, "SystemAudioCapture", _FakeCapture), patch.object(
            pipeline, "WhisperRecognizer", _FailingRecognizer
        ):
            runner = LivePipeline(
                self._options(),
                lambda _text: None,
                lambda *_args: None,
                lambda *_args: None,
                errors.append,
                lambda: None,
            )
            runner.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not _FakeCapture.instances:
                time.sleep(0.02)
            self.assertTrue(_FakeCapture.instances)

            runner.update_options(self._options(model_size="base", audio_device="设备B"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not errors:
                time.sleep(0.02)
            self.assertTrue(errors)
            time.sleep(0.25)
            self.assertEqual(load_attempts.count("base"), 1)

            runner.update_options(self._options())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(_FakeCapture.instances) < 2:
                time.sleep(0.02)
            runner.stop()

        self.assertGreaterEqual(len(_FakeCapture.instances), 2)


if __name__ == "__main__":
    unittest.main()
