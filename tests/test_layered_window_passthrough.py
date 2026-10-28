"""Win32 input styles and lifetime tested without creating a desktop window."""
import ctypes
from ctypes import wintypes
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from layered_window import LayeredWindow, MousePassthroughResult


class Function:
    def __init__(self, function):
        self.function = function
    def __call__(self, *args):
        return self.function(*args)


class NativeFixture:
    def __init__(self):
        self.hwnd = 100
        self.exists = True
        self.owner_pid = os.getpid()
        self.owner_thread = threading.get_native_id()
        self.styles = {100: 0x00040008}
        self.error = 0
        self.calls = []
        self.capture = 0
        self.children = {101}
        self.set_errors = []
        self.get_errors = []
        self.pos_errors = []
        self.drop_mask = 0
        self.buffer = ctypes.create_string_buffer(1024)
        self.user = SimpleNamespace(**{name: Function(function) for name, function in {
            'GetAncestor': lambda *_: self.hwnd,
            'IsWindow': lambda hwnd: self.exists and hwnd == self.hwnd,
            'GetWindowThreadProcessId': self.owner,
            'GetWindowLongPtrW': self.get_style,
            'SetWindowLongPtrW': self.set_style,
            'GetWindowLongW': self.get_style,
            'SetWindowLongW': self.set_style,
            'SetWindowPos': self.set_position,
            'GetCapture': lambda: self.capture,
            'IsChild': lambda hwnd, child: hwnd == self.hwnd and child in self.children,
            'ReleaseCapture': self.release_capture,
            'GetDC': lambda *_: 10,
            'ReleaseDC': lambda *_: self.calls.append(('release_dc',)) or 1,
            'UpdateLayeredWindow': lambda *_: self.calls.append(('present',)) or 1,
        }.items()})
        self.gdi = SimpleNamespace(**{name: Function(function) for name, function in {
            'CreateCompatibleDC': lambda *_: 11,
            'CreateDIBSection': self.create_bitmap,
            'SelectObject': lambda *_: 13,
            'DeleteObject': lambda *_: self.calls.append(('delete_bitmap',)) or 1,
            'DeleteDC': lambda *_: self.calls.append(('delete_dc',)) or 1,
        }.items()})
        self.window = SimpleNamespace(winfo_id=lambda: 101, winfo_exists=lambda: self.exists,
                                      winfo_x=lambda: 30, winfo_y=lambda: 40)
    def owner(self, hwnd, pointer):
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = self.owner_pid
        return self.owner_thread
    def get_style(self, hwnd, _):
        self.error = self.get_errors.pop(0) if self.get_errors else 0
        return 0 if self.error else self.styles.get(hwnd, 0)
    def set_style(self, hwnd, _, style):
        self.calls.append(('set', hwnd, style))
        self.error = self.set_errors.pop(0) if self.set_errors else 0
        if self.error:
            return 0
        before = self.styles.get(hwnd, 0)
        self.styles[hwnd] = int(style) & ~self.drop_mask
        return before
    def set_position(self, *args):
        self.calls.append(('position', args[-1]))
        self.error = self.pos_errors.pop(0) if self.pos_errors else 0
        return not self.error
    def release_capture(self):
        self.calls.append(('release_capture', self.capture))
        self.capture = 0
        return 1
    def create_bitmap(self, _, __, ___, pointer, *args):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(self.buffer)
        return 12
    def load(self, name, **kwargs):
        return self.user if name == 'user32' else self.gdi


class PassthroughNativeTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeFixture()
        self.patches = [patch('layered_window.c.WinDLL', self.native.load, create=True),
                        patch('layered_window.c.set_last_error', self.set_error, create=True),
                        patch('layered_window.c.get_last_error', lambda: self.native.error, create=True)]
        for item in self.patches:
            item.start()
        self.layer = LayeredWindow(self.native.window)
        self.results = []
        self.layer.on_mouse_passthrough_changed = self.results.append
    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
    def set_error(self, value):
        self.native.error = value
    def test_zero_previous_style_is_success_and_preserves_unrelated_flags(self):
        self.native.styles[100] = 0
        result = self.layer.set_mouse_passthrough(True)
        self.assertTrue(result.success)
        self.assertTrue(result.effective)
        self.assertIs(result, self.results[-1])
        self.assertEqual(self.native.calls[-1], ('position', 0x0037))
        self.native.styles[100] |= 0x00400008
        result = self.layer.set_mouse_passthrough(False)
        self.assertTrue(result.success)
        self.assertFalse(result.effective)
        self.assertEqual(self.native.styles[100], 0x00480008)
    def test_disable_restores_preexisting_noactivate(self):
        self.native.styles[100] |= LayeredWindow._NOACTIVATE
        before = self.native.styles[100]
        self.layer.set_mouse_passthrough(True)
        self.layer.set_mouse_passthrough(False)
        self.assertEqual(self.native.styles[100], before | LayeredWindow._LAYERED)
    def test_get_error_is_not_interpreted_as_zero_style(self):
        self.native.get_errors = [5]
        result = self.layer.set_mouse_passthrough(True)
        self.assertFalse(result.success)
        self.assertIsNone(result.effective)
        self.assertFalse(self.native.calls)
    def test_set_failure_restores_and_reports_known_off(self):
        before = self.native.styles[100]
        self.native.set_errors = [5]
        result = self.layer.set_mouse_passthrough(True)
        self.assertFalse(result.success)
        self.assertFalse(result.effective)
        self.assertEqual(self.native.styles[100], before)
    def test_frame_refresh_failure_rolls_back_managed_bits(self):
        before = self.native.styles[100]
        self.native.pos_errors = [5, 0]
        result = self.layer.set_mouse_passthrough(True)
        self.assertFalse(result.success)
        self.assertFalse(result.effective)
        self.assertEqual(self.native.styles[100], before)
    def test_failed_restore_is_unknown_and_not_retried_per_frame(self):
        self.native.pos_errors = [5]
        self.native.set_errors = [0, 5]
        result = self.layer.set_mouse_passthrough(True)
        self.assertFalse(result.success)
        self.assertIsNone(result.effective)
        sets = len([x for x in self.native.calls if x[0] == 'set'])
        for _ in range(50):
            self.layer.refresh_mouse_passthrough()
        self.assertEqual(len(self.results), 1)
        self.assertEqual(len([x for x in self.native.calls if x[0] == 'set']), sets)
        self.assertTrue(self.layer.set_mouse_passthrough(False).success)
        self.assertFalse(self.layer.mouse_passthrough)
    def test_readback_mismatch_is_rejected(self):
        self.native.drop_mask = LayeredWindow._TRANSPARENT
        result = self.layer.set_mouse_passthrough(True)
        self.assertFalse(result.success)
        self.assertFalse(result.effective)
    def test_new_hwnd_uses_its_own_noactivate_and_recovers_failed_state(self):
        self.layer.set_mouse_passthrough(True)
        self.native.hwnd = 200
        self.native.styles[200] = 0x00400008 | LayeredWindow._NOACTIVATE
        result = self.layer.refresh_mouse_passthrough()
        self.assertTrue(result.success)
        self.assertEqual(result.hwnd, 200)
        self.layer.set_mouse_passthrough(False)
        self.assertEqual(self.native.styles[200], 0x08480008)
    def test_foreign_pid_or_thread_never_mutated(self):
        for field in ('owner_pid', 'owner_thread'):
            original = getattr(self.native, field)
            setattr(self.native, field, original + 1)
            result = self.layer.set_mouse_passthrough(True)
            self.assertFalse(result.success)
            self.assertIsNone(result.effective)
            self.assertFalse(self.native.calls)
            setattr(self.native, field, original)
    def test_wrong_caller_and_closed_do_not_call_win32_or_callbacks(self):
        results = []
        thread = threading.Thread(target=lambda: results.append(self.layer.set_mouse_passthrough(True)))
        thread.start(); thread.join(2)
        self.assertFalse(results[0].success)
        self.assertFalse(self.native.calls)
        self.assertFalse(self.results)
        self.layer.close()
        self.layer.close()
        self.assertFalse(self.layer.set_mouse_passthrough(True).success)
        self.assertFalse(self.results)
    def test_capture_only_released_for_own_window_or_child(self):
        for capture in (999, 0, 100, 101):
            self.native.capture = capture
            self.layer.release_owned_capture()
            self.assertEqual(self.native.capture, capture if capture == 999 else 0)
        self.assertEqual([x[1] for x in self.native.calls if x[0] == 'release_capture'], [100, 101])
    def test_present_preserves_enabled_bits_without_repeated_set_and_cleans_gdi(self):
        self.layer.set_mouse_passthrough(True)
        before = len([x for x in self.native.calls if x[0] == 'set'])
        for _ in range(3):
            self.layer.present(Image.new('RGBA', (3, 2), (250, 100, 20, 255)))
        self.assertEqual(len([x for x in self.native.calls if x[0] == 'set']), before)
        for kind in ('present', 'delete_bitmap', 'delete_dc', 'release_dc'):
            self.assertEqual(sum(x[0] == kind for x in self.native.calls), 3)
        self.assertTrue(self.layer.mouse_passthrough)
    def test_external_style_reset_reapplies_without_touching_other_bits(self):
        self.layer.set_mouse_passthrough(True)
        self.native.styles[100] = 0x10400008
        self.layer.refresh_mouse_passthrough()
        self.assertEqual(self.native.styles[100], 0x18480028)
    def test_32_bit_get_set_api_selection(self):
        original = ctypes.sizeof
        with patch('layered_window.c.sizeof', side_effect=lambda value: 4 if value is ctypes.c_void_p else original(value)):
            layer = LayeredWindow(self.native.window)
        self.assertIs(layer._get_window_long, self.native.user.GetWindowLongW)
        self.assertIs(layer._set_window_long, self.native.user.SetWindowLongW)
    def test_typed_bool_rejects_truthy_strings_without_mutation(self):
        for value in ('false', 1, None):
            self.assertFalse(self.layer.set_mouse_passthrough(value).success)
        self.assertFalse(self.native.calls)


if __name__ == '__main__':
    unittest.main()
