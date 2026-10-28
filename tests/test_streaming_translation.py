"""Controlled pipeline scheduling tests; no model or network calls."""
from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import patch

import pipeline
from pipeline import LivePipeline, PipelineOptions
from subtitle_events import SourceUpdate, source_text_extends


def eventually(check, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(.005)
    return bool(check())


class StreamingTranslationTests(unittest.TestCase):
    def runner(self, **callbacks):
        options = PipelineOptions('en', 'zh', 'tiny', 'CPU（int8）', '', 'fixture', '仅使用本地翻译')
        self.outputs, self.contexts = [], []
        p = LivePipeline(options, lambda _: None, lambda *a: None, lambda *a: None,
                         lambda _: None, lambda: None,
                         on_translation_update=self.outputs.append,
                         on_translation_context=self.contexts.append, **callbacks)
        p.translation_local_interval = .025
        p.translation_online_interval = .025
        self.addCleanup(self.close, p)
        return p

    @staticmethod
    def close(p):
        p.stop()
        if p._thread is not None:
            p._thread.join(3)
        p._translation_pool.shutdown(wait=True, cancel_futures=True)
        if p._translation_service and hasattr(p._translation_service, 'close'):
            p._translation_service.close()

    @staticmethod
    def source(p, sequence, text, *, block=0, final=False, language='en', submit=True):
        update = SourceUpdate(sequence, block, text, language, 0, 0, 0, 0, None,
                              pipeline_id=p._pipeline_id, session_id=p._source_session_id,
                              revision=sequence, final=final,
                              translation_epoch=p._configuration_generation)
        p._latest_source_language = language
        p._publish_update(update)
        if submit:
            p._submit_translation(sequence, text, language, p.options.target_language,
                                  block, p._configuration_generation,
                                  p.options.translation_backend)
        return update

    def test_continuous_partial_results_precede_final_and_same_text_final_reuses_result(self):
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): calls.append(text); return '译:' + text
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            for sequence in range(1, 5):
                self.source(p, sequence, 'hello ' + 'world ' * sequence)
                self.assertTrue(eventually(lambda: len(self.outputs) >= sequence))
            self.assertTrue(all(not result.final for result in self.outputs))
            self.source(p, 5, self.outputs[-1].source_text, final=True)
            self.assertTrue(eventually(lambda: len(self.outputs) == 5))
        self.assertEqual(len(calls), 4)
        self.assertTrue(self.outputs[-1].final)
        self.assertEqual(self.outputs[-1].sequence, 5)

    def test_slow_translation_survives_more_than_256_appends_and_queue_coalesces(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a):
                calls.append(text)
                if len(calls) == 1:
                    entered.set(); release.wait(2)
                return '译:' + text
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello')
            self.assertTrue(entered.wait(1))
            for sequence in range(2, 302):
                self.source(p, sequence, 'hello ' + 'word ' * sequence)
            self.assertEqual(len(p._pending_translations), 1)
            release.set()
            self.assertTrue(eventually(lambda: len(self.outputs) >= 2))
        self.assertEqual([result.sequence for result in self.outputs], [1, 301])
        self.assertEqual(len(calls), 2)

    def test_rewrite_then_return_to_old_prefix_does_not_revive_inflight_result(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a):
                calls.append(text)
                if len(calls) == 1:
                    entered.set(); release.wait(2)
                return '译:' + text
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'alpha')
            self.assertTrue(entered.wait(1))
            self.source(p, 2, 'bravo')
            self.source(p, 3, 'alpha again')
            release.set()
            self.assertTrue(eventually(lambda: bool(self.outputs)))
        self.assertEqual([result.sequence for result in self.outputs], [3])

    def test_final_bypasses_partial_delay_after_inflight_draft(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a):
                calls.append(text)
                if len(calls) == 1:
                    entered.set(); release.wait(2)
                return '译:' + text
        p = self.runner()
        p.translation_local_interval = 10
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello')
            self.assertTrue(entered.wait(1))
            self.source(p, 2, 'hello world', final=True)
            release.set()
            self.assertTrue(eventually(lambda: any(result.final for result in self.outputs), .5))
        self.assertEqual(self.outputs[-1].source_text, 'hello world')

    def test_final_priority_allows_ready_partial_after_two_finals(self):
        p = self.runner()
        p._translation_running = True
        self.source(p, 1, 'draft text', block=0)
        for sequence in range(2, 6):
            self.source(p, sequence, 'final text', block=sequence, final=True)
        with p._lock:
            order = [p._next_translation_locked()[0].source.block_id for _ in range(3)]
        self.assertEqual(order, [2, 3, 0])

    def test_backend_change_same_target_rejects_old_result_and_refreshes_existing_source(self):
        entered, release = threading.Event(), threading.Event()
        services = []
        class Translator:
            def __init__(self, backend, **k): self.backend=backend; self.closed=False; services.append(self)
            def translate(self, text, *a):
                if len(services) == 1:
                    entered.set(); release.wait(2)
                return self.backend + ':' + text
            def close(self): self.closed=True
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello', final=True)
            self.assertTrue(entered.wait(1))
            p.update_options(replace(p.options, translation_backend='仅使用免费在线 MyMemory'))
            release.set()
            self.assertTrue(eventually(lambda: bool(self.outputs)))
        self.assertEqual([result.translation_epoch for result in self.outputs], [1])
        self.assertEqual(self.outputs[0].source_text, 'hello')
        self.assertTrue(services[0].closed)

    def test_off_then_on_has_new_epoch_and_noop_republishes_context(self):
        p = self.runner()
        p.update_options(replace(p.options, translation_enabled=False))
        p.update_options(replace(p.options, translation_enabled=True))
        self.assertEqual([context.translation_epoch for context in self.contexts], [1, 2])
        self.assertFalse(p.update_options(p.options))
        self.assertEqual([context.translation_epoch for context in self.contexts], [1, 2, 2])
        self.assertEqual(p._source_session_id, 0)

    def test_stop_rejects_inflight_and_wakes_throttled_scheduler(self):
        entered, release = threading.Event(), threading.Event()
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, *a): entered.set(); release.wait(2); return 'late'
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello')
            self.assertTrue(entered.wait(1))
            p.stop(); release.set()
            p._translation_pool.shutdown(wait=True)
        self.assertFalse(self.outputs)

    def test_bounds_cover_sources_pending_lineages_and_result_cache(self):
        p = self.runner()
        p._translation_running = True
        for sequence in range(65):
            self.source(p, sequence, 'hello ' + str(sequence), block=sequence)
        self.assertEqual(len(p._translation_sources), 64)
        self.assertEqual(len(p._translation_lineages), 64)
        self.assertEqual(len(p._pending_translations), 64)
        with p._lock:
            for request in p._pending_translations.values():
                p._cache_translation_locked(request, '译' * 2000)
        self.assertLessEqual(p._translation_result_characters, 65536)

    def test_short_partial_is_deferred_but_short_final_translates(self):
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): calls.append(text); return '译'
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'I')
            self.assertFalse(p._pending_translations)
            self.source(p, 2, 'I', final=True)
            self.assertTrue(eventually(lambda: bool(self.outputs)))
        self.assertEqual(calls, ['I'])

    def test_reset_notifies_before_context_and_invalidates_source_session(self):
        events = []
        p = self.runner(on_reset=lambda: events.append('reset'))
        p._on_translation_context = lambda context: events.append(('context', context.session_id))
        self.source(p, 1, 'hello', submit=False)
        p._reset_source_session()
        self.assertEqual(events, ['reset', ('context', 1)])
        self.assertFalse(p._translation_sources)

    def test_fast_continuous_finals_do_not_keep_pushing_partial_deadline(self):
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): time.sleep(.015); return '译:' + text
        p = self.runner()
        p.translation_local_interval = .12
        finished = threading.Event()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'first draft')
            self.assertTrue(eventually(lambda: bool(self.outputs)))
            self.source(p, 2, 'first draft extended')
            def feed_finals():
                for sequence in range(100, 220):
                    self.source(p, sequence, 'final ' + str(sequence), block=1, final=True)
                    time.sleep(.003)
                finished.set()
            feeder = threading.Thread(target=feed_finals)
            feeder.start()
            try:
                self.assertTrue(eventually(lambda: any(result.sequence == 2 for result in self.outputs), .3))
                self.assertFalse(finished.is_set(), 'draft must complete while finals still arrive')
            finally:
                feeder.join(2)

    def test_refresh_is_atomic_with_new_asr_final_and_old_source_cannot_regress(self):
        p = self.runner()
        p._translation_running = True
        self.source(p, 10, 'old draft', submit=False)
        reached, release, source_done = threading.Event(), threading.Event(), threading.Event()
        original = p._publish_update
        def intercept(update):
            if update.sequence == 10 and update.translation_epoch == 1:
                reached.set(); release.wait(2)
            return original(update)
        p._publish_update = intercept
        updater = threading.Thread(target=lambda: p.update_options(replace(p.options, target_language='ja')))
        updater.start()
        self.assertTrue(reached.wait(1))
        def publish_final():
            self.source(p, 11, 'new final', final=True)
            source_done.set()
        publisher = threading.Thread(target=publish_final)
        publisher.start()
        try:
            self.assertFalse(source_done.wait(.05))
        finally:
            release.set(); updater.join(2); publisher.join(2)
        self.assertTrue(source_done.is_set())
        self.assertEqual(p._translation_sources[0].sequence, 11)
        self.assertEqual(p._pending_translations[0].source.text, 'new final')
        original(replace(p._translation_sources[0], sequence=10, text='old draft'))
        self.assertEqual(p._translation_sources[0].sequence, 11)

    def test_pending_eviction_emits_overloaded_status(self):
        metrics = []
        p = self.runner(on_metrics=metrics.append)
        p._translation_running = True
        for sequence in range(65):
            self.source(p, sequence, 'hello', block=sequence)
        self.assertIn({'translation_phase': 'overloaded'}, metrics)

    def test_whisper_unchanged_closed_text_publishes_real_final_and_translates_it(self):
        import numpy as np
        from types import SimpleNamespace
        from transcription import RecognizedWord
        ready = threading.Event()
        captures = []
        sources = []
        class Capture:
            sample_rate = 16000
            def __init__(self, *a): captures.append(self)
            def start(self, callback, error, on_ready=None):
                self.feed=callback
                if on_ready: on_ready('fixture')
                ready.set()
            def stop(self, **k): pass
        class Recognizer:
            actual_device, actual_compute_type = 'cpu', 'int8'
            def load(self): pass
            def transcribe(self, *a, **k):
                return SimpleNamespace(words=[RecognizedWord(0, .2, 'hello.')], language='en')
            def close(self): pass
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): return '译:' + text
        p = self.runner(on_update=sources.append)
        with patch.object(pipeline, 'SystemAudioCapture', Capture), patch.object(
                pipeline, '_new_recognizer', return_value=Recognizer()), patch.object(
                pipeline, 'TranslationService', Translator):
            p.start()
            self.assertTrue(ready.wait(1))
            captures[0].feed(np.ones(8000, dtype=np.float32))
            self.assertTrue(eventually(lambda: len(sources) == 1 and bool(self.outputs)))
            self.assertFalse(sources[0].final)
            captures[0].feed(np.ones(8000, dtype=np.float32))
            self.assertTrue(eventually(lambda: len(sources) == 2 and any(r.final for r in self.outputs)))
            p.stop(); p._thread.join(2)
        self.assertEqual([source.text for source in sources], ['hello.', 'hello.'])
        self.assertEqual([source.final for source in sources], [False, True])
        self.assertEqual(sources[0].block_id, sources[1].block_id)
        self.assertEqual(self.outputs[-1].sequence, sources[-1].sequence)

    def test_configuration_change_cancels_obsolete_rpc_and_recreates_closed_service(self):
        entered = threading.Event()
        services = []
        class Translator:
            def __init__(self, *a, **k): self.closed=False; services.append(self)
            def set_cancel_wait(self, callback): self.cancel=callback
            def translate(self, text, *a):
                if services.index(self) == 0:
                    entered.set()
                    while not self.cancel(.005): pass
                    self.close()
                    raise pipeline.WorkerCancelled('configuration changed')
                return 'new translation'
            def close(self): self.closed=True
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello', final=True)
            self.assertTrue(entered.wait(1))
            p.update_options(replace(p.options, target_language='ja'))
            self.assertTrue(eventually(lambda: bool(self.outputs), .5))
        self.assertTrue(services[0].closed)
        self.assertEqual(len(services), 2)
        self.assertEqual(self.outputs[0].target_language, 'ja')
        self.assertEqual(self.outputs[0].translation_epoch, 1)

    def test_source_session_reset_cancels_old_rpc_and_accepts_new_same_block(self):
        entered = threading.Event()
        services = []
        class Translator:
            def __init__(self, *a, **k): self.closed=False; services.append(self)
            def set_cancel_wait(self, callback): self.cancel=callback
            def translate(self, text, *a):
                if services.index(self) == 0:
                    entered.set()
                    while not self.cancel(.005): pass
                    self.close()
                    raise pipeline.WorkerCancelled('session changed')
                return 'new session'
            def close(self): self.closed=True
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello', final=True)
            self.assertTrue(entered.wait(1))
            p._reset_source_session()
            self.source(p, 2, 'new hello', final=True)
            self.assertTrue(eventually(lambda: bool(self.outputs), .5))
        self.assertEqual([output.session_id for output in self.outputs], [1])

    def test_normal_partial_growth_does_not_cancel_slow_rpc(self):
        entered = threading.Event()
        cancelled = []
        class Translator:
            def __init__(self, *a, **k): pass
            def set_cancel_wait(self, callback): self.cancel=callback
            def translate(self, text, *a):
                entered.set()
                for _ in range(10):
                    cancelled.append(self.cancel(.005))
                return '译:' + text
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello')
            self.assertTrue(entered.wait(1))
            self.source(p, 2, 'hello world')
            self.assertTrue(eventually(lambda: len(self.outputs) == 2))
        self.assertFalse(any(cancelled))
        self.assertEqual([output.sequence for output in self.outputs], [1, 2])

    def test_text_backend_translates_separate_partial_updates_before_final_audio(self):
        import numpy as np
        from asr_backends import TextHypothesis
        ready = threading.Event()
        captures = []
        sources = []
        class Capture:
            sample_rate = 16000
            def __init__(self, *a): captures.append(self)
            def start(self, callback, error, on_ready=None):
                self.feed=callback
                if on_ready: on_ready('fixture')
                ready.set()
            def stop(self, **k): pass
        class Recognizer:
            actual_device, actual_compute_type = 'cpu', 'int8'
            def __init__(self): self.calls=0
            def load(self): pass
            def accept_audio(self, frames):
                self.calls += 1
                return [TextHypothesis(0, self.calls, 'hello ' + 'world ' * self.calls,
                                       'en', self.calls == 3, 0, .5 * self.calls)]
            def close(self): pass
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): return '译:' + text
        recognizer = Recognizer()
        p = self.runner(on_update=sources.append)
        p.options = replace(p.options, model_size='sensevoice-int8')
        with patch.object(pipeline, 'SystemAudioCapture', Capture), patch.object(
                pipeline, '_new_recognizer', return_value=recognizer), patch(
                'recognition_sessions.SegmentedTextSession', return_value=recognizer), patch.object(
                pipeline, 'TranslationService', Translator):
            p.start()
            self.assertTrue(ready.wait(1))
            for count in range(1, 4):
                captures[0].feed(np.ones(8000, dtype=np.float32))
                self.assertTrue(eventually(lambda: len(self.outputs) == count))
                self.assertEqual(self.outputs[-1].final, count == 3)
            p.stop(); p._thread.join(2)
        self.assertEqual([source.final for source in sources], [False, False, True])

    def test_explicit_noop_refresh_renews_epoch_and_retranslates_final_without_new_audio(self):
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): calls.append(text); return '译:' + text
        p = self.runner()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello world', final=True)
            self.assertTrue(eventually(lambda: len(self.outputs) == 1))
            old = self.outputs[0]
            self.assertTrue(p.refresh_translation_context())
            self.assertTrue(eventually(lambda: len(self.outputs) == 2))
        self.assertEqual(self.outputs[-1].source_text, old.source_text)
        self.assertTrue(self.outputs[-1].final)
        self.assertEqual(self.outputs[-1].translation_epoch, old.translation_epoch + 1)
        self.assertEqual(self.outputs[-1].session_id, old.session_id)
        self.assertEqual(len(calls), 1)

    def test_policy_refresh_prioritizes_latest_final_over_old_history(self):
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): calls.append(text); return '译:' + text
        p = self.runner()
        p._translation_running = True
        for sequence in range(1, 6):
            self.source(p, sequence, 'history ' + str(sequence), block=sequence, final=True)
        p.update_options(replace(p.options, target_language='ja'))
        with patch.object(pipeline, 'TranslationService', Translator):
            p._translate_pending()
        self.assertEqual(calls[0], 'history 5')
        self.assertEqual(self.outputs[0].block_id, 5)

    def test_model_call_trace_includes_real_calls_and_finishes_without_source_text(self):
        events = []
        class Trace:
            enabled=False
            def record_source(self, update): pass
            def record(self, kind, **fields): events.append((kind, fields))
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a): return '译:' + text
        p = self.runner()
        p._trace = Trace()
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, 'hello')
            self.assertTrue(eventually(lambda: bool(self.outputs)))
        self.assertEqual([kind for kind, fields in events], ['translation_call', 'translation_call_finished'])
        self.assertEqual(events[0][1]['input_characters'], 5)
        self.assertTrue(events[1][1]['success'])
        self.assertFalse(any('source_text' in fields or 'text' in fields for kind, fields in events))

    def test_shared_ancestor_contract_has_narrow_punctuation_and_word_boundaries(self):
        cases = [
            ('今天的天气。', '今天的天气很好。', False, True),
            ('现在开始吗？', '现在开始吗，我们继续。', False, True),
            ('他说“你好。”', '他说“你好啊。”', False, True),
            ('He said "hello."', 'He said "hello world."', False, True),
            ('增长10%', '增长100%', False, False),
            ('word-', 'wording', False, False),
            ('？', '？？你好', False, False),
            ('hello?', 'hello!', False, False),
            ('你好。', '你好啊。', True, False),
            ('hello"', 'hello world"', False, False),
            ('I can', 'I cannot', False, False),
            ('I can', "I can't", False, False),
            ('I can', 'I can’t', False, False),
            ("I can.", "I can't help.", False, False),
            ('we', 'we’re ready', False, False),
            ('I can', 'I can help', False, True),
            ('I can', 'I can help', False, True),
            ('第1', '第10', False, False),
            ('CPU。', 'CPU温度上升。', False, True),
            ('今天的天气很好。', '明天天气很好啊。', False, False),
        ]
        for previous, current, final, expected in cases:
            with self.subTest(previous=previous, current=current):
                self.assertEqual(source_text_extends(previous, current, previous_final=final), expected)

    def test_300_punctuated_partial_appends_preserve_slow_mt_progress(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a):
                calls.append(text)
                if len(calls) == 1:
                    entered.set(); release.wait(2)
                return 'translated:' + text
        p = self.runner()
        p.options = replace(p.options, source_language='zh', target_language='en')
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, '正在翻译。', language='zh')
            self.assertTrue(entered.wait(1))
            for sequence in range(2, 302):
                self.source(p, sequence, '正在翻译' + '内容' * sequence + '。', language='zh')
            release.set()
            self.assertTrue(eventually(lambda: len(self.outputs) == 2))
        self.assertEqual([output.sequence for output in self.outputs], [1, 301])
        self.assertEqual(len(calls), 2)

    def test_synthetic_punctuation_growth_returns_draft_before_final(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Translator:
            def __init__(self, *a, **k): pass
            def translate(self, text, *a):
                calls.append(text)
                if len(calls) == 1:
                    entered.set(); release.wait(2)
                return 'translated:' + text
        p = self.runner()
        p.options = replace(p.options, source_language='zh', target_language='en')
        with patch.object(pipeline, 'TranslationService', Translator):
            self.source(p, 1, '今天。', language='zh')
            self.source(p, 2, '今天的天气。', language='zh')
            self.assertTrue(entered.wait(1))
            self.source(p, 3, '今天的天气很好。', language='zh')
            release.set()
            self.assertTrue(eventually(lambda: bool(self.outputs)))
            self.assertEqual(self.outputs[0].sequence, 2)
            self.assertFalse(any(output.final for output in self.outputs))
            self.source(p, 4, '今天的天气很好。', final=True, language='zh')
            self.assertTrue(eventually(lambda: self.outputs[-1].final))
        self.assertEqual(self.outputs[-1].source_text, '今天的天气很好。')


if __name__ == '__main__':
    unittest.main()
