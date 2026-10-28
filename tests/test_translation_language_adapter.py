import unittest
import sys
from types import ModuleType
from unittest.mock import Mock, patch
from translation import TranslationService, ArgosTranslator, _local_models_ready


class TranslationLanguageAdapterTests(unittest.TestCase):
    def test_cantonese_written_text_uses_installed_chinese_text_model(self):
        service=TranslationService('仅使用本地翻译')
        service._argos=Mock()
        service._argos.translate.return_value='Where are you?'
        self.assertEqual(service.translate('你喺邊度？','yue','en'),'Where are you?')
        service._argos.translate.assert_called_once_with('你喺邊度？','zh','en')

    def test_local_readiness_uses_alias_without_fetching_package_index(self):
        with patch('translation._find_installed_model',return_value='cached') as find:
            self.assertTrue(_local_models_ready('yue','en'))
            find.assert_called_once_with('zh','en')

    def test_chinese_target_keeps_recognized_text_without_model_lookup(self):
        service=TranslationService('仅使用本地翻译')
        service._argos=Mock()
        self.assertEqual(service.translate('你喺邊度？','yue','zh'),'你喺邊度？')
        service._argos.translate.assert_not_called()

    def test_online_policy_keeps_its_provider_with_supported_text_language(self):
        service=TranslationService('仅使用免费在线 MyMemory')
        service._online=Mock()
        service.translate('你好','yue','en')
        service._online.translate.assert_called_once_with('你好','zh','en')

    def test_chinese_model_receives_simplified_written_text(self):
        from types import SimpleNamespace
        package=SimpleNamespace()
        translator=ArgosTranslator()
        modules = {name: ModuleType(name) for name in ('ctranslate2', 'sentencepiece')}
        with patch.dict(sys.modules, modules), \
             patch('translation._model_package_pairs',return_value=[('zh','en',package)]), \
             patch.object(translator,'_ensure_model',return_value='cached'), \
             patch.object(translator,'_load_model',return_value='model'), \
             patch.object(translator,'_translate_with_model',return_value='water') as translate:
            translator.translate('我想飲水。','yue','en')
            translate.assert_called_once_with('model','我想饮水。')

    def test_package_index_uses_download_transport_all_proxy_rules(self):
        import json
        import tempfile
        from pathlib import Path
        import translation
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'[]'
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(translation,'_package_index',None), \
             patch.object(translation,'_package_index_failed_at',0), \
             patch.object(translation,'application_data_dir',return_value=Path(folder)), \
             patch('download_manager.getproxies',return_value={'all':'http://127.0.0.1:8123'}), \
             patch('download_manager.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            self.assertEqual(translation._load_package_index(),())
            handler=opener.call_args.args[0]
            self.assertEqual(handler.proxies['https'],'http://127.0.0.1:8123')
