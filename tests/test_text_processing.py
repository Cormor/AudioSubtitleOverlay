"""字幕署名过滤测试。"""

from __future__ import annotations

import unittest

from display_module import DisplayEvent, DisplayModule
from text_processing import display_text, remove_subtitle_credit


class SubtitleCreditTests(unittest.TestCase):
    def test_removes_common_credit_variants(self):
        samples = (
            "你好 字幕byAlice",
            "你好 字幕 by Alice",
            "你好 字幕：BY_字幕组",
            "你好\n字幕:by@Alice",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertNotIn("字幕", remove_subtitle_credit(sample))

    def test_keeps_text_without_credit(self):
        text = "这是正常字幕内容。"
        self.assertEqual(remove_subtitle_credit(text), text)
        self.assertEqual(display_text(text, "en"), text)

    def test_display_text_removes_credit_before_language_normalization(self):
        self.assertEqual(display_text("繁體內容 字幕byAlice", "en"), "繁體內容")

    def test_credit_only_result_does_not_enter_display_history(self):
        module = DisplayModule("en", "zh")
        event = DisplayEvent("source", 1, "字幕byAlice", "en", block_id=1)
        self.assertFalse(module.apply(event))
        self.assertEqual(module.snapshot().entries, ())


if __name__ == "__main__":
    unittest.main()
