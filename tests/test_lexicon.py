"""识别提示词表的加载和长度限制测试。"""

from __future__ import annotations

import unittest

from lexicon import build_hotwords


class LexiconTests(unittest.TestCase):
    def test_custom_terms_are_prioritized(self):
        hotwords = build_hotwords("游戏", "星穹铁道,我的专名")
        self.assertTrue(hotwords.startswith("星穹铁道 我的专名"))
        self.assertLessEqual(len(hotwords), 180)

    def test_built_in_categories_provide_terms(self):
        gaming = build_hotwords("游戏", "")
        computing = build_hotwords("计算机", "")
        self.assertIn("游戏引擎", gaming)
        self.assertIn("终端窗口", computing)
        self.assertLessEqual(len(gaming), 32)
        self.assertLessEqual(len(computing), 32)
        self.assertTrue(all(len(term) >= 4 for term in gaming.split()))
        self.assertTrue(all(len(term) >= 4 for term in computing.split()))

    def test_general_modes_do_not_add_daily_phrases(self):
        self.assertEqual(build_hotwords("日常", ""), "")
        self.assertEqual(
            build_hotwords("日常、游戏、计算机", ""),
            build_hotwords("游戏、计算机", ""),
        )


if __name__ == "__main__":
    unittest.main()
