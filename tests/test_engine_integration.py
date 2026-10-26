import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace

import numpy as np

import pipeline
from pipeline import LivePipeline, PipelineOptions
from model_manager import _remote_model_files, model_definition, validate_model_language
from recognition_sessions import SegmentedTextSession


def options(**changes):
    values = dict(source_language="zh", target_language="zh", model_size="tiny",
                  device_mode="CPU（int8）", model_path="", audio_device="fixture",
                  translation_backend="仅使用本地翻译")
    values.update(changes)
    return PipelineOptions(**values)


class Capture:
    sample_rate = 16000
    block_size = 8000
    def __init__(self, *args):
        pass
    def start(self, on_audio, on_error, on_ready=None):
        on_audio(np.ones(8000, dtype=np.float32))
    def stop(self, wait=False):
        pass


class EngineIntegrationTests(unittest.TestCase):
    def test_translation_change_during_final_does_not_discard_consumed_source(self):
        from asr_backends import TextHypothesis
        entered, finish, ready = threading.Event(), threading.Event(), threading.Event()
        class Recognizer:
            actual_device, actual_compute_type = "cpu", "int8"
            def load(self):
                pass
            def accept_audio(self, frames):
                entered.set()
                finish.wait(2)
                return [TextHypothesis(0, 1, "已定稿", "zh", True, 0, .5)]
            def close(self):
                pass
            def reset(self):
                pass
        recognizer, events = Recognizer(), []
        selected = options(model_size="sensevoice-int8", translation_enabled=True)
        def update(event):
            events.append(event)
            ready.set()
        with patch.object(pipeline, "SystemAudioCapture", Capture), patch.object(
                pipeline, "_new_recognizer", return_value=recognizer), patch(
                "recognition_sessions.SegmentedTextSession", return_value=recognizer):
            runner = LivePipeline(selected, lambda _: None, lambda *a: None, lambda *a: None,
                                  lambda _: None, lambda: None, on_update=update)
            runner.start()
            self.assertTrue(entered.wait(1))
            runner.update_options(replace(selected, translation_enabled=False))
            finish.set()
            self.assertTrue(ready.wait(1))
            runner.stop()
            runner._thread.join(1)
        self.assertEqual([(e.text, e.final) for e in events], [("已定稿", True)])

    def test_text_detected_language_replaces_previous_language_before_translation(self):
        from asr_backends import TextHypothesis
        translated = threading.Event()
        class Recognizer:
            actual_device, actual_compute_type = "cpu", "int8"
            def load(self):
                pass
            def accept_audio(self, frames):
                return [TextHypothesis(0, 1, "当前是中文", "zh", True, 0, .5)]
            def close(self):
                pass
        class Translator:
            def __init__(self, *args, **kwargs):
                pass
            def translate(self, text, source, target):
                self_test.assertEqual((source, target), ("zh", "en"))
                return "translated"
        self_test, recognizer = self, Recognizer()
        with patch.object(pipeline, "SystemAudioCapture", Capture), patch.object(
                pipeline, "_new_recognizer", return_value=recognizer), patch(
                "recognition_sessions.SegmentedTextSession", return_value=recognizer), patch.object(
                pipeline, "TranslationService", Translator):
            runner = LivePipeline(options(model_size="sensevoice-int8", source_language=None, target_language="en"),
                                  lambda _: None, lambda *a: None, lambda *a: translated.set(),
                                  lambda _: None, lambda: None)
            runner._latest_source_language = "en"
            runner.start()
            self.assertTrue(translated.wait(1))
            runner.stop()
            runner._thread.join(1)

    def test_callbacks_already_queued_when_stopping_cannot_show_late_subtitles(self):
        from main import Application
        calls = []
        stopped = lambda: calls.append("stopped")
        app = SimpleNamespace(_session=1, _stopping=True, _pipeline_stopped=stopped)
        Application._dispatch_session(app, 1, lambda: calls.append("late source"))
        Application._dispatch_session(app, 1, stopped)
        self.assertEqual(calls, ["stopped"])

    def test_stop_callback_waits_for_native_decode_and_model_release(self):
        entered, finish, stopped = threading.Event(), threading.Event(), threading.Event()
        class Recognizer:
            actual_device = "cpu"
            actual_compute_type = "int8"
            closed = False
            def load(self):
                pass
            def transcribe(self, *args, **kwargs):
                entered.set()
                finish.wait(2)
                return SimpleNamespace(words=[], language="zh")
            def close(self):
                self.closed = True
        recognizer = Recognizer()
        observed = []
        def on_stopped():
            observed.append(recognizer.closed)
            stopped.set()
        with patch.object(pipeline, "SystemAudioCapture", Capture), patch.object(
                pipeline, "_new_recognizer", return_value=recognizer):
            runner = LivePipeline(options(), lambda _: None, lambda *a: self.fail("late source"),
                                  lambda *a: None, lambda _: None, on_stopped)
            runner.start()
            self.assertTrue(entered.wait(1))
            runner.stop()
            self.assertFalse(stopped.wait(.03))
            finish.set()
            self.assertTrue(stopped.wait(1))
            runner._thread.join(1)
        self.assertEqual(observed, [True])

    def test_streaming_text_reaches_source_update_without_word_timestamps(self):
        from asr_backends import TextHypothesis
        ready = threading.Event()
        class Recognizer:
            actual_device, actual_compute_type = "cpu", "int8"
            def load(self):
                pass
            def accept_audio(self, frames):
                self.size = frames.size
                return [TextHypothesis(0, 1, "正在识别", "zh", False, 0, .5),
                        TextHypothesis(0, 2, "识别完成", "zh", True, 0, .5)]
            def close(self):
                pass
        recognizer, events = Recognizer(), []
        def update(value):
            events.append(value)
            if value.final:
                ready.set()
        with patch.object(pipeline, "SystemAudioCapture", Capture), patch.object(
                pipeline, "_new_recognizer", return_value=recognizer):
            runner = LivePipeline(options(model_size="zipformer-zh-en-int8"), lambda _: None,
                                  lambda *a: None, lambda *a: None, lambda _: None,
                                  lambda: None, on_update=update)
            runner.start()
            self.assertTrue(ready.wait(1))
            runner.stop()
            runner._thread.join(1)
        self.assertEqual(recognizer.size, 8000)
        self.assertEqual([e.final for e in events], [False, True])
        self.assertTrue(all(e.last_word_received_at is None for e in events))
        self.assertEqual(events[-1].revision, 2)

    def test_capture_overload_is_bounded_and_counted(self):
        runner = LivePipeline(options(), lambda _: None, lambda *a: None,
                              lambda *a: None, lambda _: None, lambda: None)
        for _ in range(1000):
            runner._on_audio(np.ones(1600, dtype=np.float32))
        self.assertLessEqual(runner._queue.qsize() * 1600, 15 * 16000)
        self.assertGreater(runner._dropped_samples, 0)
        runner.stop()

    def test_vad_silence_skips_asr_and_preserves_onset_padding(self):
        recognizer = SimpleNamespace(transcribe=lambda *args: self.fail("silent audio decoded"))
        session = SegmentedTextSession(recognizer, "zh")
        session._vad = lambda *args: []
        for _ in range(100):
            self.assertEqual(session.accept_audio(np.zeros(8000, np.float32)), [])
        self.assertLessEqual(session.buffer.size, 4800)

    def test_text_only_final_commits_once_after_partial(self):
        recognizer = SimpleNamespace(transcribe=lambda *args: SimpleNamespace(text="你好", language="zh"))
        session = SegmentedTextSession(recognizer, "zh")
        session._vad = lambda *args: [{"start": 0, "end": 8000}]
        partial = session.accept_audio(np.ones(8000, np.float32))
        final = session.accept_audio(np.zeros(8000, np.float32))
        session._vad = lambda *args: []
        self.assertEqual(session.accept_audio(np.zeros(8000, np.float32)), [])
        self.assertFalse(partial[0].final)
        self.assertTrue(final[0].final)
        self.assertEqual(partial[0].segment_id, final[0].segment_id)
        self.assertGreater(final[0].revision, partial[0].revision)

    def test_language_restrictions_and_selective_quantized_download(self):
        for model in ["tiny.en", "base.en", "distil-large-v3", "zipformer-zh-en-int8"]:
            with self.assertRaises(ValueError):
                validate_model_language(model, "ja")
        validate_model_language("sensevoice-int8", "ja")
        _, files = _remote_model_files(model_definition("sensevoice-int8"))
        self.assertEqual({f.path for f in files}, {"model.int8.onnx", "tokens.txt"})
        self.assertTrue(all(f.sha256 and len(f.sha256) == 64 for f in files))

    def test_all_proxy_is_applied_to_https_but_not_loopback(self):
        import download_manager as dm
        from urllib.request import Request
        with patch.object(dm, "getproxies", return_value={"all": "http://127.0.0.1:7890"}), \
                patch.object(dm, "build_opener") as opener, patch.object(dm, "_stdlib_urlopen") as direct:
            dm.urlopen(Request("https://huggingface.co/model"), timeout=1)
            opener.return_value.open.assert_called_once()
            direct.assert_not_called()
            dm.urlopen(Request("http://127.0.0.1:12345/local"), timeout=1)
            direct.assert_called_once()


if __name__ == "__main__":
    unittest.main()
