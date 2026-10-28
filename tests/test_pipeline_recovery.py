"""Failure recovery at the real pipeline boundary, with controlled services."""
import threading
import unittest
from unittest.mock import patch
from pipeline import LivePipeline
import pipeline
from runtime_workers import WorkerCrashed, WorkerTimeout, WorkerCancelled
from tests.test_engine_integration import options, Capture
from tests.test_pipeline_reconfiguration import _FakeCapture, _FakeRecognizer


def runner(**overrides):
    callbacks = dict(on_status=lambda _:None,on_source=lambda *a:None,on_translation=lambda *a:None,
                     on_error=lambda _:None,on_stopped=lambda:None)
    callbacks.update(overrides)
    return LivePipeline(options(source_language='en',target_language='zh'),**callbacks)


class PipelineRecoveryTests(unittest.TestCase):
    def test_sixty_five_pending_blocks_keep_latest_sixty_four(self):
        p=runner();p._translation_running=True
        try:
            for block in range(65):p._submit_translation(block,'hello','en','zh',block,0,'仅使用本地翻译')
            self.assertEqual(len(p._pending_translations),64)
            self.assertNotIn(0,p._pending_translations)
            self.assertIn(64,p._pending_translations)
        finally:p.stop()

    def test_translation_terminal_failure_recreates_service_for_next_block(self):
        services=[];ready=threading.Event()
        class Translator:
            def __init__(self,*a,**kw):self.closed=False;services.append(self)
            def translate(self,*a):
                if len(services)==1:raise WorkerTimeout('fixture timeout')
                return 'translated'
            def close(self):self.closed=True
        p=runner(on_translation=lambda *a:ready.set())
        p._translation_running=True
        for block in range(2):p._submit_translation(block,'hello','en','zh',block,0,'仅使用本地翻译')
        with patch.object(pipeline,'TranslationService',Translator):p._translate_pending()
        self.assertTrue(ready.is_set());self.assertEqual(len(services),2)
        self.assertTrue(services[0].closed)
        services[1].close();p.stop()

    def test_stop_during_translation_construction_closes_final_owner(self):
        entered,release,closed=threading.Event(),threading.Event(),threading.Event()
        class Translator:
            def __init__(self,*a,**kw):entered.set();release.wait(2);self.cancel=None
            def set_cancel_wait(self,callback):self.cancel=callback
            def translate(self,*a):
                if self.cancel(0):raise WorkerCancelled('stopped')
                return 'unused'
            def close(self):closed.set()
        class Recognizer(_FakeRecognizer):
            def close(self):pass
        p=runner()
        def factory(opts,status,**kw):return Recognizer(opts.model_size,opts.device_mode,opts.model_path,status)
        with patch.object(pipeline,'SystemAudioCapture',Capture),patch.object(pipeline,'_new_recognizer',factory),patch.object(pipeline,'TranslationService',Translator):
            p.start()
            p._submit_translation(1,'hello','en','zh',1,0,'仅使用本地翻译')
            self.assertTrue(entered.wait(1))
            p.stop();release.set();p._thread.join(3)
            self.assertFalse(p._thread.is_alive());self.assertTrue(closed.is_set())
            self.assertIsNone(p._translation_service)

    def test_asr_terminal_failure_rebuilds_once_and_resumes_source(self):
        services=[];ready=threading.Event()
        class Recognizer(_FakeRecognizer):
            def close(self):self.closed=True
            def transcribe(self,*a,**kw):
                if services.index(self)==0:raise WorkerCrashed('fixture crash')
                return super().transcribe(*a,**kw)
        def factory(opts,status,**kw):
            instance=Recognizer(opts.model_size,opts.device_mode,opts.model_path,status);services.append(instance);return instance
        p=runner(on_preview=lambda *a:ready.set(),on_update=lambda *a:ready.set())
        with patch.object(pipeline,'SystemAudioCapture',_FakeCapture),patch.object(pipeline,'_new_recognizer',factory):
            p.start()
            self.assertTrue(ready.wait(3))
            p.stop();p._thread.join(3)
        self.assertFalse(p._thread.is_alive());self.assertEqual(len(services),2)
        self.assertTrue(services[0].closed)

    def test_repeated_terminal_failure_stops_without_unbounded_reload(self):
        services=[];stopped=threading.Event();errors=[]
        class Recognizer(_FakeRecognizer):
            def close(self):pass
            def transcribe(self,*a,**kw):raise WorkerCrashed('repeat')
        def factory(opts,status,**kw):
            item=Recognizer(opts.model_size,opts.device_mode,opts.model_path,status);services.append(item);return item
        p=runner(on_stopped=stopped.set,on_error=errors.append)
        with patch.object(pipeline,'SystemAudioCapture',_FakeCapture),patch.object(pipeline,'_new_recognizer',factory):
            p.start();self.assertTrue(stopped.wait(3));p._thread.join(1)
        self.assertEqual(len(services),2);self.assertTrue(any('连续中断' in x for x in errors))
