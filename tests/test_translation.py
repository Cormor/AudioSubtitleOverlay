"""本地模型翻译和在线回退测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import translation
from translation import ArgosTranslator, TranslationService


class _FakeTokenizer:
    def encode(self, text, out_type=str):
        return [text]

    def decode(self, tokens):
        return "▁" + tokens[0]


class _FakeTranslator:
    def translate_batch(self, batches, **_options):
        return [SimpleNamespace(hypotheses=[[batches[0][0]]])]


class _FakeLocalTranslator:
    def __init__(self, value="本地译文", error=None):
        self.value = value
        self.error = error

    def translate(self, *_args):
        if self.error is not None:
            raise self.error
        return self.value


class _FakeOnlineTranslator:
    def __init__(self, value="在线译文"):
        self.value = value

    def translate(self, *_args):
        return self.value


class TranslationTests(unittest.TestCase):
    def test_local_model_decodes_without_leading_piece_marker(self):
        model = translation._LoadedTranslationModel(_FakeTokenizer(), _FakeTranslator())
        result = ArgosTranslator._translate_with_model(model, "测试。")
        self.assertEqual(result, "测试。")

    def test_model_root_requires_matching_language_and_core_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "translate-en_zh-1_9"
            (root / "model").mkdir(parents=True)
            (root / "metadata.json").write_text(
                json.dumps({"from_code": "en", "to_code": "zh"}),
                encoding="utf-8",
            )
            for relative in (
                "model/config.json",
                "model/model.bin",
                "sentencepiece.model",
            ):
                (root / relative).write_bytes(b"test")
            self.assertEqual(
                translation._find_model_root(Path(temporary), "en", "zh"),
                root,
            )
            self.assertIsNone(translation._find_model_root(Path(temporary), "zh", "en"))

    def test_local_first_falls_back_to_online(self):
        service = TranslationService("本地优先，失败转在线")
        service._argos = _FakeLocalTranslator(error=RuntimeError("模型未准备"))
        service._online = _FakeOnlineTranslator()
        self.assertEqual(service.translate("hello", "en", "zh"), "在线译文")

    def test_local_backend_uses_local_translator(self):
        service = TranslationService("仅使用本地翻译")
        service._argos = _FakeLocalTranslator()
        with patch.object(service, "_online", _FakeOnlineTranslator("不应调用")):
            self.assertEqual(service.translate("hello", "en", "zh"), "本地译文")

    def test_non_english_pair_can_use_english_pivot(self):
        entries = [
            {
                "from_code": "ja",
                "to_code": "en",
                "code": "translate-ja_en",
                "package_version": "1.1",
                "links": ["https://example.invalid/ja_en.argosmodel"],
            },
            {
                "from_code": "en",
                "to_code": "zh",
                "code": "translate-en_zh",
                "package_version": "1.9",
                "links": ["https://example.invalid/en_zh.argosmodel"],
            },
        ]
        with patch.object(translation, "_load_package_index", return_value=tuple(entries)):
            pairs = translation._model_package_pairs("ja", "zh")
        self.assertEqual([(item[0], item[1]) for item in pairs], [("ja", "en"), ("en", "zh")])


if __name__ == "__main__":
    unittest.main()
