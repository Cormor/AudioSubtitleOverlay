"""Contract tests using only a fake sherpa native boundary, no model downloads."""

from collections import deque
from dataclasses import FrozenInstanceError
import gc
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import numpy as np

from asr_backends import SenseVoiceRecognizer, TextHypothesis, ZipformerRecognizer
from engine_models import ONNX_MODELS


class FakeStream:
    def __init__(self):
        self.accepted = []
        self.pending = 0
        self.finished_calls = 0
        self.result = SimpleNamespace(text="", lang="")
        self.text = ""
        self.endpoint = False

    def accept_waveform(self, rate, samples):
        if self.finished_calls:
            raise AssertionError("Cannot feed a finished stream")
        if rate != 16000 or samples.dtype != np.float32 or samples.ndim != 1:
            raise AssertionError("Incorrect audio format at native boundary")
        self.accepted.append(samples.copy())
        self.pending += 1

    def input_finished(self):
        self.finished_calls += 1


class FakeOnline:
    def __init__(self, outcomes=(), final_text=None):
        self.outcomes = deque(outcomes)
        self.final_text = final_text
        self.streams = []
        self.resets = []

    def create_stream(self):
        stream = FakeStream()
        self.streams.append(weakref.ref(stream))
        return stream

    def is_ready(self, stream):
        return stream.pending > 0

    def decode_stream(self, stream):
        stream.pending -= 1
        if self.outcomes:
            stream.text, stream.endpoint = self.outcomes.popleft()
        if stream.finished_calls and self.final_text is not None:
            stream.text = self.final_text

    def get_result(self, stream):
        return stream.text

    def is_endpoint(self, stream):
        return stream.endpoint

    def reset(self, stream):
        self.resets.append(weakref.ref(stream))
        stream.text = ""
        stream.endpoint = False


class FakeOffline:
    def __init__(self):
        self.text = "<|zh|><|NEUTRAL|><|Speech|>你好<|withitn|>"
        self.language = ""
        self.decode_calls = 0

    def create_stream(self):
        return FakeStream()

    def decode_stream(self, stream):
        self.decode_calls += 1
        stream.result = SimpleNamespace(text=self.text, lang=self.language)


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep even test temporaries inside the task's E-drive workspace.
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="model-stubs-",
            dir=os.environ.get("ASR_BACKEND_TEST_TMPDIR"),
        )
        cls.model_path = Path(cls._tmp.name)
        for info in ONNX_MODELS.values():
            for name in info["files"]:
                (cls.model_path / name).parent.mkdir(parents=True, exist_ok=True)
                (cls.model_path / name).touch()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.online = FakeOnline()
        self.offline = FakeOffline()
        self.online_configs = []
        self.offline_configs = []

        def online_factory(**kwargs):
            self.online_configs.append(kwargs)
            return self.online

        def offline_factory(**kwargs):
            self.offline_configs.append(kwargs)
            return self.offline

        module = SimpleNamespace(
            OnlineRecognizer=SimpleNamespace(from_transducer=online_factory),
            OfflineRecognizer=SimpleNamespace(from_sense_voice=offline_factory),
        )
        self.mock_module = patch.dict(sys.modules, {"sherpa_onnx": module})
        self.mock_module.start()
        self.addCleanup(self.mock_module.stop)

    def streaming(self, outcomes=(), final_text=None):
        self.online.outcomes = deque(outcomes)
        self.online.final_text = final_text
        backend = ZipformerRecognizer(str(self.model_path)).load()
        self.addCleanup(backend.close)
        return backend

    def test_auto_language_tag_is_read_even_when_text_has_no_tags(self):
        self.offline.text = "你好"
        self.offline.language = "<|zh|>"
        backend = SenseVoiceRecognizer(str(self.model_path)).load()
        try:
            result = backend.transcribe(np.ones(1600, dtype=np.float32))
            self.assertEqual(result.language, "zh")
            self.assertEqual(result.text, "你好")
        finally:
            backend.close()

    def test_ctc_stream_uses_native_ctc_constructor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "data").mkdir()
            (path / "data/tokens.txt").touch()
            (path / "ctc-chunk-16-left-64.int8.onnx").touch()
            module = sys.modules["sherpa_onnx"]
            def factory(**kwargs):
                self.online_configs.append(kwargs)
                return self.online
            module.OnlineRecognizer.from_zipformer2_ctc = factory
            backend = ZipformerRecognizer(str(path)).load()
            try:
                self.assertIn("model", self.online_configs[-1])
                self.assertNotIn("encoder", self.online_configs[-1])
                self.assertTrue(self.online_configs[-1]["enable_endpoint_detection"])
            finally:
                backend.close()

    def test_incremental_audio_is_fed_once_and_only_text_changes_rev(self):
        backend = self.streaming((("你", False), ("你", False), ("你好", False)))
        chunks = [np.full(800, value, dtype=np.float32) for value in (0.1, 0.2, 0.3)]
        first = backend.accept_audio(chunks[0])
        repeated = backend.accept_audio(chunks[1])
        third = backend.accept_audio(chunks[2])
        self.assertEqual([(x.text, x.revision) for x in first + third], [("你", 1), ("你好", 2)])
        self.assertEqual(repeated, [])
        stream = self.online.streams[0]()
        np.testing.assert_array_equal(np.concatenate(stream.accepted), np.concatenate(chunks))
        self.assertAlmostEqual(third[0].audio_end, 0.15)

    def test_endpoint_finalizes_once_then_advances_segment(self):
        backend = self.streaming((("你好", False), ("你好", True), ("再见", True)))
        chunk = np.ones(800, dtype=np.float32)
        events = backend.accept_audio(chunk) + backend.accept_audio(chunk) + backend.accept_audio(chunk)
        self.assertEqual([(e.segment_id, e.revision, e.final) for e in events],
                         [(0, 1, False), (0, 2, True), (1, 1, True)])
        self.assertEqual(len(self.online.streams), 1)
        self.assertEqual(len(self.online.resets), 2)
        self.assertAlmostEqual(events[-1].audio_start, 0.1)

    def test_one_large_input_keeps_audio_after_an_endpoint(self):
        backend = self.streaming((("第一句", True), ("第二句", False)))
        samples = np.arange(3200, dtype=np.float32) / 3200
        events = backend.accept_audio(samples)
        self.assertEqual([(e.segment_id, e.text, e.final) for e in events],
                         [(0, "第一句", True), (1, "第二句", False)])
        stream = self.online.streams[0]()
        np.testing.assert_array_equal(np.concatenate(stream.accepted), samples)

    def test_final_includes_unsubmitted_tail_and_flushes_native_cache(self):
        backend = self.streaming((("hello", False), ("hello", False)), "hello world")
        original = np.full(800, 0.25, dtype=np.float32)
        remaining = np.full(480, 0.5, dtype=np.float32)
        backend.accept_audio(original)
        old_stream = self.online.streams[0]()
        events = backend.accept_audio(remaining, final=True)
        self.assertEqual([(x.text, x.final) for x in events], [("hello world", True)])
        self.assertEqual(old_stream.finished_calls, 1)
        np.testing.assert_array_equal(old_stream.accepted[0], original)
        np.testing.assert_array_equal(old_stream.accepted[1], remaining)
        self.assertEqual(len(old_stream.accepted[2]), int(0.66 * 16000))
        self.assertFalse(np.any(old_stream.accepted[2]))
        self.assertAlmostEqual(events[-1].audio_end, 1280 / 16000)
        self.assertEqual(backend.accept_audio([], final=True), [])
        self.assertEqual(len(self.online.streams), 2)

    def test_final_without_new_audio_recovers_last_word(self):
        backend = self.streaming((("the", False),), "the end")
        backend.accept_audio(np.ones(1000, dtype=np.float32))
        events = backend.accept_audio([], final=True)
        self.assertEqual([(x.text, x.final) for x in events], [("the end", True)])

    def test_silence_and_empty_results_produce_no_fake_subtitles(self):
        backend = self.streaming((("", False), ("", True)))
        self.assertEqual(backend.accept_audio([], final=True), [])
        self.assertEqual(backend.accept_audio(np.zeros(800, dtype=np.float32)), [])
        self.assertEqual(backend.accept_audio(np.zeros(800, dtype=np.float32)), [])
        self.assertEqual(backend.accept_audio([], final=True), [])

    def test_reset_discards_cache_without_reloading_or_fake_final(self):
        backend = self.streaming((("旧段", False), ("新段", False)))
        backend.accept_audio(np.ones(800, dtype=np.float32))
        old_ref = self.online.streams[0]
        backend.reset()
        gc.collect()
        self.assertIsNone(old_ref())
        event = backend.accept_audio(np.ones(800, dtype=np.float32))[0]
        self.assertEqual((event.text, event.segment_id, event.revision, event.final), ("新段", 1, 1, False))
        self.assertEqual(len(self.online_configs), 1)
        self.assertAlmostEqual(event.audio_start, 0.05)

    def test_close_releases_stream_and_reload_starts_fresh(self):
        backend = self.streaming((("旧段", False),))
        backend.accept_audio(np.ones(800, dtype=np.float32))
        stream_ref = self.online.streams[0]
        backend.close()
        gc.collect()
        self.assertIsNone(stream_ref())
        self.assertIsNone(backend._recognizer)
        with self.assertRaises(RuntimeError):
            backend.accept_audio(np.ones(800, dtype=np.float32))
        backend.load()
        self.online.outcomes.append(("新段", False))
        event = backend.accept_audio(np.ones(800, dtype=np.float32))[0]
        self.assertEqual((event.segment_id, event.revision), (0, 1))
        self.assertEqual(len(self.online_configs), 2)

    def test_close_waits_for_native_decode_before_releasing_owner(self):
        backend = self.streaming((("完成", False),))
        entered = Event()
        proceed = Event()
        closing = Event()
        closed = Event()
        failures = []
        original_decode = self.online.decode_stream

        def slow_decode(stream):
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("Test did not release native decode")
            original_decode(stream)

        def decode():
            try:
                backend.accept_audio(np.ones(800, dtype=np.float32))
            except Exception as exc:
                failures.append(exc)

        def close():
            closing.set()
            backend.close()
            closed.set()

        self.online.decode_stream = slow_decode
        worker = Thread(target=decode)
        closer = Thread(target=close)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            closer.start()
            self.assertTrue(closing.wait(1))
            self.assertFalse(closed.wait(0.05))
            self.assertIsNotNone(backend._recognizer)
        finally:
            proceed.set()
            worker.join(3)
            if closer.ident is not None:
                closer.join(3)
        self.assertEqual(failures, [])
        self.assertTrue(closed.is_set())
        self.assertIsNone(backend._recognizer)

    def test_sensevoice_removes_metadata_and_reports_no_word_timestamps(self):
        backend = SenseVoiceRecognizer(str(self.model_path)).load()
        self.addCleanup(backend.close)
        result = backend.transcribe(np.ones(800, dtype=np.float32))
        self.assertEqual((result.text, result.language, result.words), ("你好", "zh", []))
        self.assertFalse(backend.capabilities.streaming)
        self.assertFalse(backend.capabilities.word_timestamps)
        self.assertEqual(self.offline_configs[0]["provider"], "cpu")
        self.assertFalse(self.offline_configs[0]["debug"])

    def test_sensevoice_language_override_reconfigures_and_close_can_reload(self):
        backend = SenseVoiceRecognizer(str(self.model_path), language="en").load()
        self.addCleanup(backend.close)
        self.offline.text = "test"
        backend.transcribe(np.ones(800, dtype=np.float32), source_language="ja")
        self.assertEqual(self.offline_configs[-1]["language"], "ja")
        backend.transcribe(np.ones(800, dtype=np.float32), source_language="auto")
        self.assertEqual(self.offline_configs[-1]["language"], "auto")
        backend.close()
        self.assertIsNone(backend._recognizer)
        backend.load()
        self.assertEqual(self.offline_configs[-1]["language"], "en")

    def test_unsupported_languages_and_threads_fail_before_native_calls(self):
        for cls in (SenseVoiceRecognizer, ZipformerRecognizer):
            with self.assertRaises(ValueError):
                cls(str(self.model_path), language="de")
            for value in (0, 9, 1.5, True):
                with self.assertRaises(ValueError):
                    cls(str(self.model_path), cpu_threads=value)
        backend = SenseVoiceRecognizer(str(self.model_path)).load()
        self.addCleanup(backend.close)
        with self.assertRaises(ValueError):
            backend.transcribe(np.ones(800, dtype=np.float32), source_language="fr")
        self.assertEqual(self.offline.decode_calls, 0)

    def test_invalid_audio_does_not_reach_native_decoder(self):
        backend = self.streaming()
        for samples in (np.zeros((2, 3)), np.array([np.nan]), np.array([np.inf])):
            with self.assertRaises(ValueError):
                backend.accept_audio(samples)
        self.assertEqual(self.online.streams[0]().accepted, [])

    def test_hypotheses_are_immutable(self):
        result = TextHypothesis(1, 2, "test", None, False, 0.0, 0.1)
        with self.assertRaises(FrozenInstanceError):
            result.text = "changed"


if __name__ == "__main__":
    unittest.main()
