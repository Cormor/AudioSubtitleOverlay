"""Isolated installed acceptance must preserve the user's actual ASR selection."""
import copy
from pathlib import Path
import unittest
from tools import verify_installed_runtime as module


class InstalledRuntimeSelectionTests(unittest.TestCase):
    def formal(self):
        return dict(recognition_model='large-v3-turbo',recognition_device='自动（优先GPU）',model_path='',
                    source_language='中文',target_language='中文',loopback_device='Synthetic selected source',
                    recognition_beam_size=2,recognition_update_interval=.42,recognition_window_seconds=19,
                    recognition_temperature_schedule='0,0.3',recognition_no_repeat_ngram_size=4,
                    recognition_repetition_penalty=1.07,recognition_no_speech_threshold=.55,
                    recognition_vad_silence_ms=520,recognition_condition_on_previous_text=True,
                    recognition_future_parameter={'value':7},overlay_font_size=28,
                    overlay_translation_font_size=21,overlay_text_color='#66ccee',translation_enabled=False)
    def test_preserves_large_gpu_language_and_all_recognition_parameters_without_mutation(self):
        formal=self.formal();before=copy.deepcopy(formal)
        result=module.build_acceptance_settings(formal,keep_recognition_selection=True,
                    keep_source_selection=True,target_language='英语')
        self.assertEqual(module.requested_recognition_selection(result),module.requested_recognition_selection(formal))
        self.assertEqual(result['recognition_model'],'large-v3-turbo')
        self.assertEqual(result['recognition_device'],'自动（优先GPU）')
        self.assertEqual(result['recognition_beam_size'],2)
        self.assertEqual(result['source_language'],'中文')
        self.assertEqual(result['target_language'],'英语')
        self.assertTrue(result['translation_enabled'])
        self.assertEqual(formal,before)
        for key in ('overlay_font_size','overlay_translation_font_size','overlay_text_color'):
            self.assertEqual(result[key],formal[key])
    def test_custom_override_is_preserved_only_in_keep_recognition_mode(self):
        formal=self.formal();formal['model_path']='X:/synthetic-custom-model'
        kept=module.build_acceptance_settings(formal,keep_recognition_selection=True,keep_source_selection=True)
        self.assertEqual(kept['model_path'],formal['model_path'])
        default=module.build_acceptance_settings(formal,source_name='Synthetic default')
        self.assertEqual(default['model_path'],'')
    def test_flag_off_retains_original_sensevoice_cpu_auto_defaults(self):
        result=module.build_acceptance_settings(self.formal(),source_name='Synthetic default')
        self.assertEqual(result['recognition_model'],'sensevoice-int8')
        self.assertEqual(result['recognition_device'],'CPU（int8）')
        self.assertEqual(result['source_language'],'自动检测')
        self.assertEqual(result['loopback_device'],'系统播放：Synthetic default')
        self.assertEqual(result['model_path'],'')
    def test_explicit_legacy_model_and_language_override_still_work(self):
        result=module.build_acceptance_settings(self.formal(),model='tiny.en',source_language='英语',
                                               source_name='Synthetic default',show_main=True)
        self.assertEqual(result['recognition_model'],'tiny.en')
        self.assertEqual(result['source_language'],'英语')
        self.assertFalse(result['start_minimized'])
    def test_source_policy_is_independent_and_does_not_require_speaker_when_kept(self):
        formal=self.formal()
        result=module.build_acceptance_settings(formal,keep_recognition_selection=True,source_name='Synthetic default')
        self.assertEqual(result['loopback_device'],'系统播放：Synthetic default')
        kept=module.build_acceptance_settings(formal,keep_source_selection=True)
        self.assertEqual(kept['loopback_device'],formal['loopback_device'])
        self.assertEqual(kept['recognition_model'],'sensevoice-int8')
    def test_conflicting_explicit_overrides_are_rejected_in_cli_and_pure_builder(self):
        base=['--exe','fixture.exe','--output','fixture-output','--keep-recognition-selection']
        for extra in (['--model','tiny.en'],['--source-language','英语']):
            with self.assertRaises(SystemExit): module.parse_arguments(base+extra)
        for extra in (dict(model='tiny.en'),dict(source_language='英语')):
            with self.assertRaises(ValueError):
                module.build_acceptance_settings(self.formal(),keep_recognition_selection=True,
                                                keep_source_selection=True,**extra)
    def test_invalid_or_incomplete_inputs_do_not_invent_a_formal_selection(self):
        with self.assertRaises(TypeError): module.build_acceptance_settings(None,source_name='Synthetic')
        with self.assertRaises(TypeError): module.build_acceptance_settings(self.formal(),keep_recognition_selection='false')
        with self.assertRaises(ValueError): module.build_acceptance_settings({},keep_recognition_selection=True,keep_source_selection=True)
        with self.assertRaises(ValueError): module.build_acceptance_settings(self.formal(),source_name='')
    def test_parser_preserves_defaults_without_accessing_exe_or_windows(self):
        args=module.parse_arguments(['--exe','fixture.exe','--output','fixture-output'])
        self.assertIsNone(args.model)
        self.assertIsNone(args.source_language)
        self.assertFalse(args.keep_recognition_selection)
        kept=module.parse_arguments(['--exe','fixture.exe','--output','fixture-output','--keep-recognition-selection'])
        self.assertTrue(kept.keep_recognition_selection)


    def test_default_mouse_override_preserves_true_false_or_absent_formal_preference(self):
        args=module.parse_arguments(['--exe','fixture.exe','--output','fixture-output'])
        self.assertIsNone(args.mouse_passthrough)
        for original in (None,False,True):
            with self.subTest(original=original):
                formal=self.formal()
                if original is not None: formal['overlay_mouse_passthrough']=original
                before=copy.deepcopy(formal)
                isolated=module.build_acceptance_settings(formal,keep_recognition_selection=True,
                    keep_source_selection=True,mouse_passthrough=args.mouse_passthrough)
                self.assertEqual('overlay_mouse_passthrough' in isolated,original is not None)
                if original is not None:self.assertIs(isolated['overlay_mouse_passthrough'],original)
                self.assertEqual(formal,before)
                self.assertEqual(module.requested_recognition_selection(isolated),module.requested_recognition_selection(formal))

    def test_explicit_mouse_enable_only_changes_isolated_preference_and_keeps_asr_source_style(self):
        formal=self.formal();formal['overlay_mouse_passthrough']=False;before=copy.deepcopy(formal)
        args=module.parse_arguments(['--exe','fixture.exe','--output','fixture-output','--mouse-passthrough'])
        baseline=module.build_acceptance_settings(formal,keep_recognition_selection=True,keep_source_selection=True)
        isolated=module.build_acceptance_settings(formal,keep_recognition_selection=True,
            keep_source_selection=True,mouse_passthrough=args.mouse_passthrough)
        self.assertIs(args.mouse_passthrough,True)
        self.assertEqual(isolated,{**baseline,'overlay_mouse_passthrough':True})
        self.assertEqual(formal,before)
        for name in ('recognition_model','recognition_device','model_path','loopback_device','overlay_font_size','overlay_translation_font_size','overlay_text_color'):
            self.assertEqual(isolated[name],formal[name])

    def test_explicit_mouse_disable_isolated_only_and_nonboolean_overrides_are_rejected(self):
        formal=self.formal();formal['overlay_mouse_passthrough']=True;before=copy.deepcopy(formal)
        args=module.parse_arguments(['--exe','fixture.exe','--output','fixture-output','--no-mouse-passthrough'])
        baseline=module.build_acceptance_settings(formal,keep_recognition_selection=True,keep_source_selection=True)
        isolated=module.build_acceptance_settings(formal,keep_recognition_selection=True,
            keep_source_selection=True,mouse_passthrough=args.mouse_passthrough)
        self.assertIs(args.mouse_passthrough,False)
        self.assertEqual(isolated,{**baseline,'overlay_mouse_passthrough':False})
        self.assertEqual(formal,before)
        for invalid in (0,1,'true'):
            with self.subTest(invalid=invalid),self.assertRaises(TypeError):
                module.build_acceptance_settings(formal,keep_recognition_selection=True,
                    keep_source_selection=True,mouse_passthrough=invalid)


if __name__=='__main__':
    unittest.main()
