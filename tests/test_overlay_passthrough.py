"""Overlay input transition contract without Tk windows or desktop input."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from overlay import OverlayWindow
from layered_window import MousePassthroughResult
from subtitle_layout import Motion1D


class OverlayPassthroughTests(unittest.TestCase):
    def item(self):
        item = OverlayWindow.__new__(OverlayWindow)
        item._closed = False
        item._tk_thread_id = threading.get_ident()
        item._mouse_passthrough_result = MousePassthroughResult(True, False, False, '', 100)
        item.on_mouse_passthrough_changed = Mock()
        item._drag_mode = 'resize'
        item._grip_hover = True
        item._scrollbar_show_until = 10.0
        item._follow_tail = False
        item._scroll_target = 50.0
        item._scroll_motion = Motion1D(0, value=10.0)
        item.settings = SimpleNamespace(overlay_mouse_passthrough=False, overlay_font_size=28,
                                       overlay_text_color='#44ccdd', overlay_background_opacity=0.75)
        item.canvas = Mock()
        item.text_canvas = item.canvas
        item._menu = Mock()
        item.window = Mock()
        item.window.grab_current.return_value = None
        item.window.winfo_exists.return_value = True
        item._layer = Mock()
        def set_mode(enabled):
            result = MousePassthroughResult(True, enabled, enabled, '', 100)
            item._publish_mouse_passthrough(result)
            return result
        item._layer.set_mouse_passthrough.side_effect = set_mode
        return item
    def test_enable_cleans_owned_pointer_and_shares_immutable_result(self):
        item = self.item()
        before = vars(item.settings).copy()
        grab = Mock()
        grab.winfo_toplevel.return_value = item.window
        item.window.grab_current.return_value = grab
        result = item.set_mouse_passthrough(True)
        self.assertTrue(result.success)
        self.assertIs(result, item.on_mouse_passthrough_changed.call_args.args[0])
        with self.assertRaises(AttributeError):
            result.effective = False
        grab.grab_release.assert_called_once()
        item._menu.unpost.assert_called_once()
        item._layer.release_owned_capture.assert_called_once()
        self.assertEqual(item._drag_mode, '')
        self.assertFalse(item._grip_hover)
        self.assertEqual(item._scroll_target, 50.0)
        self.assertEqual(item._scroll_motion.value, 10.0)
        self.assertFalse(item._follow_tail)
        self.assertEqual(vars(item.settings), before)
    def test_foreign_grab_untouched_and_cleanup_failure_does_not_enable(self):
        item = self.item()
        grab = Mock()
        grab.winfo_toplevel.return_value = object()
        item.window.grab_current.return_value = grab
        self.assertTrue(item.set_mouse_passthrough(True).success)
        grab.grab_release.assert_not_called()
        item = self.item()
        item.window.grab_current.side_effect = RuntimeError('query failed')
        result = item.set_mouse_passthrough(True)
        self.assertFalse(result.success)
        self.assertFalse(result.effective)
        item._layer.set_mouse_passthrough.assert_not_called()
        self.assertIs(result, item.on_mouse_passthrough_changed.call_args.args[0])
    def test_disabled_restore_remains_available_after_unknown(self):
        item = self.item()
        item._publish_mouse_passthrough(MousePassthroughResult(False, True, None, 'failed', 100))
        self.assertIsNone(item.mouse_passthrough)
        result = item.set_mouse_passthrough(False)
        self.assertTrue(result.success)
        self.assertFalse(item.mouse_passthrough)
    def test_late_mouse_callbacks_do_nothing_for_enabled_unknown_or_closed(self):
        for effective in (True, None):
            item = self.item()
            item._mouse_passthrough_result = MousePassthroughResult(False, True, effective)
            event = object()  # Accessing coordinates would fail if any body still runs.
            for name in ('_on_button_press', '_on_drag', '_on_button_release', '_on_motion', '_on_mouse_wheel', '_show_menu'):
                getattr(item, name)(event)
            self.assertEqual(item._drag_mode, 'resize')
            item.canvas.grab_set.assert_not_called()
            item._menu.tk_popup.assert_not_called()
            item._closed = True
            item._mouse_passthrough_result = MousePassthroughResult(True, False, False)
            item._on_drag(event)
    def test_wrong_thread_or_closed_returns_rejection_without_callback_or_tk(self):
        item = self.item()
        results = []
        worker = threading.Thread(target=lambda: results.append(item.set_mouse_passthrough(True)))
        worker.start(); worker.join(2)
        self.assertFalse(results[0].success)
        item.on_mouse_passthrough_changed.assert_not_called()
        item._closed = True
        self.assertFalse(item.set_mouse_passthrough(False).success)
        item.window.grab_current.assert_not_called()
        item._layer.set_mouse_passthrough.assert_not_called()
    def test_map_refresh_only_for_own_toplevel(self):
        item = self.item()
        item._on_map(SimpleNamespace(widget=object()))
        item._layer.refresh_mouse_passthrough.assert_not_called()
        item._on_map(SimpleNamespace(widget=item.window))
        item._layer.refresh_mouse_passthrough.assert_called_once()
    def test_show_uses_nonactivating_tk_state_in_enabled_or_unknown_mode(self):
        for effective in (True, None, False):
            item = self.item()
            item._mouse_passthrough_result = MousePassthroughResult(True, bool(effective), effective)
            item._set_bounded_geometry = Mock()
            item._current_rect = Mock()
            item._enforce_bounds = Mock()
            item._sync_layer_geometry = Mock()
            item._redraw = Mock()
            item._separate_layers = False
            item._configuration_active = False
            item.show()
            if effective is False:
                item.window.deiconify.assert_called_once()
                item.window.state.assert_not_called()
            else:
                item.window.state.assert_called_once_with('normal')
                item.window.deiconify.assert_not_called()
            item.window.focus_force.assert_not_called()
    def test_close_is_idempotent_and_stops_callbacks_and_native_lifetime(self):
        item = self.item()
        item.persist_geometry = Mock()
        item._cancel_auto_scroll = Mock()
        item._cancel_lane_idle_check = Mock()
        item._animation_after = None
        item._redraw_after = None
        item._separate_layers = False
        item.close()
        item.close()
        item._layer.close.assert_called_once()
        item.window.destroy.assert_called_once()
        self.assertIsNone(item.on_mouse_passthrough_changed)
        self.assertFalse(item.set_mouse_passthrough(True).success)
    def test_apply_settings_refreshes_native_without_altering_user_values(self):
        item = self.item()
        item.settings.overlay_topmost = True
        item._configuration_active = False
        item._request_redraw = Mock()
        before = vars(item.settings).copy()
        item.apply_settings()
        item._layer.refresh_mouse_passthrough.assert_called_once()
        self.assertEqual(vars(item.settings), before)


if __name__ == '__main__':
    unittest.main()
