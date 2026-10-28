"""Acceptance analysis rejects missing identity, placeholders and final drift."""
from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
from subtitle_events import source_text_extends
from display_module import DisplayModule
from tools.verify_streaming_translation import (analyze, real_text, bind_trace, sha256, source_identity, SOURCE_FILES,
                                                analysis_contract_identity,target_build_contract,load_analysis_contracts,CONTRACT_FILES)


def fixture():
    rows = []
    identity = dict(pipeline_id='fixture', session_id=0, source_id='audio', block_id=0, translation_epoch=0)
    rows.append(dict(kind='translation_context', perf=0, pipeline_id='fixture', session_id=0,
                     translation_epoch=0, target_language='en', enabled=True))
    for sequence, text, translated, final in [(1, '你好。', 'Hello.', False),
                                               (2, '你好世界。', 'Hello world.', False),
                                               (3, '你好世界。', 'Hello world.', True)]:
        now = sequence * 2
        source = dict(identity, sequence=sequence, revision=sequence, text=text, language='zh', final=final)
        rows.append(dict(source, kind='source', perf=now))
        rows.append(dict(source,kind='presented',perf=now+.65,presented_at=now+.65))
        rows.append(dict(identity, kind='translation_call', sequence=sequence, revision=sequence,
                         final=final, started_at=now + .1, queued_at=now, input_characters=len(text),
                         source_language='zh', target_language='en', backend='仅使用本地翻译'))
        rows.append(dict(identity, kind='translation_call_finished', sequence=sequence,
                         revision=sequence, final=final, source_language='zh', target_language='en',
                         backend='仅使用本地翻译', started_at=now + .1, finished_at=now + .5, success=True))
        rows.append(dict(identity, kind='translation', sequence=sequence, revision=sequence,
                         source_text=text, source_language='zh', target_language='en', text=translated,
                         final=final, display_applied=True, perf=now + .6))
        visible = dict(identity, sequence=sequence, revision=sequence, final=final,
                       source_text=text, source_sequence=sequence, source_final=final,
                       translation_state='final' if final else 'draft', pending=not final,
                       fragments=[{'text': translated}])
        rows.append(dict(kind='translation_frame', presented_at=now + .7, visible=[visible]))
    return rows, dict(text_included=True, dropped_events=0, pending_events=0, error=None)


class StreamingAcceptanceTests(unittest.TestCase):
    def check(self, rows, metadata):
        return analyze(rows, metadata, source_text_extends)

    def test_two_actual_fragment_updates_before_exact_final_pass(self):
        rows, metadata = fixture()
        result = self.check(rows, metadata)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['distinct_drafts_drawn_before_final'], 2)
        self.assertEqual(result['real_model_calls'], 3)

    def test_placeholder_frames_never_count_as_translation(self):
        rows, metadata = fixture()
        for row in rows:
            if row['kind'] == 'translation_frame' and not row['visible'][0]['final']:
                row['visible'][0]['fragments'] = [{'text': '翻译中…'}]
        result = self.check(rows, metadata)
        self.assertFalse(result['success'])
        self.assertEqual(result['distinct_drafts_drawn_before_final'], 0)

    def test_old_epoch_cannot_match_current_frame(self):
        rows, metadata = fixture()
        rows[-1]['visible'][0]['translation_epoch'] = 1
        result = self.check(rows, metadata)
        self.assertIn('latest_final_not_exactly_drawn', result['failures'])
        self.assertEqual(result['invalid_translation_frames'], 1)

    def test_nonempty_but_unrelated_drawn_fragment_fails(self):
        rows, metadata = fixture()
        rows[-1]['visible'][0]['fragments'] = [{'text': 'unrelated'}]
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_drafts_drawn_only_after_source_final_do_not_prove_streaming(self):
        rows, metadata = fixture()
        for row in rows:
            if row['kind'] == 'translation_frame' and not row['visible'][0]['final']:
                row['presented_at'] = 7
        result = self.check(rows, metadata)
        self.assertEqual(result['distinct_drafts_drawn_before_final'], 0)
        self.assertFalse(result['success'])

    def test_redacted_or_dropped_trace_remains_incomplete(self):
        rows, metadata = fixture()
        metadata.update(text_included=False, dropped_events=1)
        result = self.check(rows, metadata)
        self.assertIn('trace_text_missing', result['failures'])
        self.assertIn('trace_incomplete', result['failures'])

    def test_temporarily_not_applied_orphan_can_later_be_proved_by_actual_frame(self):
        rows, metadata = fixture()
        for row in rows:
            if row['kind'] == 'translation':
                row.update(kind='translation_not_applied', display_applied=False)
        self.assertTrue(self.check(rows, metadata)['success'])

    def test_final_source_identity_must_equal_the_latest_exact_source(self):
        rows, metadata = fixture()
        rows[-1]['visible'][0]['source_sequence'] = 2
        self.assertIn('latest_final_not_exactly_drawn', self.check(rows, metadata)['failures'])

    def test_unrelated_calls_and_all_failed_completions_cannot_prove_frames(self):
        rows, metadata = fixture()
        for row in rows:
            if row['kind'] == 'translation_call':
                row['pipeline_id'] = 'unrelated'
            elif row['kind'] == 'translation_call_finished':
                row['success'] = False
        result = self.check(rows, metadata)
        self.assertFalse(result['success'])
        self.assertEqual(result['matched_translation_frames'], 0)

    def test_all_failed_matching_completions_alone_do_not_prove_frames(self):
        rows, metadata = fixture()
        for row in rows:
            if row['kind'] == 'translation_call_finished': row['success']=False
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_frame_cannot_reference_a_future_source_version(self):
        rows, metadata = fixture()
        first = next(row for row in rows if row['kind'] == 'translation_frame')
        first['visible'][0]['source_sequence'] = 2
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_rewrite_before_frame_cannot_be_hidden_by_old_current_sequence(self):
        rows, metadata = fixture()
        old = next(row for row in rows if row['kind'] == 'source')
        rows.append(dict(old, sequence=2, revision=2, text='再见。', perf=2.65))
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_parenthesized_placeholder_is_not_real_translation(self):
        self.assertFalse(real_text('（翻译中…）'))
        self.assertFalse(real_text('[正在翻译...]'))

    def test_fragments_out_of_order_cannot_match_by_individual_substrings(self):
        rows, metadata = fixture()
        rows[-1]['visible'][0]['fragments'] = [{'text': 'world.'}, {'text': 'Hello'}]
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_disabled_context_rejects_old_epoch_frames_without_new_source(self):
        rows, metadata = fixture()
        rows.append(dict(kind='translation_context', pipeline_id='fixture', session_id=0,
                         translation_epoch=1, target_language='en', enabled=False, perf=6.65))
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_cache_promotion_requires_matching_identity_and_current_epoch(self):
        rows, metadata = fixture()
        final_result = next(row for row in rows if row['kind'] == 'translation' and row['final'])
        rows = [row for row in rows if not (row['kind'] in {'translation_call', 'translation_call_finished'}
                                           and row['sequence'] == 3)]
        rows.append(dict(final_result, kind='translation_cached', started_at=6.5, finished_at=6.5))
        self.assertTrue(self.check(rows, metadata)['success'])
        rows[-1]['translation_epoch'] = 5
        self.assertFalse(self.check(rows, metadata)['success'])

    def test_binding_requires_recorded_pid_hash_trace_root_and_time_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            exe = root / 'AudioSubtitleOverlay.exe'; exe.write_bytes(b'test executable')
            trace = root / 'trace' / 'run-1'; trace.mkdir(parents=True)
            digest = sha256(exe)
            record = dict(exe=str(exe), pid=7, owned_pids=[7], trace_root=str(root/'trace'),
                          executable_sha256=digest, executable_unchanged=True,
                          started_epoch=10, ended_epoch=30)
            metadata = dict(process_id=7, executable=str(exe), executable_sha256=digest,
                            started_epoch=11, ended_epoch=29)
            self.assertEqual(bind_trace(trace, metadata, record, exe=exe)['process_id'], 7)
            for key, wrong in [('process_id', 8), ('executable_sha256', 'wrong'), ('started_epoch', 1), ('ended_epoch', 40)]:
                changed = dict(metadata); changed[key]=wrong
                with self.subTest(key=key), self.assertRaises(ValueError):
                    bind_trace(trace, changed, record, exe=exe)
            with self.assertRaises(ValueError):
                bind_trace(root/'outside', metadata, record, exe=exe)
            exe.write_bytes(b'new executable at same path')
            with self.assertRaises(ValueError):bind_trace(trace,metadata,record,exe=exe)

    def test_source_binding_accepts_only_the_owned_direct_venv_producer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root / 'source'; source.mkdir()
            for name in SOURCE_FILES:
                file = source / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text(name)
            executable = root / 'python.exe'; executable.write_bytes(b'venv redirector')
            trace = root / 'trace' / 'run-1'; trace.mkdir(parents=True)
            digest = sha256(executable)
            record = dict(pid=7, producer_pid=8, producer_parent_pid=7, source=str(source),
                          source_sha256=source_identity(source), source_unchanged=True,
                          trace_root=str(root/'trace'), started_epoch=10, ended_epoch=30,
                          executable=str(executable), executable_sha256=digest)
            metadata = dict(process_id=8, executable=str(executable), executable_sha256=digest,
                            started_epoch=11, ended_epoch=29)
            self.assertEqual(bind_trace(trace, metadata, record, source=source)['process_id'], 8)
            bad_parent = dict(record, producer_parent_pid=9)
            with self.assertRaises(ValueError): bind_trace(trace, metadata, bad_parent, source=source)
            (source/'pipeline.py').write_text('changed source')
            with self.assertRaises(ValueError): bind_trace(trace, metadata, record, source=source)

    def test_late_second_final_does_not_extend_the_pre_final_draft_window(self):
        rows, metadata = fixture()
        first_final = next(row for row in rows if row['kind'] == 'source' and row['final'])
        first_final['perf'] = 4.5
        second_draft_frame = next(row for row in rows if row['kind'] == 'translation_frame'
                                  and row['visible'][0]['sequence'] == 2)
        second_draft_frame['visible'][0].update(source_sequence=3, source_final=True,
                                               translation_state='updating', pending=True)
        templates = [deepcopy(row) for row in rows if row.get('sequence') == 3]
        for row in templates:
            row.update(sequence=4, revision=4)
            if row['kind'] == 'source': row['perf']=7
            elif row['kind'] == 'translation_call': row.update(started_at=7.1, queued_at=7)
            elif row['kind'] == 'translation_call_finished': row.update(started_at=7.1, finished_at=7.5)
            elif row['kind'] == 'translation': row['perf']=8
            elif row['kind'] == 'presented': row.update(perf=7.65,presented_at=7.65)
            rows.append(row)
        final_frame = deepcopy(next(
            row for row in rows if row['kind'] == 'translation_frame' and row['visible'][0]['sequence'] == 3))
        final_frame['presented_at']=8.1
        final_frame['visible'][0].update(sequence=4, revision=4, source_sequence=4)
        rows.append(final_frame)
        result = self.check(rows, metadata)
        self.assertEqual(result['distinct_drafts_drawn_before_final'], 1)
        self.assertIn('same_block_drafts_and_exact_final_not_proved', result['failures'])

    def set_source_text(self,rows,sequence,raw):
        for row in rows:
            if row.get('sequence')!=sequence:continue
            if row['kind'] in {'source','presented'}:row['text']=raw
            elif row['kind']=='translation':row['source_text']=raw
            elif row['kind']=='translation_call':row['input_characters']=len(raw)
        for row in rows:
            if row['kind']=='translation_frame':
                for frame in row['visible']:
                    if frame['sequence']==sequence:frame['source_text']=DisplayModule._normalize_text(raw,'zh')

    def test_chinese_comma_and_crlf_frame_match_only_the_exact_production_normalizer(self):
        rows,metadata=fixture();self.set_source_text(rows,3,'你\r\n好,世界。')
        result=self.check(rows,metadata)
        self.assertTrue(result['success'],result)
        self.assertEqual(result['exact_final_frames'],1)
        rows[-1]['visible'][0]['source_text']='你\n好，地球。'
        self.assertFalse(self.check(rows,metadata)['success'])

    def test_raw_mt_source_must_not_be_normalized_to_hide_different_input(self):
        rows,metadata=fixture();self.set_source_text(rows,3,'你好,世界。')
        final_result=next(row for row in rows if row['kind']=='translation' and row['sequence']==3)
        final_result['source_text']='你好，世界。'
        result=self.check(rows,metadata)
        self.assertFalse(result['success']);self.assertEqual(result['invalid_translation_frames'],1)

    def test_published_rewrite_not_yet_presented_does_not_replace_the_visible_source(self):
        rows,metadata=fixture()
        self.set_source_text(rows,2,'明天见面。');self.set_source_text(rows,3,'明天见面。')
        second=next(row for row in rows if row['kind']=='source' and row['sequence']==2)
        second['perf']=2.65
        result=self.check(rows,metadata)
        self.assertTrue(result['success'],result)
        shown=next(row for row in rows if row['kind']=='presented' and row['sequence']==2)
        shown.update(perf=2.66,presented_at=2.66)
        result=self.check(rows,metadata)
        self.assertFalse(result['success']);self.assertGreater(result['invalid_translation_frames'],0)

    def test_hidden_intermediate_rewrite_in_raw_lineage_cannot_be_skipped(self):
        rows,metadata=fixture()
        for row in rows:
            if row.get('sequence',0)>=2:row.update(sequence=row['sequence']+1,revision=row['revision']+1)
            if row['kind']=='translation_frame':
                for frame in row['visible']:
                    if frame['sequence']>=2:
                        frame.update(sequence=frame['sequence']+1,revision=frame['revision']+1,
                                     source_sequence=frame['source_sequence']+1)
        first_frame=next(row for row in rows if row['kind']=='translation_frame')
        first=next(row for row in rows if row['kind']=='source' and row['sequence']==1)
        next(row for row in rows if row['kind']=='source' and row['sequence']==3)['perf']=2.4
        shown=next(row for row in rows if row['kind']=='presented' and row['sequence']==1)
        shown.update(perf=2.1,presented_at=2.1)
        shown=next(row for row in rows if row['kind']=='presented' and row['sequence']==3)
        shown.update(perf=2.5,presented_at=2.5)
        first_frame['visible'][0].update(source_sequence=3,source_final=False,translation_state='updating',pending=True)
        self.assertTrue(self.check(rows,metadata)['success'])
        # Sequence2 was skipped by UI, but its real raw rewrite still cuts
        # the ancestry between translated1 and displayed3.
        rows.append(dict(first,sequence=2,revision=2,text='明天见面。',perf=2.2))
        result=self.check(rows,metadata)
        self.assertFalse(result['success']);self.assertEqual(result['invalid_translation_frames'],1)

    def test_ui_presentation_cannot_claim_unpublished_or_different_raw_source(self):
        rows,metadata=fixture()
        first=next(row for row in rows if row['kind']=='presented')
        first['text']='不同内容。'
        self.assertIn('unmatched_source_presentation',self.check(rows,metadata)['failures'])
        rows,metadata=fixture();first=next(row for row in rows if row['kind']=='presented')
        first.update(perf=1.9,presented_at=1.9)
        self.assertIn('unmatched_source_presentation',self.check(rows,metadata)['failures'])

    def test_wrong_revision_still_cannot_match_a_normalized_frame(self):
        rows,metadata=fixture();self.set_source_text(rows,3,'你好,世界。')
        rows[-1]['visible'][0]['revision']=2
        self.assertFalse(self.check(rows,metadata)['success'])

    def test_missing_presented_evidence_is_not_replaced_by_frame_self_report(self):
        rows,metadata=fixture();rows=[row for row in rows if row['kind']!='presented']
        result=self.check(rows,metadata)
        self.assertIn('source_presentation_evidence_missing',result['failures'])
        self.assertEqual(result['matched_translation_frames'],0)

    def test_display_contracts_must_match_all_three_target_build_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.mkdir()
            for name in CONTRACT_FILES:(source/name).write_text('synthetic '+name)
            actual=analysis_contract_identity(source)
            exe=root/'AudioSubtitleOverlay.exe';exe.write_bytes(b'synthetic exe')
            data=root/'_internal';data.mkdir();build=data/'BUILD_INFO.json'
            import json
            build.write_text(json.dumps(dict(version='2.2.0',source_files=actual)))
            expected,identity=target_build_contract(exe)
            self.assertEqual(analysis_contract_identity(source,expected),actual)
            for name in CONTRACT_FILES:
                wrong={**expected,name:'0'*64}
                with self.subTest(name=name),self.assertRaises(ValueError):analysis_contract_identity(source,wrong)
            build.write_text(json.dumps(dict(source_files={})))
            with self.assertRaises(ValueError):target_build_contract(exe)

    def test_bound_production_contract_loader_keeps_module_owners_and_exact_normalization(self):
        import inspect,sys
        source=Path(inspect.getfile(DisplayModule)).parent
        previous={name:sys.modules.get(Path(name).stem) for name in CONTRACT_FILES}
        expected=analysis_contract_identity(source)
        extends,normalizer,loaded=load_analysis_contracts(source,expected)
        self.assertEqual(loaded,expected)
        self.assertEqual(normalizer('你好,\r\n朋友。','zh'),DisplayModule._normalize_text('你好,\r\n朋友。','zh'))
        self.assertFalse(extends('原本。','原来。'))
        for name,module in previous.items():self.assertIs(sys.modules.get(Path(name).stem),module)


if __name__ == '__main__':
    unittest.main()
