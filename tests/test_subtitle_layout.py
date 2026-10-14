"""字幕布局引擎测试：换行、分组、底部固定区与高度/动效计算。"""

from __future__ import annotations

import unittest

from subtitle_layout import (
    GROUP_GAP,
    PAIR_GAP,
    EntryView,
    Motion1D,
    ParagraphLedger,
    StyleMetrics,
    build_lines,
    compute_geometry,
    fade_progress,
    fit_rows_height,
    fit_window_height,
    select_footer_lines,
    wrap_text,
    reading_positions,
    ReadingCursor,
)


def fake_measure(text: str) -> float:
    """每个字符按 10 像素计宽，便于精确断言换行边界。"""
    return len(text) * 10.0


def make_styles():
    return {
        "source": StyleMetrics("source", line_height=20.0, pixel_size=16),
        "translation": StyleMetrics("translation", line_height=18.0, pixel_size=13),
    }


class WrapTextTests(unittest.TestCase):
    def test_wraps_on_width_and_keeps_char_ranges(self):
        records = wrap_text("abcdef", 25.0, fake_measure)
        self.assertEqual([record["text"] for record in records], ["ab", "cd", "ef"])
        self.assertEqual([(record["start"], record["end"]) for record in records],
                         [(0, 2), (2, 4), (4, 6)])

    def test_explicit_newline_splits_without_leaking_marker(self):
        records = wrap_text("ab\ncd", 100.0, fake_measure)
        self.assertEqual([record["text"] for record in records], ["ab", "cd"])
        self.assertEqual([(record["start"], record["end"]) for record in records],
                         [(0, 2), (3, 5)])

    def test_empty_text_produces_single_empty_line(self):
        records = wrap_text("", 100.0, fake_measure)
        self.assertEqual(records, [{"text": "", "start": 0, "end": 0}])

    def test_latin_words_are_not_split_mid_word(self):
        records = wrap_text("hello world foo", 65.0, fake_measure)
        self.assertEqual([record["text"] for record in records], ["hello", "world", "foo"])
        self.assertEqual([(record["start"], record["end"]) for record in records],
                         [(0, 5), (6, 11), (12, 15)])

    def test_long_word_without_boundary_falls_back_to_char_break(self):
        records = wrap_text("aaaaa", 25.0, fake_measure)
        self.assertEqual([record["text"] for record in records], ["aa", "aa", "a"])

    def test_space_beyond_width_ends_line_without_leaking(self):
        records = wrap_text("abc def", 35.0, fake_measure)
        self.assertEqual([record["text"] for record in records], ["abc", "def"])


class ParagraphLedgerTests(unittest.TestCase):
    def test_growth_only_rewraps_tail(self):
        ledger = ParagraphLedger()
        key = (0, "source")
        ledger.records(key, "aaaa", "source", 16, 20.0, fake_measure)
        records, _ = ledger.records(key, "aaaabbbb", "source", 16, 20.0, fake_measure)
        self.assertEqual([record["text"] for record in records], ["aa", "aa", "bb", "bb"])

    def test_head_revision_rebuilds(self):
        ledger = ParagraphLedger()
        key = (0, "source")
        ledger.records(key, "aaaa", "source", 16, 20.0, fake_measure)
        records, _ = ledger.records(key, "zzzzaaaa", "source", 16, 20.0, fake_measure)
        self.assertEqual(records[0]["text"], "zz")

    def test_prune_removes_stale_states(self):
        ledger = ParagraphLedger()
        ledger.records((0, "source"), "aaaa", "source", 16, 20.0, fake_measure)
        ledger.prune({(1, "source")})
        records, _ = ledger.records((0, "source"), "aaaa", "source", 16, 20.0, fake_measure)
        self.assertEqual(len(records), 2)


class BuildLinesTests(unittest.TestCase):
    def _build(self, entries, show_translation=True):
        return build_lines(
            entries,
            make_styles(),
            wrap_width=200.0,
            scale=1,
            show_translation=show_translation,
            ledger=ParagraphLedger(),
            measure=fake_measure,
        )

    def test_entry_keeps_source_above_translation(self):
        lines, _ = self._build([EntryView("src", "译文")])
        self.assertEqual([line["style"] for line in lines], ["source", "translation"])
        self.assertLess(lines[0]["y"], lines[1]["y"])

    def test_pair_gap_within_entry_and_group_gap_across_entries(self):
        lines, _ = self._build([EntryView("aa", "bb"), EntryView("cc", "dd")])
        gaps = [line["gap_before"] for line in lines]
        self.assertEqual(gaps, [0, PAIR_GAP, GROUP_GAP, PAIR_GAP])

    def test_translation_hidden_drops_translation_lines(self):
        lines, _ = self._build([EntryView("aa", "bb")], show_translation=False)
        self.assertEqual([line["style"] for line in lines], ["source"])

    def test_draft_marks_only_preview_lines(self):
        lines, _ = self._build([EntryView("aa", "bb", draft=True)])
        self.assertEqual([line["draft"] for line in lines], [True, False])

    def test_empty_entries_show_placeholder(self):
        lines, _ = self._build([])
        self.assertEqual(lines[0]["text"], "等待识别")
        self.assertIsNone(lines[0]["entry"])


class GeometryTests(unittest.TestCase):
    def _lines(self):
        # 两个字幕组：每组一行原文 + 一行译文。
        entries = [EntryView("aa", "bb"), EntryView("cc", "dd")]
        lines, content_height = build_lines(
            entries,
            make_styles(),
            wrap_width=200.0,
            scale=1,
            show_translation=True,
            ledger=ParagraphLedger(),
            measure=fake_measure,
        )
        return lines, content_height

    def test_footer_contains_whole_latest_group(self):
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=10, viewport_bottom=100,
            content_height=content_height, follow_tail=True, latest_entry=1,
        )
        self.assertTrue(geometry["footer_active"])
        self.assertEqual(
            [lines[index]["entry"] for index in geometry["footer_indices"]],
            [1, 1],
        )
        self.assertEqual(
            [lines[index]["style"] for index in geometry["footer_indices"]],
            ["source", "translation"],
        )

    def test_footer_keeps_source_above_translation_and_anchors_bottom(self):
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=10, viewport_bottom=100,
            content_height=content_height, follow_tail=True, latest_entry=1,
        )
        positions = geometry["footer_positions"]
        source_index, translation_index = geometry["footer_indices"]
        self.assertLess(positions[source_index], positions[translation_index])
        translation = lines[translation_index]
        self.assertAlmostEqual(
            positions[translation_index] + translation["height"], 100
        )

    def test_footer_keeps_whole_group_even_when_over_budget(self):
        """最新组永不按视口裁剪；装不下时顶行由绘制层裁掉，不搬进历史区。"""
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=90, viewport_bottom=100,
            content_height=content_height, follow_tail=True, latest_entry=1,
        )
        kept = [lines[index]["style"] for index in geometry["footer_indices"]]
        self.assertEqual(kept, ["source", "translation"])
        self.assertLess(geometry["footer_top"], 90)

    def test_no_overlap_between_history_and_footer_at_tail(self):
        """尾部视图下行与行的绘制矩形不得相交（乱叠回归）。"""
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=10, viewport_bottom=100,
            content_height=content_height, follow_tail=True, latest_entry=1,
        )
        footer_set = set(geometry["footer_indices"])
        scroll = geometry["tail_offset"]
        rects = []
        for index, line in enumerate(lines):
            if index in footer_set:
                y = geometry["footer_positions"][index]
            else:
                y = line["y"] - scroll
            rects.append((y, y + line["height"], index))
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                overlaps = a[0] < b[1] - 0.5 and b[0] < a[1] - 0.5
                self.assertFalse(
                    overlaps,
                    f"行 {a[2]} 与行 {b[2]} 重叠：{a[:2]} vs {b[:2]}",
                )

    def test_history_tail_offset_excludes_footer(self):
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=10, viewport_bottom=100,
            content_height=content_height, follow_tail=True, latest_entry=1,
        )
        # 第一组高度 20 + 4 + 18 = 42，从 y=12 起；历史区最后一行底边进入固定区才需要滚动。
        self.assertGreaterEqual(geometry["tail_offset"], 0)
        self.assertNotIn(
            geometry["footer_indices"][0], range(len(geometry["scroll_line_starts"]))
        )
        self.assertEqual(len(geometry["scroll_line_starts"]), 2)

    def test_non_follow_mode_disables_footer(self):
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=10, viewport_bottom=100,
            content_height=content_height, follow_tail=False, latest_entry=1,
        )
        self.assertFalse(geometry["footer_active"])
        self.assertEqual(len(geometry["scroll_line_starts"]), len(lines))

    def test_fit_window_height_clamps_between_bounds(self):
        self.assertEqual(fit_window_height(500, 68, 320), 320)
        self.assertEqual(fit_window_height(10, 68, 320), 68)
        self.assertEqual(fit_window_height(200, 68, 320), 200)

    def test_geometry_exposes_overlay_contract(self):
        """悬浮窗消费的每个布局字段都必须存在，避免运行期 KeyError。"""
        lines, content_height = self._lines()
        geometry = compute_geometry(
            lines, 1, viewport_top=10, viewport_bottom=100,
            content_height=content_height, follow_tail=True, latest_entry=1,
        )
        for key in (
            "footer_indices", "footer_positions", "footer_active", "footer_top",
            "footer_height", "line_starts", "tail_offset", "scroll_line_starts",
            "scrollable", "visible_line_capacity", "content_height",
        ):
            self.assertIn(key, geometry)
        self.assertAlmostEqual(
            geometry["footer_height"],
            geometry["footer_positions"][geometry["footer_indices"][0]] * -1 + 100,
        )


class FitRowsHeightTests(unittest.TestCase):
    def _stacked_lines(self):
        entries = [EntryView("aa", "bb"), EntryView("cc", "dd")]
        lines, _ = build_lines(
            entries,
            make_styles(),
            wrap_width=200.0,
            scale=1,
            show_translation=True,
            ledger=ParagraphLedger(),
            measure=fake_measure,
        )
        return lines

    def test_full_content_fits_to_exact_row_space(self):
        lines = self._stacked_lines()
        total = 12 + sum(line["height"] + line["gap_before"] for line in lines) + 10
        height = fit_rows_height(
            lines, [2, 3], True, scale=1,
            top_padding=12, bottom_padding=10, minimum=68, maximum=1000,
        )
        self.assertEqual(height, total)

    def test_maximum_drops_top_row_instead_of_cutting_half(self):
        lines = self._stacked_lines()
        full = 12 + sum(line["height"] + line["gap_before"] for line in lines) + 10
        # 丢弃顶行时它的行距也不再需要，窗口不留半行空档。
        without_top = full - lines[0]["height"] - lines[1]["gap_before"]
        height = fit_rows_height(
            lines, [2, 3], True, scale=1,
            top_padding=12, bottom_padding=10,
            minimum=68, maximum=without_top,
        )
        self.assertEqual(height, without_top)
        self.assertLess(height, full)

    def test_clamps_between_minimum_and_maximum(self):
        lines = self._stacked_lines()
        height = fit_rows_height(
            lines, [], False, 1, 12, 10, minimum=200, maximum=220,
        )
        self.assertGreaterEqual(height, 200)
        self.assertLessEqual(height, 220)

    def test_empty_lines_returns_minimum(self):
        self.assertEqual(
            fit_rows_height([], [], True, 1, 12, 10, minimum=68, maximum=320),
            68,
        )

    def test_latest_group_is_fully_visible_even_above_maximum(self):
        """最新组高于上限时窗口扩到组高：当前字幕正文译文不能被截掉。"""
        lines = self._stacked_lines()
        height = fit_rows_height(
            lines, [2, 3], True, scale=1,
            top_padding=12, bottom_padding=10,
            minimum=40, maximum=60, hard_maximum=1000,
        )
        group_space = 12 + lines[2]["height"] + lines[3]["gap_before"] + lines[3]["height"] + 10
        self.assertEqual(height, group_space)
        self.assertGreater(height, 60)


class MotionTests(unittest.TestCase):
    def test_motion_converges_to_target(self):
        motion = Motion1D(duration_ms=0, value=0)
        motion = Motion1D(duration_ms=0, value=0)
        motion.set_target(10)
        value, done = motion.advance()
        self.assertTrue(done)
        self.assertEqual(value, 10)

    def test_motion_is_monotonic_toward_target(self):
        motion = Motion1D(duration_ms=0, value=0)
        motion.duration_ms = 100
        motion.set_target(10)
        previous = motion.value
        for step in range(1, 6):
            value, _ = motion.advance(motion._started_at + step * 0.02)
            self.assertGreaterEqual(value, previous)
            previous = value
        self.assertLessEqual(value, 10)

    def test_snap_skips_animation(self):
        motion = Motion1D(duration_ms=100, value=0)
        motion.snap(5)
        self.assertEqual(motion.value, 5)
        self.assertFalse(motion.running)

    def test_fade_progress_reaches_one(self):
        self.assertEqual(fade_progress(0, 200), 0.0)
        self.assertEqual(fade_progress(200, 200), 1.0)
        self.assertEqual(fade_progress(50, 0), 1.0)


class ReadingPaceTests(unittest.TestCase):
    def test_new_row_waits_dwell_then_advances_without_hiding_initial_text(self):
        cursor = ReadingCursor()
        self.assertEqual(cursor.select(2, 2, 0.0, 1.0), (0, None))
        self.assertEqual(cursor.select(3, 2, .1, 1.0), (0, 1.0))
        self.assertEqual(cursor.select(3, 2, 1.0, 1.0), (1, None))
        self.assertEqual(cursor.select(5, 2, 1.1, 1.0), (1, 2.0))
        self.assertEqual(cursor.select(5, 2, 2.0, 1.0), (2, 3.0))
        cursor.reset()
        self.assertEqual(cursor.select(5, 2, 2.1, 1.0), (3, None))


class ReadingRegressionTests(unittest.TestCase):
    def test_growth_does_not_reconsume_newline_or_space(self):
        for old, new in (("ab\ncd", "ab\ncdef"), ("hello world", "hello world again")):
            ledger = ParagraphLedger()
            ledger.records((0, "source"), old, "source", 16, 55, fake_measure)
            records, _ = ledger.records((0, "source"), new, "source", 16, 55, fake_measure)
            self.assertTrue(all(r["text"] for r in records))
            self.assertEqual("".join(r["text"].replace(" ", "") for r in records), new.replace(" ", "").replace("\n", ""))
            for r in records:
                self.assertEqual(new[r["start"]:r["end"]], r["text"])

    def test_translation_arrival_and_growth_never_moves_source(self):
        ledger = ParagraphLedger()
        previous = None
        for translation in ("译文", "译文" * 40, "短译文"):
            lines, _ = build_lines([EntryView("source words " * 8, translation)], make_styles(), 100, 1, True, ledger, fake_measure)
            geometry = reading_positions(lines, 10, 180, 1, True, {k: v.line_height for k, v in make_styles().items()})
            source_y = [y for i, y in geometry["reading_positions"].items() if lines[i]["style"] == "source"]
            if previous is not None:
                self.assertEqual(source_y, previous)
            previous = source_y
            for i, y in geometry["reading_positions"].items():
                top, bottom = geometry["reading_lanes"][lines[i]["style"]]
                self.assertGreaterEqual(y, top)
                self.assertLessEqual(y + lines[i]["height"], bottom + .01)

    def test_long_bilingual_content_never_overlaps_or_draws_partial_rows(self):
        lines, _ = build_lines([EntryView("原文" * 80, "译文" * 100)], make_styles(), 90, 1, True, ParagraphLedger(), fake_measure)
        for height in (20, 80, 150, 250, 600):
            geometry = reading_positions(lines, 10, height, 1, True, {k: v.line_height for k, v in make_styles().items()})
            rectangles = [(y, y + lines[i]["height"]) for i, y in geometry["reading_positions"].items()]
            for start, end in rectangles:
                self.assertGreaterEqual(start, 10)
                self.assertLessEqual(end, height + .01)
            for first, second in zip(rectangles, rectangles[1:]):
                self.assertLessEqual(first[1], second[0] + .01)

    def test_same_language_collapses_translation_lane(self):
        lines, _ = build_lines([EntryView("current source", "old translation")], make_styles(), 100, 1, True, ParagraphLedger(), fake_measure)
        metrics = {k: v.line_height for k, v in make_styles().items()}
        bilingual = reading_positions(lines, 10, 180, 1, True, metrics)
        same = reading_positions(lines, 10, 180, 1, False, metrics, translation_current=False)
        self.assertNotIn("translation", same["reading_lanes"])
        self.assertEqual(same["reading_lanes"]["source"][0], bilingual["reading_lanes"]["source"][0])
        self.assertTrue(all(lines[i]["style"] == "source" for i in same["reading_positions"]))


    def test_cjk_closing_punctuation_is_not_stranded(self):
        records = wrap_text("你好，世界。", 25, fake_measure)
        self.assertTrue(all(not row["text"].startswith(("，", "。")) for row in records))
        self.assertEqual("".join(row["text"] for row in records), "你好，世界。")


if __name__ == "__main__":
    unittest.main()
