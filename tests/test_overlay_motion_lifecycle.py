"""Actual OverlayWindow scheduling with the production spring, without Tk windows."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from overlay import OverlayWindow
from subtitle_layout import Motion1D


class OverlayMotionLifetimeTests(unittest.TestCase):
    def item(self):
        item=OverlayWindow.__new__(OverlayWindow)
        item._closed=False
        item.settings=SimpleNamespace(overlay_animation=True,overlay_topmost=True,
                                      overlay_font_size=28,overlay_background_opacity=.3)
        item._reduced_motion=False
        item._configuration_active=False
        item._animation_after='animation'
        item._redraw_after=None
        item._redraw_pending=False
        item._bounds_after_id=None
        item._screen_poll_after_id=None
        item._separate_layers=False
        item._follow_tail=False
        item._scroll_target=100
        item._trans_scroll_target=200
        item._tail_offset=300
        item._scroll_motion=Motion1D(0)
        item._trans_scroll_motion=Motion1D(0)
        now=10.0
        with patch('subtitle_layout.time.monotonic',return_value=now):
            for motion,target in ((item._scroll_motion,100),(item._trans_scroll_motion,200)):
                motion.snap(0)
                motion.set_target(target)
                motion.advance(now+.12)
        item.window=Mock()
        item.window.winfo_exists.return_value=True
        item.window.winfo_viewable.return_value=True
        item.window.after.side_effect=lambda delay,callback:'queued-'+callback.__name__
        item.window.withdraw.side_effect=lambda:setattr(item.window.winfo_viewable,'return_value',False)
        item.canvas=Mock()
        item.canvas.winfo_width.return_value=1
        item.text_canvas=item.canvas
        item._menu=Mock()
        item._layer=Mock()
        item.window.grab_current.return_value=None
        item.persist_geometry=Mock()
        item._cancel_auto_scroll=Mock()
        item._refresh_mouse_passthrough=Mock()
        return item
    def settled(self,item):
        for motion,target in ((item._scroll_motion,item._scroll_target),
                              (item._trans_scroll_motion,item._trans_scroll_target)):
            self.assertEqual(motion.value,target)
            self.assertEqual(motion.velocity,0)
            self.assertFalse(motion.running)
    def test_hide_cancels_frames_settles_both_motions_preserves_reading_and_style(self):
        item=self.item()
        item._redraw_after='redraw';item._redraw_pending=True
        before=vars(item.settings).copy()
        self.assertGreater(item._scroll_motion.velocity,0)
        item.hide()
        item._layer.release_owned_capture.assert_called_once()
        self.settled(item)
        self.assertIsNone(item._animation_after)
        self.assertIsNone(item._redraw_after)
        self.assertFalse(item._redraw_pending)
        self.assertEqual({call.args[0] for call in item.window.after_cancel.call_args_list},{'animation','redraw'})
        self.assertFalse(item._follow_tail)
        self.assertEqual(vars(item.settings),before)
        item._ensure_animation_loop();item._animation_tick();item._request_redraw()
        item.window.after.assert_not_called()
    def test_hidden_tick_does_not_draw_or_queue_new_animation(self):
        item=self.item()
        item.window.winfo_viewable.return_value=False
        item._redraw=Mock()
        item._animation_tick();item._ensure_animation_loop()
        item._redraw.assert_not_called()
        item.window.after.assert_not_called()
    def test_disable_midflight_settles_and_schedules_only_ordinary_redraw(self):
        item=self.item();item.settings.overlay_animation=False
        item.apply_settings()
        self.settled(item)
        self.assertIsNone(item._animation_after)
        item.window.after_cancel.assert_called_once_with('animation')
        self.assertEqual([call.args[1].__name__ for call in item.window.after.call_args_list],['_finish_redraw'])
    def test_disabled_clamp_cannot_restart_same_or_new_target(self):
        item=self.item();item.settings.overlay_animation=False
        for target in (100,150,999,-10):
            item._scroll_target=target
            item._clamp_scroll_offset(100)
            self.assertEqual(item._scroll_motion.value,item._scroll_target)
            self.assertFalse(item._scroll_motion.running)
            self.assertEqual(item._scroll_motion.velocity,0)
    def test_reenable_keeps_zero_old_velocity_and_new_target_can_animate(self):
        item=self.item();item.settings.overlay_animation=False
        item.apply_settings()
        item.settings.overlay_animation=True
        item.apply_settings()
        self.settled(item)
        item._set_scroll_position(250)
        self.assertTrue(item._scroll_motion.running)
        self.assertEqual(item._scroll_motion.velocity,0)
        item._ensure_animation_loop()
        self.assertIsNotNone(item._animation_after)
    def test_system_reduced_motion_poll_settles_and_cancels_animation(self):
        item=self.item()
        item._screen_signature=('monitor',96)
        item.window.winfo_fpixels.return_value=96
        with patch('overlay.reduced_motion',return_value=True),patch('overlay.monitor_areas',return_value='monitor'):
            item._poll_screen_bounds()
        self.assertTrue(item._reduced_motion)
        self.settled(item)
        self.assertIsNone(item._animation_after)
        callbacks=[call.args[1].__name__ for call in item.window.after.call_args_list]
        self.assertNotIn('_animation_tick',callbacks)
        self.assertIn('_poll_screen_bounds',callbacks)
    def test_closed_queued_callbacks_do_not_touch_tk(self):
        item=self.item();item._closed=True
        for function in (item._request_redraw,item._finish_redraw,item._ensure_animation_loop,
                         item._animation_tick,item._poll_screen_bounds,item._queue_bounds_check,
                         item._finish_bounds_check,item._advance_reading,item.show,item.hide,item.apply_settings):
            function()
        item._on_configure(SimpleNamespace(widget=item.window))
        item.window.assert_not_called()
        self.assertFalse(item.window.method_calls)
    def test_tick_catches_disable_even_before_settings_callback(self):
        item=self.item();item.settings.overlay_animation=False
        item._redraw=Mock()
        item._animation_tick()
        self.settled(item)
        self.assertIsNone(item._animation_after)
        item.window.after.assert_not_called()
        item._redraw.assert_called_once()


if __name__=='__main__':
    unittest.main()
