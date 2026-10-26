"""字幕分段与标点补全测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from rolling_transcript import RollingTranscript


def word(text: str, start: float, end: float):
    return SimpleNamespace(text=text, start=start, end=end)


def feed(transcript: RollingTranscript, words, audio_end: float, passes: int = 2):
    """模拟流水线重复识别同一窗口，使候选经两轮一致后确认。"""
    updates = []
    for _ in range(passes):
        updates.extend(transcript.update(words, 0.0, audio_end))
    return updates


def block_texts(updates):
    return [text for _block_id, text in updates]


class SentenceSplitTests(unittest.TestCase):
    def test_closing_quote_stays_with_sentence(self):
        """句末分段等待收尾引号，不能把“。”和“”拆到两段。"""
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('他说“你好。”', 0.0, 1.2)],
            audio_end=2.0,
        )
        updates += feed(
            transcript,
            [word('走了', 2.2, 2.6)],
            audio_end=3.4,
        )
        texts = block_texts(updates)
        self.assertTrue(any(text.startswith('他说“你好。') and text.endswith('”') for text in texts))
        self.assertTrue(any(text.startswith('走了') for text in texts))
        for text in texts:
            self.assertNotIn('。”走了', text)

    def test_decimal_point_is_not_sentence_end(self):
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('3.14159圆周率', 0.0, 1.2)],
            audio_end=2.0,
        )
        joined = "".join(block_texts(updates))
        self.assertIn('3.14159圆周率', joined)

    def test_abbreviation_period_is_not_sentence_end(self):
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('e.g. apple', 0.0, 1.2)],
            audio_end=2.0,
        )
        joined = "".join(block_texts(updates))
        self.assertIn('e.g. apple', joined)


class PausePunctuationTests(unittest.TestCase):
    def test_pause_inserts_fullwidth_comma_for_cjk(self):
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('你好', 0.0, 0.4), word('世界', 0.8, 1.2)],
            audio_end=2.0,
        )
        joined = "".join(block_texts(updates))
        self.assertIn('你好，世界', joined)

    def test_pause_inserts_halfwidth_marks_for_latin(self):
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('hello', 0.0, 0.4), word(' world', 0.8, 1.2)],
            audio_end=2.0,
        )
        joined = "".join(block_texts(updates))
        self.assertIn('hello, world', joined)
        self.assertNotIn('，', joined)

    def test_no_duplicate_punctuation(self):
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('好的。', 0.0, 0.5), word('然后', 1.2, 1.6)],
            audio_end=2.4,
        )
        joined = "".join(block_texts(updates))
        self.assertNotIn('。。', joined)
        self.assertNotIn('。，', joined)

    def test_pause_split_block_ends_with_sentence_mark(self):
        transcript = RollingTranscript()
        updates = feed(
            transcript,
            [word('你好', 0.0, 0.4), word('世界', 1.2, 1.6)],
            audio_end=2.4,
        )
        texts = [text for text in block_texts(updates) if text.startswith('你好')]
        self.assertTrue(texts)
        self.assertIn(texts[-1][-1], '。，.!')


class LongBlockTests(unittest.TestCase):
    def test_long_speech_splits_at_boundary_with_punctuation(self):
        transcript = RollingTranscript()
        text = "字" * 200
        updates = feed(
            transcript,
            [word(text, 0.0, 6.0)],
            audio_end=7.0,
        )
        texts = [item for item in block_texts(updates) if item]
        self.assertGreater(len(texts), 0)
        longest = max(len(item) for item in texts)
        self.assertLessEqual(longest, transcript.long_block_hard_limit)
        self.assertTrue(any(item.endswith(('，', '。', ',', '.')) for item in texts))

    def test_hard_split_never_exceeds_hard_limit(self):
        transcript = RollingTranscript()
        transcript.long_block_limit = 20
        transcript.long_block_hard_limit = 40
        feed(
            transcript,
            [word("一" * 120, 0.0, 4.0)],
            audio_end=5.0,
            passes=3,
        )
        # 以最终发布的段落为准；未确认草稿允许短暂超出。
        self.assertGreater(len(transcript._published), 1)
        for item in transcript._published.values():
            self.assertLessEqual(len(item), 42)


class DraftDisplayTests(unittest.TestCase):
    def test_pending_text_uses_pause_separators(self):
        transcript = RollingTranscript()
        transcript.update(
            [word('你好', 0.0, 0.4), word('世界', 1.0, 1.4)],
            0.0,
            1.4,
        )
        display = transcript._pending_display_text()
        self.assertIn('你好', display)
        self.assertIn('世界', display)


class OverlapRegressionTests(unittest.TestCase):
    def test_new_terminal_is_previewed_then_can_be_withdrawn_without_a_tail_block(self):
        transcript = RollingTranscript()
        initial = [word("这次表现不", 0, .8), word("差", .8, 1),
                   word("后面继续说话", 1.05, 2)]
        provisional = [word("这次表现不。", 0, .8), word("差。", .8, 1),
                       word("后面继续说话", 1.05, 2)]
        corrected = [word("这次表现不差。", 0, 1), word("后面继续说话", 1.05, 2.2)]
        transcript.update(initial, 0, 2)
        transcript.update(provisional, 0, 2.1)
        self.assertIn("这次表现不。差。", transcript._published[1])
        self.assertEqual(transcript.block_id, 1)
        self.assertEqual(transcript.safe_decode_start, 0)
        transcript.update(corrected, 0, 2.2)
        transcript.update(corrected, 0, 2.3)
        self.assertEqual(transcript._published[1], "这次表现不差。")
        self.assertEqual(transcript._published[2], "后面继续说话")
        self.assertNotIn("差。", transcript._published.values())

    def test_new_terminal_confirmed_in_next_window_keeps_the_previous_block(self):
        transcript = RollingTranscript()
        words = [word("第一句", 0, .8), word("下一句话还在说", 1, 2)]
        marked = [word("第一句。", 0, .8), word("下一句话还在说", 1, 2)]
        transcript.update(words, 0, 2)
        transcript.update(marked, 0, 2.1)
        self.assertEqual(transcript.block_id, 1)
        transcript.update(marked, 0, 2.2)
        self.assertEqual(transcript._published[1], "第一句。")
        self.assertEqual(transcript._published[2], "下一句话还在说")
        corrected_tail = [word("第一句。", 0, .8), word("下一句确实还在说", 1, 2.4)]
        transcript.update(corrected_tail, 0, 2.4)
        self.assertEqual(transcript._published[1], "第一句。")

    def test_terminal_fluctuation_does_not_permanently_block_confirmation(self):
        transcript = RollingTranscript()
        unmarked = [word("这个结果不差", 0, 1), word("接下来继续讲", 1.05, 2)]
        marked = [word("这个结果不。差。", 0, 1), word("接下来继续讲", 1.05, 2)]
        for index, words in enumerate((unmarked, marked, unmarked, marked, unmarked, unmarked)):
            transcript.update(words, 0, 2 + index * .05)
        self.assertTrue(transcript.confirmed_text.startswith("这个结果不差"))
        self.assertEqual(transcript.block_id, 1)

    def test_comma_decimal_and_abbreviation_changes_do_not_delay_body(self):
        for initial, corrected in (("你好世界", "你好，世界"),
                                   ("value 314 keeps going", "value 3.14 keeps going"),
                                   ("eg apple keeps going", "e.g. apple keeps going")):
            with self.subTest(corrected=corrected):
                transcript = RollingTranscript()
                transcript.update([word(initial, 0, 1), word(" 尾部", 1.1, 2)], 0, 2)
                transcript.update([word(corrected, 0, 1), word(" 尾部", 1.1, 2)], 0, 2.1)
                self.assertTrue(transcript.confirmed_text.startswith(corrected))

    def test_real_one_character_sentence_after_a_pause_is_preserved(self):
        transcript = RollingTranscript()
        words = [word("前一句", 0, .8), word("好。", 1.6, 1.9)]
        feed(transcript, words, 2.7)
        self.assertEqual(transcript._published[1], "前一句。")
        self.assertEqual(transcript._published[2], "好。")
        self.assertGreater(transcript.safe_decode_start, .8)
        self.assertFalse(transcript.pending)

    def test_repeated_characters_survive_terminal_withdrawal(self):
        transcript = RollingTranscript()
        words = [word("好好说话", 0, .8), word("后面继续", .85, 2)]
        marked = [word("好。好说话", 0, .8), word("后面继续", .85, 2)]
        for index, result in enumerate((words, marked, words, words)):
            transcript.update(result, 0, 2 + index * .1)
        self.assertTrue(transcript._published[1].startswith("好好说话"))
        self.assertEqual(transcript.block_id, 1)

    def test_single_confirmed_character_retiming_does_not_repeat_it(self):
        transcript = RollingTranscript()
        original = [word("好", 0, .16), word("后", 1.2, 2)]
        corrected = [word("好", 0, .2), word("后", 1.2, 2)]
        transcript.update(original, 0, 2)
        transcript.update(original, 0, 2.1)
        transcript.update(corrected, 0, 2.2)
        self.assertEqual(transcript.confirmed_text, "好")
        self.assertNotIn("好好", "".join(transcript._published.values()))

    def test_new_repeated_single_character_after_boundary_is_kept(self):
        transcript = RollingTranscript()
        original = [word("好", 0, .2), word("后", 1.2, 2)]
        transcript.update(original, 0, 2)
        transcript.update(original, 0, 2.1)
        # A shifted window contains a second real 好, starting after the old
        # confirmed boundary. Equal text alone must not remove this new audio.
        later = [word("好", .02, .22), word("后", 1, 1.8)]
        transcript.update(later, .2, 2.2)
        transcript.update(later, .2, 2.3)
        self.assertTrue(transcript.confirmed_text.startswith("好好"))

    def test_stop_like_empty_window_after_terminal_withdrawal_keeps_text(self):
        transcript = RollingTranscript()
        words = [word("连续句尾不差", 0, 1), word("下一段", 1.05, 2)]
        marked = [word("连续句尾不。差", 0, 1), word("下一段", 1.05, 2)]
        for index, result in enumerate((words, marked, words)):
            transcript.update(result, 0, 2 + index * .1)
        transcript.update([], 0, 3)
        self.assertIn("连续句尾不差", "".join(transcript._published.values()))
        self.assertFalse(transcript.pending)


    def test_expired_prefix_is_confirmed_before_retimed_window_dedup(self):
        transcript = RollingTranscript()
        transcript.update([word("hello", 0, .5), word(" world", .5, 1.5)], 0, 1.5)
        transcript.update([word("hello", 0, .3), word(" world", .3, .8)], .6, 1.6)
        self.assertEqual(transcript._published[1], "hello world")

    def test_long_expired_paragraph_prefix_is_not_appended_twice(self):
        transcript = RollingTranscript()
        prefix = "在重叠识别窗口中同一段已经显示的文字不应该反复重新追加" * 3
        transcript.update([word(prefix, 0, .5), word(" 后续字幕", .5, 1.5)], 0, 1.5)
        transcript.update([word(prefix, 0, .3), word(" 后续字幕", .3, .8)], .6, 1.6)
        self.assertEqual(transcript._published[1], prefix + " 后续字幕")

    def test_real_repeated_speech_at_later_times_is_not_deleted(self):
        transcript = RollingTranscript()
        feed(transcript, [word("hello world.", 0, 1)], 2)
        feed(transcript, [word("hello world.", 4, 5)], 6)
        combined = " ".join(transcript._published.values())
        self.assertEqual(combined.count("hello world"), 2)

    def test_closed_sentence_is_not_republished_with_next_sentence_draft(self):
        transcript = RollingTranscript()
        feed(transcript, [word("第一句。", 0, 1)], 2)
        transcript.update([word("下一句", 3, 3.5)], 0, 3.5)
        self.assertEqual(transcript._published[1], "第一句。")
        self.assertEqual(transcript._published[2], "下一句")

    def test_unicode_casefold_overlap_does_not_duplicate_or_crash(self):
        transcript = RollingTranscript()
        feed(transcript, [word("Straße", 0, 1)], 2)
        feed(transcript, [word("Straße", 0, 1)], 2.2)
        self.assertEqual(" ".join(transcript._published.values()).count("Straße"), 1)


if __name__ == "__main__":
    unittest.main()
