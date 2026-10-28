"""Automatic button capture and owned cleanup without desktop input."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from overlay import OverlayWindow
from layered_window import MousePassthroughResult


class AutomaticDragCaptureTests(unittest.TestCase):
    def item(self):
        item=OverlayWindow.__new__(OverlayWindow)
        item._closed=False
        item._mouse_passthrough_result=MousePassthroughResult(True,False,False)
        item.window=Mock()
        item.window.winfo_width.return_value=680
        item.window.winfo_height.return_value=256
        item.window.winfo_x.return_value=60
        item.window.winfo_y.return_value=100
        item.canvas=Mock()
        item.text_canvas=item.canvas;item._menu=Mock();item._layer=Mock()
        item.window.grab_current.return_value=None
        item._tk_thread_id=threading.get_ident()
        item.on_mouse_passthrough_changed=Mock()
        item._scrollbar_geometry=Mock(return_value=dict(left=650,right=660,top=25,bottom=230,thumb_top=40,thumb_height=30))
        item._full_tail_offset=Mock(return_value=0)
        item._in_resize_grip=Mock(return_value=False)
        return item

    def test_press_keeps_automatic_button_capture_for_move_and_resize(self):
        for resizing in (False,True):
            item=self.item();item._in_resize_grip.return_value=resizing
            item._on_button_press(SimpleNamespace(x=666,y=244,x_root=726,y_root=344))
            self.assertEqual(item._drag_mode,'resize' if resizing else 'move')
            self.assertEqual(item._drag_origin,(726,344))
            item.canvas.grab_set.assert_not_called()
            item.canvas.grab_set_global.assert_not_called()

    def test_scrollbar_press_still_establishes_reading_drag_without_regrab(self):
        item=self.item();item._full_tail_offset.return_value=100
        item._cancel_auto_scroll=Mock();item._reveal_scrollbar=Mock()
        item._on_button_press(SimpleNamespace(x=655,y=50,x_root=715,y_root=150))
        self.assertEqual(item._drag_mode,'scrollbar')
        self.assertEqual(item._scrollbar_grab,10)
        item._cancel_auto_scroll.assert_called_once()
        item.canvas.grab_set.assert_not_called()

    def test_blocked_press_does_not_establish_drag_or_take_capture(self):
        for effective in (True,None):
            item=self.item();item._drag_mode=''
            item._mouse_passthrough_result=MousePassthroughResult(False,True,effective)
            item._on_button_press(object())
            self.assertEqual(item._drag_mode,'')
            item.canvas.grab_set.assert_not_called()
            item.window.winfo_width.assert_not_called()

    def test_tk_grab_query_or_release_failure_still_attempts_native_cleanup(self):
        for stage in ('query','release','menu'):
            item=self.item();item._drag_mode='resize'
            if stage=='query':item.window.grab_current.side_effect=RuntimeError('query failed')
            elif stage=='release':
                item.window.grab_current.return_value=item.canvas
                item.canvas.grab_release.side_effect=RuntimeError('release failed')
            else:item._menu.unpost.side_effect=RuntimeError('menu failed')
            result=item.set_mouse_passthrough(True)
            self.assertFalse(result.success)
            self.assertEqual(item._drag_mode,'')
            item._layer.release_owned_capture.assert_called_once()
            item._layer.set_mouse_passthrough.assert_not_called()

    def test_hide_releases_automatic_capture_before_withdrawing(self):
        item=self.item();item._drag_mode='resize';item._separate_layers=False
        calls=[]
        item._layer.release_owned_capture.side_effect=lambda:calls.append('release')
        item.window.withdraw.side_effect=lambda:calls.append('withdraw')
        item.persist_geometry=Mock();item._cancel_auto_scroll=Mock();item._stop_reading_animation=Mock()
        item.hide()
        self.assertEqual(calls,['release','withdraw'])
        self.assertEqual(item._drag_mode,'')

    def test_hidden_cleanup_failure_leaves_window_visible_and_reports_failure(self):
        item=self.item();item._drag_mode='resize'
        item._layer.release_owned_capture.side_effect=OSError('native cleanup failed')
        item.persist_geometry=Mock();item._cancel_auto_scroll=Mock()
        with self.assertRaises(OSError):item.hide()
        item.window.withdraw.assert_not_called()
        self.assertEqual(item._drag_mode,'')

    def test_close_while_dragging_releases_native_capture_before_destroy(self):
        item=self.item();item._drag_mode='move';item._separate_layers=False
        item.persist_geometry=Mock();item._cancel_auto_scroll=Mock();item._cancel_lane_idle_check=Mock()
        item._stop_reading_animation=Mock()
        calls=[]
        item._layer.release_owned_capture.side_effect=lambda:calls.append('release')
        item.window.destroy.side_effect=lambda:calls.append('destroy')
        item.close()
        self.assertEqual(calls,['release','destroy'])
        self.assertEqual(item._drag_mode,'')

    def test_foreign_tk_grab_is_preserved_while_owned_native_capture_cleanup_runs(self):
        item=self.item();foreign=Mock();foreign.winfo_toplevel.return_value=object()
        item.window.grab_current.return_value=foreign
        item._release_owned_pointer()
        foreign.grab_release.assert_not_called()
        item._layer.release_owned_capture.assert_called_once()


if __name__=='__main__':
    unittest.main()
