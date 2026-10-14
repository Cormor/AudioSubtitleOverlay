from dataclasses import replace
from types import SimpleNamespace
import threading,time,unittest
from unittest.mock import patch
from pipeline import LivePipeline, PipelineOptions, translation_required, translation_only_change
from display_module import DisplayModule
from subtitle_layout import ReadingCursor
from transcription import WhisperRecognizer, RecognizedWord, clean_recognized_words
from text_processing import remove_subtitle_credit

def options(**changes):
    return replace(PipelineOptions('en','zh','tiny','CPU（int8）','','test audio','仅使用本地翻译'),**changes)

def pipeline(o,on_translation=lambda *a:None):
    return LivePipeline(o,lambda *a:None,lambda *a:None,on_translation,lambda *a:None,lambda:None)

class TranslationDisableTests(unittest.TestCase):
    def test_explicit_off_same_language_and_unknown_never_construct_backend(self):
        for o,language in ((options(translation_enabled=False),'en'),(options(source_language='zh'),'en'),(options(source_language=None),None),(options(source_language=None),'zh')):
            p=pipeline(o)
            with patch('pipeline.TranslationService') as backend:
                p._submit_translation(1,'hello',language,'zh',1,0,o.translation_backend)
                time.sleep(.02)
                backend.assert_not_called()
                self.assertFalse(p._pending_translations)
            p.stop()

    def test_switch_off_discards_inflight_and_clears_queued_work(self):
        entered=threading.Event();release=threading.Event();outputs=[]
        class Backend:
            def __init__(self,*a,**k):pass
            def translate(self,*a):entered.set();release.wait(2);return 'translation'
        p=pipeline(options(),lambda *a:outputs.append(a))
        with patch('pipeline.TranslationService',Backend):
            p._submit_translation(1,'hello','en','zh',1,0,p.options.translation_backend)
            self.assertTrue(entered.wait(1))
            p._submit_translation(2,'next','en','zh',2,0,p.options.translation_backend)
            p.update_options(replace(p.options,translation_enabled=False))
            release.set();p._translation_pool.shutdown(wait=True)
            self.assertFalse(outputs);self.assertFalse(p._pending_translations)
        p.stop()

    def test_switch_back_on_resumes_new_translation(self):
        outputs=[];p=pipeline(options(translation_enabled=False),lambda *a:outputs.append(a))
        class Backend:
            def __init__(self,*a,**k):pass
            def translate(self,*a):return 'new translation'
        p.update_options(replace(p.options,translation_enabled=True))
        with patch('pipeline.TranslationService',Backend):
            p._submit_translation(2,'hello','en','zh',1,1,p.options.translation_backend)
            p._translation_pool.shutdown(wait=True)
        self.assertEqual(outputs,[(2,'new translation','zh')]);p.stop()

    def test_auto_same_language_blocks_previously_queued_foreign_work(self):
        p=pipeline(options(source_language=None));p._latest_source_language='zh'
        with patch('pipeline.TranslationService') as backend:
            p._submit_translation(1,'hello','en','zh',1,0,p.options.translation_backend)
            backend.assert_not_called()
        p.stop()

    def test_translation_changes_are_not_recognition_restarts(self):
        a=options()
        for b in (replace(a,translation_enabled=False),replace(a,translation_backend='other'),replace(a,target_language='ja')):
            self.assertTrue(translation_only_change(a,b))
        self.assertFalse(translation_only_change(a,replace(a,beam_size=5)))
        self.assertFalse(translation_only_change(a,replace(a,source_language='zh')))

    def test_ui_display_hides_clears_and_rejects_late_results(self):
        m=DisplayModule('en','zh');m.apply_source(1,'hello','en',1);m.apply_translation(1,'你好','zh')
        m.set_translation_enabled(False)
        self.assertEqual(m.snapshot().entries,(('hello',''),));self.assertFalse(m.snapshot().translation_visible)
        self.assertFalse(m.apply_translation(1,'old','zh'))
        m.set_translation_enabled(True);self.assertTrue(m.snapshot().translation_visible)
        self.assertTrue(m.apply_translation(1,'new','zh'))

    def test_live_toggle_does_not_reset_capture_model_or_source_history(self):
        import pipeline as module
        from test_pipeline_reconfiguration import _FakeCapture, _FakeRecognizer
        _FakeCapture.instances.clear();_FakeRecognizer.instances.clear();resets=[]
        p=pipeline(options(source_language='en',target_language='en'));p._on_reset=lambda:resets.append(True)
        with patch.object(module,'SystemAudioCapture',_FakeCapture),patch.object(module,'WhisperRecognizer',_FakeRecognizer):
            p.start()
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and (not _FakeRecognizer.instances or len(_FakeRecognizer.instances[0].languages)<2):time.sleep(.01)
            self.assertEqual(len(_FakeRecognizer.instances),1)
            before=len(_FakeRecognizer.instances[0].languages)
            p.update_options(replace(p.options,translation_enabled=False))
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and len(_FakeRecognizer.instances[0].languages)<before+3:time.sleep(.01)
            self.assertGreaterEqual(len(_FakeRecognizer.instances[0].languages),before+3)
            self.assertEqual(len(_FakeCapture.instances),1);self.assertEqual(len(_FakeRecognizer.instances),1);self.assertFalse(resets)
            p.stop();p._thread.join(2)

class LatencyAndCreditTests(unittest.TestCase):
    def test_zero_dwell_immediately_selects_latest_without_timer(self):
        c=ReadingCursor();c.select(1,1,0,0)
        self.assertEqual(c.select(200,1,.01,0),(199,None))

    def test_realtime_burst_does_not_replay_entire_history(self):
        c=ReadingCursor();c.select(3,3,0,.129)
        start,_=c.select(150,3,.01,.129)
        self.assertGreaterEqual(start,144)
        for i in range(1,4):c.select(150,3,i*.129,.129)
        self.assertEqual(c.start,147)

    def test_truncated_credit_and_cross_word_credit_are_removed_before_rolling(self):
        self.assertEqual(remove_subtitle_credit('你好 幕byTestAuthor 世界'),'你好 世界')
        words=[RecognizedWord(0,.2,'字'),RecognizedWord(.2,.4,'幕'),RecognizedWord(.4,.6,'by'),RecognizedWord(.6,.8,'TestAuthor'),RecognizedWord(1,2,'真实内容')]
        clean=clean_recognized_words(words)
        self.assertEqual(clean,[words[-1]])
        from rolling_transcript import RollingTranscript
        transcript=RollingTranscript();transcript.update(clean,0,2)
        self.assertNotIn('by',transcript._pending_display_text())

    def test_bracketed_truncated_chinese_credit_does_not_leave_wrappers(self):
        self.assertEqual(remove_subtitle_credit('你好 【幕by测试组】 世界'),'你好 世界')
        words=[RecognizedWord(0,.2,'幕'),RecognizedWord(.2,.4,'by'),RecognizedWord(.4,.6,'测试组'),RecognizedWord(.9,1.5,'真实内容')]
        self.assertEqual(clean_recognized_words(words),[words[-1]])

    def test_cleaning_preserves_unrelated_words_and_time_ranges(self):
        words=[RecognizedWord(0,1,'Hello world')]
        self.assertIs(clean_recognized_words(words),words)
        clean=clean_recognized_words([RecognizedWord(0,2,'幕byArtist 你好')])
        self.assertEqual(''.join(w.text for w in clean).strip(),'你好')
        self.assertTrue(all(0<=w.start<w.end<=2 for w in clean))

    def test_actual_recognition_adapter_cleans_model_output_not_insert_text(self):
        model=SimpleNamespace(transcribe=lambda *a,**k:([SimpleNamespace(text='幕byArtist',words=[SimpleNamespace(start=0,end=1,word='幕byArtist')])],SimpleNamespace(language='zh')))
        r=WhisperRecognizer('tiny','CPU（int8）');r._model=model
        result=r.transcribe(None,'zh')
        self.assertEqual(result.text,'');self.assertEqual(result.words,[])

class LiveCreditRegressionTests(unittest.TestCase):
    def test_punctuated_credit_from_live_overlay(self):
        self.assertEqual(remove_subtitle_credit('完成。幕。by测试组。继续。'),'完成。。继续。')
        words=[RecognizedWord(0,.1,'幕。'),RecognizedWord(.5,.6,'by'),RecognizedWord(.6,.9,'测试组。'),RecognizedWord(1.2,1.5,'继续。')]
        result=clean_recognized_words(words)
        self.assertEqual(''.join(w.text for w in result),'。继续。')
        self.assertEqual(result[-1],words[-1])
