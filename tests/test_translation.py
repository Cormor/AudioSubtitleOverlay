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


class TranslationBackendRoutingTests(unittest.TestCase):
    """按用户选择分流：在线-only 不得改道本地。"""

    def test_online_only_uses_online_even_when_local_models_installed(self):
        service = TranslationService("仅使用免费在线 MyMemory")
        service._argos = _FakeLocalTranslator("不应走本地")
        service._online = _FakeOnlineTranslator("在线译文")
        with patch("translation._local_models_ready", return_value=True):
            self.assertEqual(service.translate("hello", "en", "zh"), "在线译文")

    def test_local_first_prefers_local_only_when_ready(self):
        service = TranslationService("本地优先，失败转在线")
        service._argos = _FakeLocalTranslator("本地译文")
        service._online = _FakeOnlineTranslator("在线译文")
        with patch("translation._local_models_ready", return_value=True):
            self.assertEqual(service.translate("hello", "en", "zh"), "本地译文")
        with patch("translation._local_models_ready", return_value=False):
            self.assertEqual(service.translate("hello", "en", "zh"), "在线译文")

    def test_online_only_never_probes_local_readiness(self):
        """在线-only 不得做本地 readiness，更不得改道本地。"""
        service = TranslationService("仅使用免费在线 MyMemory")
        service._argos = _FakeLocalTranslator("不应走本地")
        service._online = _FakeOnlineTranslator("在线译文")
        with patch("translation._local_models_ready") as ready, patch(
            "translation._model_package_pairs"
        ) as pairs, patch("translation._load_package_index") as index:
            self.assertEqual(service.translate("hello", "en", "zh"), "在线译文")
            ready.assert_not_called()
            pairs.assert_not_called()
            index.assert_not_called()

    def test_local_models_ready_false_when_nothing_installed(self):
        """离线且无安装模型：返回 False，且不访问清单。"""
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            translation, "_translation_model_root", return_value=Path(temporary)
        ), patch("translation._load_package_index") as index, patch(
            "translation._model_package_pairs"
        ) as pairs:
            self.assertFalse(translation._local_models_ready("en", "zh"))
            self.assertFalse(translation._local_models_ready("ja", "zh"))
            index.assert_not_called()
            pairs.assert_not_called()

    def test_local_models_ready_never_loads_package_index(self):
        """readiness 只查安装目录，不得触发网络/清单发现。"""
        with patch("translation._model_package_pairs") as pairs, patch(
            "translation._load_package_index"
        ) as index:
            translation._local_models_ready("en", "zh")
            pairs.assert_not_called()
            index.assert_not_called()

    def test_local_models_ready_offline_pivot_when_both_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def _install(from_code: str, to_code: str):
                model_root = root / f"{from_code}_{to_code}" / "t"
                (model_root / "model").mkdir(parents=True)
                (model_root / "metadata.json").write_text(
                    json.dumps({"from_code": from_code, "to_code": to_code}),
                    encoding="utf-8",
                )
                for relative in (
                    "model/config.json",
                    "model/model.bin",
                    "sentencepiece.model",
                ):
                    (model_root / relative).write_bytes(b"t")
                return root / f"{from_code}_{to_code}"

            _install("ja", "en")
            _install("en", "zh")
            with patch.object(translation, "_translation_model_root", return_value=root):
                self.assertTrue(translation._local_models_ready("ja", "zh"))
                self.assertTrue(translation._local_models_ready("ja", "en"))
                # 只有 ja_en 时，en→zh 尚未安装则未就绪
                self.assertFalse(translation._local_models_ready("en", "ja"))


class OfflineModelTests(unittest.TestCase):
    def setUp(self):
        translation._package_index = None
        translation._package_index_failed_at = 0.0

    def tearDown(self):
        translation._package_index = None
        translation._package_index_failed_at = 0.0

    @staticmethod
    def _install_model(root: Path, from_code: str = "en", to_code: str = "zh") -> Path:
        model_root = root / f"{from_code}_{to_code}" / "translate-test-1"
        (model_root / "model").mkdir(parents=True)
        (model_root / "metadata.json").write_text(
            json.dumps({"from_code": from_code, "to_code": to_code}),
            encoding="utf-8",
        )
        for relative in ("model/config.json", "model/model.bin", "sentencepiece.model"):
            (model_root / relative).write_bytes(b"test")
        return model_root

    def test_installed_model_is_found_without_network(self):
        """已安装的本地模型不需要联网清单也能定位（离线承诺）。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._install_model(root)
            with patch.object(translation, "_translation_model_root", return_value=root), patch.object(
                translation, "_load_package_index", side_effect=RuntimeError("offline")
            ):
                package = translation._find_package("en", "zh")
            self.assertIsNotNone(package)
            self.assertEqual(package.urls, ())

    def test_ensure_model_reuses_installed_without_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = self._install_model(root)
            translator = ArgosTranslator()
            package = translation.TranslationPackage("en", "zh", "translate-en_zh", "installed", ())
            with patch.object(translation, "_translation_model_root", return_value=root):
                self.assertEqual(translator._ensure_model(package), expected)

    def test_ensure_model_without_urls_and_not_installed_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            translator = ArgosTranslator()
            package = translation.TranslationPackage("en", "zh", "translate-en_zh", "1", ())
            with patch.object(translation, "_translation_model_root", return_value=Path(temporary)):
                with self.assertRaises(RuntimeError):
                    translator._ensure_model(package)

    def test_package_index_falls_back_to_disk_cache_when_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "index.json"
            cache.write_text(
                json.dumps([{"from_code": "en", "to_code": "zh"}]),
                encoding="utf-8",
            )
            with patch.object(translation, "_package_index_cache_path", return_value=cache), patch.object(
                translation, "urlopen", side_effect=OSError("offline")
            ):
                index = translation._load_package_index()
            self.assertEqual(index[0]["from_code"], "en")

    def test_package_index_failure_is_backed_off(self):
        """清单读取失败后退避重试，不为每个字幕段都撞一次网络。"""
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "index.json"
            with patch.object(translation, "_package_index_cache_path", return_value=cache), patch.object(
                translation, "urlopen", side_effect=OSError("offline")
            ) as mocked:
                with self.assertRaises(RuntimeError):
                    translation._load_package_index()
                with self.assertRaises(RuntimeError):
                    translation._load_package_index()
                self.assertEqual(mocked.call_count, 1)

    def test_package_index_success_is_cached_to_disk(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps([{"from_code": "en", "to_code": "zh"}]).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "index.json"
            with patch.object(translation, "_package_index_cache_path", return_value=cache), patch.object(
                translation, "urlopen", return_value=_FakeResponse()
            ):
                translation._load_package_index()
            self.assertTrue(cache.is_file())


if __name__ == "__main__":
    unittest.main()
