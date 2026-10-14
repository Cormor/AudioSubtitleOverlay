import unittest
from display_module import DisplayModule, SubtitleHistory

class DisplayReadabilityTests(unittest.TestCase):
    def test_auto_language_switching_keeps_translation_lane(self):
        module = DisplayModule(None, "zh")
        for seq, lang in enumerate(("en", "zh", "en", "zh"), 1):
            module.apply_source(seq, "hello", lang, 1)
            self.assertTrue(module.snapshot().translation_visible)

    def test_source_revision_clears_only_its_obsolete_translation(self):
        module = DisplayModule("en", "zh")
        module.apply_source(1, "hello", "en", 1)
        module.apply_translation(1, "你好", "zh")
        module.apply_source(2, "goodbye world", "en", 1)
        self.assertEqual(module.snapshot().entries, (("goodbye world", ""),))
        self.assertEqual(module.snapshot().latest_translation, "")
        module.apply_translation(1, "旧译文", "zh")
        self.assertEqual(module.snapshot().entries, (("goodbye world", ""),))
        self.assertFalse(module.history._pending_translations)
        module.apply_translation(2, "你好世界", "zh")
        self.assertEqual(module.snapshot().entries, (("goodbye world", "你好世界"),))

    def test_translation_ahead_of_source_still_attaches(self):
        history = SubtitleHistory()
        history.update_translation(3, "译文")
        history.update_source(1, 3, "source", "en")
        self.assertEqual(history.entries(), [("source", "译文")])

    def test_history_and_orphan_translation_storage_are_bounded(self):
        history = SubtitleHistory()
        for seq in range(1, 600):
            history.update_source(seq, seq, "source", "en")
            history.update_translation(seq - 1, "stale")
        self.assertEqual(len(history.blocks), history.max_blocks)
        self.assertEqual(len(history._sequence_blocks), history.max_blocks)
        self.assertFalse(history._pending_translations)

    def test_growing_source_keeps_partial_translation_without_claiming_current_version(self):
        module = DisplayModule("en", "zh")
        module.apply_source(1, "hello", "en", 1)
        module.apply_translation(1, "你好", "zh")
        module.apply_source(2, "hello world", "en", 1)
        snapshot = module.snapshot()
        self.assertEqual(snapshot.entries, (("hello world", "你好"),))
        self.assertEqual(snapshot.translation_pending, (True,))
        module.apply_translation(2, "你好世界", "zh")
        self.assertEqual(module.snapshot().translation_pending, (False,))

class PresentationTimingTests(unittest.TestCase):
    def test_unshown_latest_result_is_retained_until_its_tail_is_visible(self):
        from main import Application
        from types import SimpleNamespace
        from unittest.mock import patch
        update = SimpleNamespace(sequence=7, audio_end=1.0, measurements=lambda now: {"delay": now})
        app = SimpleNamespace(_pending_presentations={1: update}, _history=SimpleNamespace(blocks={1: SimpleNamespace(sequence=7)}))
        with patch("main.logging.info") as log:
            Application._display_presented(app, 2.0, set())
            self.assertIn(1, app._pending_presentations)
            log.assert_not_called()
            Application._display_presented(app, 3.0, {0})
            self.assertFalse(app._pending_presentations)
            log.assert_called_once()
