"""真实 DisplayModule → Application 快照 → Overlay 生产排版与绘制路径。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from application_controller import SessionController
from display_module import DisplayModule
from main import Application
from overlay import OverlayWindow
from PIL import ImageFont
from settings import Settings
from runtime_trace import RuntimeTrace
from subtitle_events import SourceUpdate, TranslationContext, TranslationUpdate
from subtitle_layout import Motion1D, ParagraphLedger, ReadingCursor, TranslationLaneState


def context(epoch=0, session=1, pipeline="p", source="en", target="zh", enabled=True):
    return TranslationContext(pipeline, session, epoch, target, enabled, source)


def source(seq, text, block=1, language="en", final=False, epoch=0, session=1, pipeline="p"):
    return SourceUpdate(seq, block, text, language, 0, 0, 0, 0, None,
                        session_id=session, revision=seq, final=final,
                        pipeline_id=pipeline, translation_epoch=epoch)


def translated(update, text="你好", target="zh", **changes):
    result = TranslationUpdate(update.sequence, update.block_id, update.text, update.language,
                               target, text, source_id=update.source_id,
                               session_id=update.session_id, revision=update.revision,
                               final=update.final, pipeline_id=update.pipeline_id,
                               translation_epoch=update.translation_epoch)
    return replace(result, **changes)


class DisplayStreamingTests(unittest.TestCase):
    def module(self, source_language="en", target="zh"):
        result = DisplayModule(source_language, target)
        self.assertTrue(result.apply_translation_context(context(source=source_language, target=target)))
        return result

    def test_partial_translation_is_visible_before_final(self):
        m = self.module()
        update = source(1, "hello")
        m.apply_update(update)
        self.assertTrue(m.apply_translation_update(translated(update)))
        snapshot = m.take_snapshot()
        self.assertEqual(snapshot.entries, (("hello", "你好"),))
        self.assertEqual(snapshot.revision_modes, ("preview",))
        self.assertEqual(snapshot.translation_states, ("draft",))
        self.assertEqual(snapshot.latest_translation, "你好")
        self.assertIsNone(m.take_snapshot())

    def test_append_preserves_old_snapshot_and_new_result_replaces_atomically(self):
        m = self.module()
        first, grown = source(1, "hello"), source(2, "hello world")
        m.apply_update(first)
        m.apply_translation_update(translated(first))
        m.apply_update(grown)
        self.assertEqual(m.snapshot().entries, (("hello world", "你好"),))
        self.assertEqual(m.snapshot().translation_pending, (True,))
        self.assertEqual(m.snapshot().latest_translation_state, "updating")
        self.assertEqual(m.history.blocks[1].translation_sequence, 1)
        m.apply_translation_update(translated(grown, "你好世界"))
        self.assertEqual(m.snapshot().latest_translation_state, "draft")
        self.assertEqual(m.snapshot().latest_translation, "你好世界")

    def test_partial_sentence_end_punctuation_can_move_as_text_grows(self):
        m = self.module(source_language="zh", target="en")
        first = source(1, "测试。", language="zh")
        m.apply_update(first)
        m.apply_update(source(2, "测试的。", language="zh"))
        m.apply_update(source(3, "测试的结果。", language="zh"))
        self.assertTrue(m.apply_translation_update(translated(first, "test", target="en")))
        self.assertEqual(m.snapshot().latest_translation, "test")
        self.assertEqual(m.snapshot().latest_translation_state, "updating")
        self.assertEqual(m.history.blocks[1].lineage_start_sequence, 1)

    def test_word_internal_growth_is_rewrite_but_word_append_is_growth(self):
        for first_text, next_text, retained in (("I can", "I cannot", False),
                                                ("10", "100", False),
                                                ("I can", "I can help", True)):
            with self.subTest(text=next_text):
                m = self.module()
                first = source(1, first_text)
                m.apply_update(first)
                m.apply_update(source(2, next_text))
                self.assertEqual(m.apply_translation_update(translated(first)), retained)

    def test_final_punctuation_is_not_reinterpreted_as_partial_growth(self):
        m = self.module(source_language="zh", target="en")
        first = source(1, "测试。", language="zh", final=True)
        m.apply_update(first)
        m.apply_update(source(2, "测试结果。", language="zh", final=True))
        self.assertFalse(m.apply_translation_update(translated(first, target="en")))

    def test_rewrite_then_old_prefix_return_does_not_revive_old_request(self):
        m = self.module()
        first = source(1, "I can")
        m.apply_update(first)
        m.apply_translation_update(translated(first, "我可以"))
        m.apply_update(source(2, "I cannot"))  # 词内字符增长改变末词，属于改写。
        m.apply_update(source(3, "We cannot"))
        self.assertEqual(m.snapshot().latest_translation, "")
        m.apply_update(source(4, "I can help"))
        self.assertFalse(m.apply_translation_update(translated(first, "旧结果")))
        self.assertEqual(m.snapshot().entries, (("I can help", ""),))
        self.assertEqual(m.history.blocks[1].lineage_start_sequence, 4)

    def test_rewrite_invalidates_pending_older_snapshot(self):
        m = self.module()
        m.apply_update(source(1, "alpha"))
        future = source(3, "alpha beta")
        m.apply_translation_update(translated(future, "提前结果"))
        self.assertTrue(m.history._pending_translations)
        m.apply_update(source(4, "changed"))
        self.assertFalse(m.history._pending_translations)
        self.assertFalse(m.apply_update(future))

    def test_detection_language_change_and_back_invalidates_old_lineage(self):
        m = self.module(source_language=None)
        first = source(1, "hello", language="en")
        m.apply_update(first)
        m.apply_translation_update(translated(first))
        m.apply_update(source(2, "hello", language="zh"))
        self.assertEqual(m.snapshot().latest_translation, "")
        self.assertTrue(m.snapshot().translation_visible)
        m.apply_update(source(3, "hello again", language="en"))
        self.assertFalse(m.apply_translation_update(translated(first)))

    def test_slow_translation_survives_thousands_of_append_partials(self):
        m = self.module()
        first = source(1, "hello")
        m.apply_update(first)
        for seq in range(2, 1202):
            m.apply_update(source(seq, "hello" + " a" * seq))
        self.assertTrue(m.apply_translation_update(translated(first)))
        self.assertEqual(m.snapshot().latest_translation, "你好")
        self.assertEqual(m.snapshot().latest_translation_state, "updating")
        self.assertEqual(len(m.history._sequence_blocks), 1)

    def test_late_older_result_cannot_replace_newer_translation(self):
        m = self.module()
        first, second = source(1, "hello"), source(2, "hello world")
        m.apply_update(first)
        m.apply_update(second)
        m.apply_translation_update(translated(second, "较新译文"))
        activity = m.snapshot().translation_activity
        self.assertFalse(m.apply_translation_update(translated(first, "旧译文")))
        self.assertEqual(m.snapshot().latest_translation, "较新译文")
        self.assertEqual(m.snapshot().translation_activity, activity)

    def test_same_text_final_keeps_draft_until_final_result(self):
        m = self.module()
        first, final = source(1, "hello"), source(2, "hello", final=True)
        m.apply_update(first)
        m.apply_translation_update(translated(first))
        m.apply_update(final)
        self.assertEqual(m.snapshot().latest_translation, "你好")
        self.assertEqual(m.snapshot().latest_translation_state, "updating")
        m.apply_translation_update(translated(final))
        self.assertEqual(m.snapshot().latest_translation_state, "final")
        self.assertEqual(m.snapshot().revision_modes, ("stable",))

    def test_final_longer_than_translated_prefix_stays_updating(self):
        m = self.module()
        first = source(1, "hello")
        m.apply_update(first)
        m.apply_update(source(2, "hello world", final=True))
        m.apply_translation_update(translated(first))
        self.assertEqual(m.snapshot().latest_translation_state, "updating")

    def test_translation_before_source_attaches_once_and_stays_draft(self):
        m = self.module()
        update = source(3, "hello")
        self.assertFalse(m.apply_translation_update(translated(update)))
        self.assertEqual(m.snapshot().translation_activity, 1)
        m.apply_update(update)
        self.assertEqual(m.snapshot().entries, (("hello", "你好"),))
        self.assertEqual(m.snapshot().translation_activity, 1)
        self.assertEqual(m.snapshot().latest_translation_state, "draft")

    def test_orphan_snapshot_can_attach_when_a_newer_append_source_arrives(self):
        m = self.module()
        m.apply_update(source(1, "hello"))
        m.apply_translation_update(translated(source(3, "hello world"), "你好世界"))
        m.apply_update(source(4, "hello world again"))
        self.assertEqual(m.snapshot().latest_translation, "你好世界")
        self.assertEqual(m.snapshot().latest_translation_state, "updating")
        self.assertFalse(m.history._pending_translations)

    def test_translation_for_previous_block_does_not_change_latest_pair(self):
        m = self.module()
        first = source(1, "first", final=True)
        second = source(2, "second", block=2)
        m.apply_update(first)
        m.apply_update(second)
        m.apply_translation_update(translated(first, "第一段"))
        self.assertEqual(m.snapshot().entries, (("first", "第一段"), ("second", "")))
        self.assertEqual(m.snapshot().latest_translation, "")
        self.assertEqual(m.snapshot().latest_translation_state, "waiting")

    def test_context_change_preserves_source_and_rejects_queued_old_epoch(self):
        m = self.module()
        update = source(1, "hello")
        m.apply_update(update)
        m.apply_translation_update(translated(update))
        m.invalidate_translation_context()
        self.assertFalse(m.apply_translation_update(translated(update, "旧排队")))
        self.assertFalse(m.apply_translation_context(context()))
        self.assertFalse(m.apply_translation_update(translated(update, "旧排队")))
        m.apply_translation_context(context(epoch=1))
        self.assertEqual(m.snapshot().latest_source, "hello")
        self.assertFalse(m.apply_translation_update(translated(update)))
        self.assertTrue(m.apply_translation_update(translated(update, translation_epoch=1)))

    def test_target_change_drops_orphans_and_old_language(self):
        m = self.module()
        update = source(1, "hello")
        m.apply_translation_update(translated(update))
        m.set_languages("en", "ja")
        self.assertFalse(m.history._pending_translations)
        self.assertFalse(m.apply_translation_context(context()))
        m.apply_translation_context(context(epoch=1, target="ja"))
        m.apply_update(replace(update, translation_epoch=1))
        self.assertFalse(m.apply_translation_update(translated(update)))
        self.assertTrue(m.apply_translation_update(translated(update, "こんにちは", target="ja", translation_epoch=1)))

    def test_disable_enable_and_debounced_noop_resume(self):
        m = self.module()
        update = source(1, "hello")
        m.apply_update(update)
        m.set_translation_enabled(False)
        self.assertFalse(m.apply_translation_update(translated(update)))
        m.set_translation_enabled(True)
        self.assertFalse(m.apply_translation_update(translated(update)))
        self.assertFalse(m.apply_translation_context(context()))
        self.assertTrue(m.awaiting_translation_context)
        self.assertTrue(m.apply_translation_context(context(epoch=1)))
        self.assertFalse(m.apply_translation_update(translated(update)))
        self.assertTrue(m.apply_translation_update(translated(update, translation_epoch=1)))

    def test_source_session_reset_and_new_pipeline_reject_old_results(self):
        m = self.module()
        old = source(1, "old")
        m.apply_update(old)
        m.apply_translation_context(context(epoch=1, session=2))
        self.assertEqual(m.snapshot().entries, ())
        self.assertFalse(m.apply_update(old))
        self.assertFalse(m.apply_translation_update(translated(old)))
        self.assertFalse(m.apply_translation_context(context(epoch=99)))
        m.reset("en", "zh")
        self.assertFalse(m.apply_translation_update(translated(old)))
        m.apply_translation_context(context(pipeline="new"))
        self.assertFalse(m.apply_update(old))

    def test_identity_and_revision_are_not_weakened_for_prefix_translation(self):
        m = self.module()
        update = source(3, "hello world")
        m.apply_update(update)
        result = translated(update)
        for fields in ({"session_id": 2}, {"pipeline_id": "wrong"}, {"source_id": "other"},
                       {"translation_epoch": 9}, {"source_language": "fr"},
                       {"source_text": "goodbye"}, {"revision": 9}, {"final": True}):
            self.assertFalse(m.apply_translation_update(replace(result, **fields)), fields)
        self.assertEqual(m.snapshot().latest_translation, "")
        self.assertFalse(m.apply_translation(3, "无身份", "zh"))

    def test_source_and_translation_source_use_identical_display_normalization(self):
        m = self.module()
        update = source(1, " hello\r\nworld ")
        m.apply_update(update)
        m.apply_translation_update(translated(update))
        self.assertEqual(m.snapshot().entries, (("hello\nworld", "你好"),))

    def test_history_and_future_orphans_remain_bounded(self):
        m = self.module()
        for seq in range(1, 400):
            m.apply_translation_update(translated(source(seq, "future", block=seq)))
        self.assertEqual(len(m.history._pending_translations), 64)
        for seq in range(400, 800):
            m.apply_update(source(seq, str(seq), block=seq, final=True))
        self.assertEqual(len(m.history.blocks), 64)
        self.assertEqual(len(m.history._sequence_blocks), 64)
        self.assertFalse(m.history._pending_translations)
        self.assertFalse(m.apply_translation_update(translated(source(399, "future", block=399))))


class FakeWindow:
    def __init__(self):
        self.jobs = {}
        self.next_id = 0
    def after(self, ms, callback):
        self.next_id += 1
        self.jobs[self.next_id] = (ms, callback)
        return self.next_id
    def after_cancel(self, job):
        self.jobs.pop(job, None)
    def winfo_fpixels(self, _):
        return 96


class Value:
    def __init__(self, value=None):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class DisplayOverlayIntegrationTests(unittest.TestCase):
    def overlay(self):
        item = OverlayWindow.__new__(OverlayWindow)
        item.settings = Settings()
        item.render_scale = 1
        item.canvas = SimpleNamespace(winfo_width=lambda: 680, winfo_height=lambda: 300)
        item.window = FakeWindow()
        item._fonts = {("label", 1): ImageFont.load_default()}
        item._entries = []
        item._entry_keys = []
        item._next_entry_key = 0
        item._entry_revision_modes = []
        item._entry_translation_pending = []
        item._entry_translation_states = []
        item._entries_view = []
        item._latest_translation_state = "waiting"
        item._translation_activity = None
        item._lane_idle_after_id = None
        item._reading_cursors = {style: ReadingCursor() for style in ("source", "translation")}
        item._layout_cache_key = None
        item._layout_cache = None
        item._ledger = ParagraphLedger()
        item._show_translation = True
        item._same_language = False
        item._follow_tail = True
        item._translation_lane_state = TranslationLaneState()
        item._translation_lane_state.configure("en", "zh", False)
        item._scroll_motion = Motion1D(0, value=0)
        item._scroll_target = 0
        item._auto_scroll_after = None
        item._drag_mode = ""
        item._scrollbar_show_until = 0
        item._update_auto_height = lambda _: False
        item._layer = SimpleNamespace(present=lambda _: None)
        item._request_redraw = lambda: None
        item.on_presented = None
        return item

    def app(self):
        app = SimpleNamespace(
            _display=DisplayModule("en", "zh"), overlay=self.overlay(),
            _has_source=False, _stopping=False, _last_error="", _in_event_drain=True,
            _pending_presentations={}, _trace_versions={}, _last_metrics={},
            source_display_var=Value(), translation_display_var=Value(), translation_status_var=Value(),
            translation_enabled_var=Value(True), _runtime_phase="recognizing",
            _set_runtime_phase=lambda _: None, _apply_translation_visibility=lambda _: None,
        )
        app._history = app._display.history
        for method in ("_show_timed_source", "_show_translation_update", "_show_translation_context", "_flush_display", "_record_visible_frame"):
            setattr(app, method, getattr(Application, method).__get__(app))
        app._show_translation_context(context())
        return app

    def render(self, item):
        drawn = []
        with patch("overlay.ImageFont.truetype", return_value=ImageFont.load_default()), patch.object(
            item, "_draw_line", side_effect=lambda _image, line, *_: drawn.append(dict(line))
        ):
            item._draw_content(24, 0)
        return drawn

    def header(self, item):
        seen = []
        draw = SimpleNamespace(text=lambda _pos, text, **_: seen.append(text), line=lambda *_, **__: None)
        item._draw_header(draw, ImageFont.load_default(), 680)
        return seen[0]

    def test_application_partial_to_snapshot_to_actual_overlay_layout_and_draw(self):
        app = self.app()
        update = source(1, "hello")
        app._show_timed_source(update)
        app._show_translation_update(translated(update))
        app._flush_display()
        lines = self.render(app.overlay)
        self.assertEqual([line["text"] for line in lines], ["hello", "你好"])
        self.assertEqual(app.translation_display_var.value, "你好")
        self.assertEqual(app.translation_status_var.value, "译文草稿 · 会继续修订")
        self.assertIn("草稿", self.header(app.overlay))
        self.assertFalse(lines[-1]["draft"])  # 可读译文保留现有正文绘制样式。

    def test_append_rewrite_and_final_update_the_drawn_translation_and_header(self):
        app = self.app()
        first = source(1, "hello")
        app._show_timed_source(first)
        app._show_translation_update(translated(first))
        app._show_timed_source(source(2, "hello world"))
        app._flush_display()
        self.assertEqual([line["text"] for line in self.render(app.overlay)], ["hello world", "你好"])
        self.assertIn("更新中", self.header(app.overlay))
        changed = source(3, "goodbye", final=True)
        app._show_timed_source(changed)
        app._flush_display()
        self.assertNotIn("你好", [line["text"] for line in self.render(app.overlay)])
        self.assertEqual(app.translation_status_var.value, "正在翻译当前内容…")
        app._show_translation_update(translated(changed, "再见"))
        app._flush_display()
        self.assertEqual([line["text"] for line in self.render(app.overlay)], ["goodbye", "再见"])
        self.assertEqual(app.translation_status_var.value, "本段译文已定稿")
        self.assertNotIn("更新中", self.header(app.overlay))

    def test_queued_translation_is_blocked_by_controller_stop_and_restart(self):
        app = self.app()
        jobs = []
        class Pipeline:
            def __init__(self, _options, **callbacks):
                self.callbacks = callbacks
            def start(self): pass
            def stop(self): pass
        controller = SessionController(lambda *job: jobs.append(job), Pipeline)
        handlers = {"on_translation_update": app._show_translation_update, "on_stopped": lambda: None}
        first = source(1, "hello")
        app._show_timed_source(first)
        old = controller.start({}, handlers)
        old.callbacks["on_translation_update"](translated(first, "不应显示"))
        controller.stop()
        old.callbacks["on_stopped"]()
        while jobs:
            callback, *args = jobs.pop(0)
            callback(*args)
        controller.start({}, handlers)
        old.callbacks["on_translation_update"](translated(first, "也不应显示"))
        while jobs:
            callback, *args = jobs.pop(0)
            callback(*args)
        app._flush_display()
        self.assertEqual(app.translation_display_var.value, "译文随原文更新。")
        self.assertNotIn("不应显示", [line["text"] for line in self.render(app.overlay)])

    def test_controller_records_rich_translation_as_ready(self):
        app = self.app()
        jobs = []
        class Pipeline:
            def __init__(self, _options, **callbacks):
                self.callbacks = callbacks
            def start(self): pass
            def stop(self): pass
        controller = SessionController(lambda *job: jobs.append(job), Pipeline)
        p = controller.start({}, {"on_translation_update": app._show_translation_update})
        update = source(1, "hello")
        app._show_timed_source(update)
        p.callbacks["on_translation_update"](translated(update))
        callback, *args = jobs.pop(0)
        callback(*args)
        self.assertEqual(controller.state.translation_phase, "ready")

    def test_debounced_disable_enable_refreshes_final_translation_without_new_audio(self):
        app = self.app()
        update = source(1, "hello", final=True)
        app._show_timed_source(update)
        app._show_translation_update(translated(update))
        app._flush_display()
        self.assertEqual(app.translation_display_var.value, "你好")
        app._display.set_translation_enabled(False)
        app._display.set_translation_enabled(True)
        jobs = []
        options = SimpleNamespace(source_language="en", target_language="zh",
                                  translation_backend="backend-a", translation_enabled=True)
        class Pipeline:
            def __init__(self, _options, **callbacks):
                self.options = _options
                self.callbacks = callbacks
                self.refreshes = 0
            def start(self): pass
            def stop(self): pass
            def update_options(self, _options):
                self.callbacks["on_translation_context"](context())
                self.callbacks["on_translation_update"](translated(update, "排队旧结果"))
                return False
            def refresh_translation_context(self):
                self.refreshes += 1
                self.callbacks["on_translation_context"](context(epoch=1))
                self.callbacks["on_translation_update"](translated(update, translation_epoch=1))
                return True
        controller = SessionController(lambda *job: jobs.append(job), Pipeline)
        handlers = {"on_translation_context": app._show_translation_context,
                    "on_translation_update": app._show_translation_update}
        app._controller = controller
        app.pipeline = controller.start(options, handlers)
        app._live_update_after_id = None
        app._closing = False
        app._pipeline_options_from_ui = lambda: options
        app._set_status = lambda _: None
        Application._apply_live_configuration(app)
        self.assertEqual(app.pipeline.refreshes, 1)
        for _ in range(2):
            callback, *args = jobs.pop(0)
            callback(*args)
        self.assertEqual(app._display.snapshot().latest_translation, "")
        self.assertTrue(app._display.awaiting_translation_context)
        while jobs:
            callback, *args = jobs.pop(0)
            callback(*args)
        app._flush_display()
        self.assertEqual(app.translation_display_var.value, "你好")
        self.assertEqual(app.translation_status_var.value, "本段译文已定稿")
        self.assertFalse(app._display.awaiting_translation_context)

    def test_trace_correlates_visible_draft_with_exact_request_and_redacts_by_default(self):
        for include_text in (False, True):
            with self.subTest(include_text=include_text), tempfile.TemporaryDirectory() as folder:
                app = self.app()
                update = source(1, "hello")
                result = translated(update, queued_at=1.1, started_at=1.2, finished_at=1.3)
                trace = RuntimeTrace(folder, include_text=include_text)
                with patch("main.get_runtime_trace", return_value=trace):
                    app._show_timed_source(update)
                    app._flush_display()
                    self.render(app.overlay)
                    self.assertFalse(app.overlay.visible_translation_fragments)
                    Application._display_presented(app, 2.0, set())
                    app._show_translation_update(result)
                    app._flush_display()
                    self.render(app.overlay)
                    Application._display_presented(app, 2.1, set())
                trace.close()
                self.assertTrue(trace._closed.wait(1))
                rows = [json.loads(line) for line in (trace.directory / "events.jsonl").read_text(encoding="utf8").splitlines()]
                outputs = [row for row in rows if row["kind"] == "translation"]
                frames = [row for row in rows if row["kind"] == "translation_frame"]
                self.assertEqual(len(outputs), 1)
                self.assertEqual(len(frames), 1)  # 占位行不是一次译文输出。
                self.assertEqual([outputs[0][key] for key in ("queued_at", "started_at", "finished_at")], [1.1, 1.2, 1.3])
                visible = frames[0]["visible"][0]
                self.assertEqual((visible["pipeline_id"], visible["session_id"], visible["sequence"], visible["translation_epoch"]), ("p", 1, 1, 0))
                self.assertEqual(visible["translation_state"], "draft")
                self.assertFalse(visible["final"])
                self.assertEqual("source_text" in outputs[0], include_text)
                self.assertEqual("source_text" in visible, include_text)
                self.assertEqual("text" in visible["fragments"][0], include_text)

    def test_disable_silence_late_metrics_and_enable_restore_truthful_status(self):
        app = self.app()
        queued = []
        app._queue_live_configuration = lambda: queued.append(app.translation_enabled_var.get())
        update = source(1, "hello", final=True)
        app._show_timed_source(update)
        app._show_translation_update(translated(update))
        app._flush_display()
        self.assertEqual(app.translation_status_var.value, "本段译文已定稿")
        app.translation_enabled_var.set(False)
        Application._on_translation_enabled_changed(app)
        self.assertEqual(app.translation_status_var.value, "翻译已关闭。")
        app._show_translation_context(context(epoch=1, enabled=False))
        app._flush_display()
        for phase in ("ready", "translating", "overloaded", "error"):
            Application._show_metrics(app, {"translation_phase": phase})
            self.assertEqual(app.translation_status_var.value, "翻译已关闭。")
        self.assertIsNone(app._display.take_snapshot())
        self.assertFalse(app.overlay._show_translation)
        app._show_translation_update(translated(update, "旧结果"))
        app._flush_display()
        self.assertEqual(app.translation_status_var.value, "翻译已关闭。")
        app.translation_enabled_var.set(True)
        Application._on_translation_enabled_changed(app)
        self.assertEqual(app.translation_status_var.value, "正在翻译当前内容…")
        app._show_translation_update(translated(update, "旧epoch结果"))
        self.assertEqual(app.translation_status_var.value, "正在翻译当前内容…")
        app._show_translation_context(context(epoch=2))
        app._show_translation_update(translated(update, translation_epoch=2))
        app._flush_display()
        self.assertEqual(app.translation_status_var.value, "本段译文已定稿")
        self.assertEqual(app.translation_display_var.value, "你好")
        self.assertTrue(app.overlay._show_translation)
        self.assertEqual(queued, [False, True])

    def test_enable_without_any_source_restores_default_hint(self):
        app = self.app()
        app._queue_live_configuration = lambda: None
        app.translation_enabled_var.set(False)
        Application._on_translation_enabled_changed(app)
        self.assertEqual(app.translation_status_var.value, "翻译已关闭。")
        app.translation_enabled_var.set(True)
        Application._on_translation_enabled_changed(app)
        self.assertEqual(app.translation_status_var.value, "译文随原文更新。")
        self.assertEqual(app._display.snapshot().latest_source, "")

    def test_enabled_manual_same_language_still_reports_no_translation_needed(self):
        app = self.app()
        app._display = DisplayModule("en", "en")
        app._flush_display()
        self.assertEqual(app.translation_status_var.value, "当前原文与目标语言相同，无需翻译。")

    def test_queued_translation_error_is_hidden_when_off_and_current_error_shows_when_enabled(self):
        app = self.app()
        app._queue_live_configuration = lambda: None
        messages = []
        app._set_status = messages.append
        app._last_error = "已有识别错误"
        app.translation_enabled_var.set(False)
        Application._on_translation_enabled_changed(app)
        Application._show_error(app, "翻译暂不可用，旧请求失败")
        self.assertEqual(app.translation_status_var.value, "翻译已关闭。")
        self.assertEqual(app._last_error, "已有识别错误")
        self.assertEqual(messages, [])
        app.translation_enabled_var.set(True)
        Application._on_translation_enabled_changed(app)
        Application._show_metrics(app, {"translation_phase": "error"})
        Application._show_error(app, "翻译暂不可用，当前请求失败")
        self.assertEqual(app.translation_status_var.value, "翻译暂不可用，当前请求失败")
        self.assertEqual(messages, ["翻译暂不可用，当前请求失败"])
        self.assertEqual(app._last_error, "已有识别错误")

    def test_successful_current_translation_clears_error_or_overload_but_stale_result_does_not(self):
        for phase in ("error", "overloaded"):
            with self.subTest(phase=phase):
                app = self.app()
                update = source(1, "hello", final=True)
                app._show_timed_source(update)
                app._last_metrics["translation_phase"] = phase
                message = "翻译暂不可用" if phase == "error" else "翻译有积压"
                app.translation_status_var.set(message)
                app._show_translation_update(translated(update, translation_epoch=99))
                app._flush_display()
                self.assertEqual(app._last_metrics["translation_phase"], phase)
                self.assertEqual(app.translation_status_var.value, message)
                app._show_translation_update(translated(update))
                app._flush_display()
                self.assertEqual(app._last_metrics["translation_phase"], "ready")
                self.assertEqual(app.translation_status_var.value, "本段译文已定稿")


if __name__ == "__main__":
    unittest.main()
