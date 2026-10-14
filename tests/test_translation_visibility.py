import unittest
from subtitle_layout import TranslationLaneState
from display_module import DisplayModule

class TranslationVisibilityTests(unittest.TestCase):
    def state(self,source=None,target='zh',automatic=True):
        state=TranslationLaneState();state.configure(source,target,automatic);return state

    def test_manual_different_languages_keep_empty_area(self):
        s=self.state('en','zh',False)
        for t in (0,5,10,100):s.source_updated(t)
        self.assertTrue(s.visible)

    def test_manual_same_languages_never_open_on_translation(self):
        s=self.state('zh','zh',False);s.translation_output(1);s.source_updated(6)
        self.assertFalse(s.visible)

    def test_auto_hides_at_five_seconds_not_on_brief_blank(self):
        s=self.state()
        for t in (0,1,2,3,4,4.999):s.source_updated(t);self.assertTrue(s.visible)
        s.source_updated(5);self.assertFalse(s.visible)

    def test_translation_recovers_and_restarts_timeout(self):
        s=self.state()
        for t in (0,1,2,3,4,5):s.source_updated(t)
        s.translation_output(5.2);self.assertTrue(s.visible)
        for t in (6,7,8,9,10.199):s.source_updated(t);self.assertTrue(s.visible)
        s.source_updated(10.2);self.assertFalse(s.visible)

    def test_idle_time_does_not_close_or_consume_new_active_period(self):
        s=self.state();s.source_updated(0)
        self.assertTrue(s.visible)  # no event during idle
        s.source_updated(100);self.assertTrue(s.visible)
        for t in (101,102,103,104):s.source_updated(t);self.assertTrue(s.visible)
        s.source_updated(105);self.assertFalse(s.visible)

    def test_detected_language_switch_does_not_reset_auto_timeout(self):
        s=self.state()
        for i in range(6):
            s.configure('en' if i%2 else 'zh','zh',True);s.source_updated(i)
        self.assertFalse(s.visible)

    def test_user_language_change_resets_state(self):
        s=self.state()
        for i in range(6):s.source_updated(i)
        self.assertFalse(s.visible)
        s.configure(None,'en',True);self.assertTrue(s.visible)
        s.configure('zh','zh',False);self.assertFalse(s.visible)
        s.configure('en','zh',False);self.assertTrue(s.visible)

    def test_same_text_new_translation_version_is_an_output(self):
        m=DisplayModule(None,'zh');m.apply_source(1,'hello','en',1);m.apply_translation(1,'你好','zh')
        first=m.snapshot().translation_activity
        m.apply_source(2,'hello world','en',1);m.apply_translation(2,'你好','zh')
        self.assertEqual(m.snapshot().translation_activity,first+1)
        m.apply_translation(1,'过时结果','zh');self.assertEqual(m.snapshot().translation_activity,first+1)

    def test_history_translation_alone_does_not_generate_output_activity(self):
        m=DisplayModule(None,'zh');m.apply_source(1,'hello','en',1);m.apply_translation(1,'你好','zh')
        count=m.snapshot().translation_activity
        m.apply_source(2,'new block','en',2)
        self.assertEqual(m.snapshot().translation_activity,count)

    def test_translation_ahead_of_source_counts_once(self):
        m=DisplayModule(None,'zh')
        m.apply_translation(3,'提前译文','zh')
        self.assertEqual(m.snapshot().translation_activity,1)
        m.apply_source(3,'source','en',1)
        self.assertEqual(m.snapshot().entries, (('source','提前译文'),))
        self.assertEqual(m.snapshot().translation_activity,1)
