"""识别质量参数向 faster-whisper 的传递测试。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

import numpy as np

from transcription import WhisperRecognizer


class _FakeModel:
    def __init__(self) -> None:
        self.options = None

    def transcribe(self, _audio, **options):
        self.options = options
        segment = SimpleNamespace(text="测试", start=0.0, end=1.0, words=None)
        return iter([segment]), SimpleNamespace(language="zh")


class TranscriptionOptionTests(unittest.TestCase):
    def test_quality_options_are_passed_to_decoder(self):
        recognizer = WhisperRecognizer("tiny", "CPU（int8）")
        model = _FakeModel()
        recognizer._model = model

        result = recognizer.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "zh",
            beam_size=5,
            condition_on_previous_text=True,
            temperature_schedule="0,0.2,0.4",
            lexicon_mode="计算机",
            custom_hotwords="项目专名",
        )

        self.assertEqual(result.text, "测试")
        self.assertEqual(model.options["beam_size"], 5)
        self.assertEqual(model.options["best_of"], 5)
        self.assertEqual(model.options["temperature"], (0.0, 0.2, 0.4))
        self.assertTrue(model.options["condition_on_previous_text"])
        self.assertTrue(model.options["hotwords"].startswith("项目专名"))


if __name__ == "__main__":
    unittest.main()
