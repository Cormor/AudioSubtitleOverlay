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

    def test_auto_keeps_lane_while_source_is_collecting(self):
        """原文持续采集时不收起译文栏，避免左右栏被拆散。"""
        s=self.state()
        for t in (0,1,2,3,4,5,6,10,20,60):s.source_updated(t);self.assertTrue(s.visible)
        self.assertFalse(s.maybe_collapse_if_idle(60.1))

    def test_auto_collapses_only_after_source_idle_and_no_translation(self):
        s=self.state()
        for t in (0,1,2,3,4,5):s.source_updated(t)
        self.assertTrue(s.visible)
        # 原文停止后再等 timeout，仍无译文才收起
        self.assertFalse(s.maybe_collapse_if_idle(5.5))
        self.assertTrue(s.maybe_collapse_if_idle(11.0))
        self.assertFalse(s.visible)

    def test_translation_recovers_and_restarts_timeout(self):
        s=self.state()
        for t in (0,1,2,3,4,5):s.source_updated(t)
        s.translation_output(5.2);self.assertTrue(s.visible)
        for t in (6,7,8,9,10):s.source_updated(t)
        self.assertTrue(s.visible)
        s.translation_output(12)
        self.assertFalse(s.maybe_collapse_if_idle(13))
        self.assertFalse(s.maybe_collapse_if_idle(20))

    def test_idle_time_does_not_close_or_consume_new_active_period(self):
        s=self.state();s.source_updated(0)
        self.assertTrue(s.visible)
        s.source_updated(100);self.assertTrue(s.visible)
        for t in (101,102,103,104,105):s.source_updated(t)
        self.assertTrue(s.visible)
        self.assertTrue(s.maybe_collapse_if_idle(111))
        self.assertFalse(s.visible)

    def test_detected_language_switch_does_not_reset_auto_timeout(self):
        s=self.state()
        for i in range(6):
            s.configure('en' if i%2 else 'zh','zh',True);s.source_updated(i)
        self.assertTrue(s.visible)
        self.assertTrue(s.maybe_collapse_if_idle(12))

    def test_user_language_change_resets_state(self):
        s=self.state()
        for i in range(6):s.source_updated(i)
        self.assertTrue(s.maybe_collapse_if_idle(12))
        self.assertFalse(s.visible)
        s.configure(None,'en',True);self.assertTrue(s.visible)
        s.configure('zh','zh',False);self.assertFalse(s.visible)
        s.configure('en','zh',False);self.assertTrue(s.visible)

    def test_idle_timer_path_collapses_without_further_snapshots(self):
        """运行时定时路径：原文停住后由 timer 收起，不依赖新快照。"""
        s = self.state()
        for t in (0, 1, 2):
            s.source_updated(t)
        self.assertTrue(s.visible)
        # 模拟 Tk after 到期：到 timeout+ε 后由 maybe_collapse 收起
        changed = s.maybe_collapse_if_idle(2 + TranslationLaneState.timeout + 0.2)
        self.assertTrue(changed)
        self.assertFalse(s.visible)

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


class _FakeWindow:
    """最小 Tk after 替身，验证运行时会注册定时器。"""

    def __init__(self):
        self.jobs = {}
        self._next = 1

    def after(self, ms, callback):
        job_id = self._next
        self._next += 1
        self.jobs[job_id] = (ms, callback)
        return job_id

    def after_cancel(self, job_id):
        self.jobs.pop(job_id, None)


class OverlayIdleTimerTests(unittest.TestCase):
    """覆盖作者指出的运行时路径：set_entries / 定时器，而非只调状态函数。"""

    def _overlay_stub(self):
        from types import SimpleNamespace
        from overlay import OverlayWindow

        lane = TranslationLaneState()
        lane.configure(None, "zh", True)
        redraws = []

        stub = SimpleNamespace(
            window=_FakeWindow(),
            _translation_lane_state=lane,
            _lane_idle_after_id=None,
            _layout_cache_key="x",
            _translation_lane_visible=lambda: bool(lane.visible),
            _request_redraw=lambda: redraws.append(1),
        )
        # 绑定 OverlayWindow 的运行时方法（真正执行调度/定时回调）。
        for name in (
            "_cancel_lane_idle_check",
            "_schedule_lane_idle_check",
            "_on_lane_idle_timer",
            "_run_lane_idle_check",
        ):
            setattr(stub, name, getattr(OverlayWindow, name).__get__(stub))
        return OverlayWindow, stub, redraws

    def test_source_update_registers_tk_after(self):
        """最后一次 source 后应注册 Tk 定时检查。"""
        OverlayWindow, stub, _ = self._overlay_stub()
        stub._translation_lane_state.source_updated(0.0)
        OverlayWindow._schedule_lane_idle_check(stub)
        self.assertTrue(stub.window.jobs)
        ms, _callback = next(iter(stub.window.jobs.values()))
        self.assertGreater(ms, 200)

    def test_timer_callback_collapses_when_no_translation(self):
        """定时器到期且始终无译文时收起译文栏。"""
        OverlayWindow, stub, redraws = self._overlay_stub()
        lane = stub._translation_lane_state
        for t in (0.0, 1.0, 2.0):
            lane.source_updated(t)
        OverlayWindow._schedule_lane_idle_check(stub)
        _job_id, (ms, callback) = next(iter(stub.window.jobs.items()))
        # 模拟 after 到期
        lane.last_source_at = 2.0
        lane.missing_since = 0.0
        lane.got_translation = False
        # 把 now 推到超时之后
        import time as _time
        # 直接调用定时器回调（运行时路径）
        # maybe_collapse 使用 time.perf_counter()，先 mock
        real = _time.perf_counter
        _time.perf_counter = lambda: 10.0
        try:
            callback()
        finally:
            _time.perf_counter = real
        self.assertFalse(lane.visible)
        self.assertTrue(redraws)

    def test_new_source_reschedules_timer(self):
        OverlayWindow, stub, _ = self._overlay_stub()
        OverlayWindow._schedule_lane_idle_check(stub)
        first = next(iter(stub.window.jobs.keys()))
        OverlayWindow._schedule_lane_idle_check(stub)
        self.assertNotIn(first, stub.window.jobs)
        self.assertTrue(stub.window.jobs)
