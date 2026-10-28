"""Real Main/Tcl integration; mocked native acknowledgements, no windows/input."""
import json
from pathlib import Path
import tempfile
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

from main import Application
from layered_window import MousePassthroughResult
from settings import Settings
from tests import test_parameter_editing as parameter_tests


class MousePassthroughControlsTests(unittest.TestCase):
    def app(self, saved=False):
        app = parameter_tests.MainParameterEditingTests().application()
        app.settings.overlay_mouse_passthrough = saved
        app._initialize_mouse_passthrough(master=app._test_interpreter)
        app.mouse_passthrough_check = parameter_tests._EditorWidget(".mousecheck")
        app._sync_mouse_passthrough_control()
        app._queue_overlay_focus = lambda *_: None
        app.root.iconified = app.root.withdrawn = 0
        app.root.iconify = lambda: setattr(app.root, "iconified", app.root.iconified+1)
        app.root.withdraw = lambda: setattr(app.root, "withdrawn", app.root.withdrawn+1)
        class Overlay:
            def __init__(self):
                self.calls = []
                self.on_mouse_passthrough_changed = None
                self.result = None
                self.last_callback = None
            def set_mouse_passthrough(self, value):
                self.calls.append(value)
                result = self.result or MousePassthroughResult(True, value, value, hwnd=41)
                self.last_callback = self.on_mouse_passthrough_changed
                if self.last_callback:
                    self.last_callback(result)
                return result
            def set_configuration_active(self, value): pass
            def persist_geometry(self): pass
            def close(self): pass
        app.overlay = Overlay()
        app._connect_mouse_passthrough()
        app._test_preference_saves = []
        app._save_settings = lambda: app._test_preference_saves.append(app.settings.overlay_mouse_passthrough)
        return app

    def test_saved_true_is_applied_only_after_callback_and_external_card_are_available(self):
        app = self.app(True)
        app.overlay.on_mouse_passthrough_changed(MousePassthroughResult(True, False, False, hwnd=41))
        self.assertTrue(app.settings.overlay_mouse_passthrough)
        self.assertEqual(app.mouse_passthrough_var.get(), 0)
        self.assertFalse(app._test_preference_saves)
        app._start_mouse_passthrough()
        self.assertEqual(app.overlay.calls, [True])
        self.assertEqual(app.mouse_passthrough_var.get(), 1)
        self.assertTrue(app._mouse_passthrough_effective)
        app.root.run()
        self.assertFalse(app._test_preference_saves)  # same saved preference
        app._restore_mouse_operation()
        app.root.run()
        self.assertEqual(app.overlay.calls, [True, False])
        self.assertEqual(app._test_preference_saves, [False])

    def test_known_native_failure_reads_actual_bool_without_committing_requested_value(self):
        for saved, requested, actual in ((False, True, False), (True, False, True)):
            with self.subTest(saved=saved):
                app = self.app(saved)
                app._mouse_passthrough_initialized = True
                app.overlay.result = MousePassthroughResult(False, requested, actual, "native failure", 41)
                app.mouse_passthrough_var.set(int(requested))
                app._on_mouse_passthrough_toggle()
                self.assertEqual(app.mouse_passthrough_var.get(), int(actual))
                self.assertIs(app.settings.overlay_mouse_passthrough, saved)
                self.assertIn("切换未成功", app.mouse_passthrough_status_var.get())
                self.assertEqual(app.root.deiconify_calls, 1)
                app.root.run()
                self.assertFalse(app._test_preference_saves)

    def test_automatic_hwnd_refresh_cannot_replace_a_failed_explicit_user_preference(self):
        app = self.app(True)
        app.overlay.result = MousePassthroughResult(False, True, False, "initial failure", 41)
        app._start_mouse_passthrough()
        self.assertIsNone(app._mouse_passthrough_request)
        self.assertTrue(app.settings.overlay_mouse_passthrough)
        app.overlay.on_mouse_passthrough_changed(MousePassthroughResult(True, False, False, hwnd=42))
        app.root.run()
        self.assertEqual(app.mouse_passthrough_var.get(), 0)
        self.assertTrue(app._mouse_passthrough_committed)
        self.assertTrue(app.settings.overlay_mouse_passthrough)
        self.assertFalse(app._test_preference_saves)
        app.overlay.result = None
        app._restore_mouse_operation()
        app.root.run()
        self.assertFalse(app.settings.overlay_mouse_passthrough)
        self.assertEqual(app._test_preference_saves, [False])
        self.assertIsNone(app._mouse_passthrough_request)
        app.overlay.on_mouse_passthrough_changed(MousePassthroughResult(True, True, True, hwnd=43))
        app.root.run()
        self.assertEqual(app.mouse_passthrough_var.get(), 1)
        self.assertFalse(app.settings.overlay_mouse_passthrough)
        self.assertEqual(app._test_preference_saves, [False])

    def test_explicit_request_marker_is_cleared_even_if_setter_raises(self):
        app = self.app(True)
        def broken(_): raise RuntimeError("unexpected fake setter error")
        app.overlay.set_mouse_passthrough = broken
        with self.assertRaisesRegex(RuntimeError, "unexpected fake"):
            app._request_mouse_passthrough(False)
        self.assertIsNone(app._mouse_passthrough_request)
        app.overlay.on_mouse_passthrough_changed(MousePassthroughResult(True, False, False, hwnd=42))
        self.assertTrue(app.settings.overlay_mouse_passthrough)
        self.assertFalse(app._test_preference_saves)

    def test_unknown_state_is_alternate_not_false_and_restore_stays_available(self):
        app = self.app(True)
        app._mouse_passthrough_initialized = True
        app.overlay.result = MousePassthroughResult(False, True, None, "unknown", 41)
        app._request_mouse_passthrough(True)
        self.assertEqual(app.mouse_passthrough_var.get(), -1)
        self.assertIsNone(app._mouse_passthrough_effective)
        self.assertTrue(app.mouse_passthrough_check.instate(["alternate", "!selected"]))
        self.assertTrue(app._mouse_passthrough_committed)
        self.assertTrue(app.settings.overlay_mouse_passthrough)
        self.assertIn("状态未确认", app.mouse_passthrough_status_var.get())
        self.assertEqual(app.mouse_passthrough_status_var.get().count("保存偏好保持不变"), 1)
        app.overlay.result = None
        app._restore_mouse_operation()
        app.root.run()
        self.assertEqual(app.overlay.calls, [True, False])
        self.assertEqual(app.mouse_passthrough_var.get(), 0)
        self.assertTrue(app.mouse_passthrough_check.instate(["!alternate", "!selected"]))
        self.assertEqual(app._test_preference_saves, [False])

    def test_main_drives_checkbox_unknown_initially_then_known_off_on_and_unknown(self):
        app = self.app(True)
        self.assertTrue(app.mouse_passthrough_check.instate(["alternate", "!selected"]))
        app._start_mouse_passthrough()
        self.assertTrue(app.mouse_passthrough_check.instate(["!alternate", "selected"]))
        app._restore_mouse_operation()
        self.assertTrue(app.mouse_passthrough_check.instate(["!alternate", "!selected"]))
        app.overlay.result = MousePassthroughResult(False, True, None, "unknown", 41)
        app._request_mouse_passthrough(True)
        self.assertTrue(app.mouse_passthrough_check.instate(["alternate", "!selected"]))

    def test_recovery_button_shares_wide_row_and_wraps_when_requested_width_wont_fit(self):
        app = self.app()
        width = [600]
        app._mouse_passthrough_frame = SimpleNamespace(winfo_exists=lambda: True, winfo_width=lambda: width[0])
        app.mouse_passthrough_check.winfo_reqwidth = lambda: 220
        def control(request=0):
            return SimpleNamespace(winfo_reqwidth=lambda: request,
                                   grid_configure=lambda **options: options_seen.append(options))
        options_seen = []
        app._mouse_restore_button = control(140)
        app._mouse_status_label = control()
        app._mouse_help_label = control()
        app._layout_mouse_controls()
        self.assertEqual((options_seen[0]["row"],options_seen[0]["column"],options_seen[0]["sticky"]), (0,1,"e"))
        options_seen.clear();width[0] = 320
        app._layout_mouse_controls()
        self.assertEqual((options_seen[0]["row"],options_seen[0]["column"],options_seen[0]["sticky"]), (1,0,"w"))
        self.assertEqual([item["row"] for item in options_seen], [1,2,3])
        options_seen.clear()
        app._layout_mouse_controls()
        self.assertFalse(options_seen)

    def test_sync_callback_and_repeated_lifecycle_acknowledgements_save_only_once(self):
        app = self.app()
        app._start_mouse_passthrough()
        app._request_mouse_passthrough(True)
        callback = app.overlay.on_mouse_passthrough_changed
        callback(MousePassthroughResult(True, True, True, hwnd=41))
        callback(MousePassthroughResult(True, True, True, hwnd=41))
        app.root.run()
        self.assertEqual(app._test_preference_saves, [True])

    def test_no_tray_keeps_taskbar_restore_entry_and_startup_failure_keeps_main_visible(self):
        app = self.app(True)
        app._start_mouse_passthrough()
        app._hide_settings_on_startup()
        self.assertEqual(app.root.iconified, 1)
        self.assertEqual(app.root.withdrawn, 0)
        failed = self.app(True)
        failed.overlay.result = MousePassthroughResult(False, True, None, "failure", 41)
        failed._start_mouse_passthrough()
        failed._hide_settings_on_startup()
        self.assertEqual(failed.root.iconified, 0)
        self.assertEqual(failed.root.withdrawn, 0)
        self.assertEqual(failed.root.deiconify_calls, 1)

    def test_current_owner_main_thread_and_alive_are_required_for_native_callbacks(self):
        app = self.app()
        app._start_mouse_passthrough()
        result = MousePassthroughResult(True, True, True, hwnd=41)
        app._mouse_passthrough_changed(SimpleNamespace(), result)
        thread = threading.Thread(target=lambda: app._mouse_passthrough_changed(app.overlay, result))
        thread.start();thread.join()
        self.assertEqual(app.mouse_passthrough_var.get(), 0)
        app.root.alive = False
        app.overlay.on_mouse_passthrough_changed(result)
        self.assertEqual(app.mouse_passthrough_var.get(), 0)
        self.assertFalse(app._test_preference_saves)

    def test_exit_discards_late_native_and_tray_restore_without_touching_destroyed_tcl(self):
        app = self.app(True)
        app._start_mouse_passthrough()
        callback = app.overlay.on_mouse_passthrough_changed
        app.pipeline = None
        app.close()
        before = list(app.overlay.calls)
        callback(MousePassthroughResult(True, False, False, hwnd=41))
        app._restore_mouse_operation()
        app._post(app._show_settings)
        app._hide_settings()
        self.assertIsNone(app.overlay.on_mouse_passthrough_changed)
        self.assertEqual(app.overlay.calls, before)
        self.assertTrue(app.settings.overlay_mouse_passthrough)
        self.assertEqual(app.root.deiconify_calls, 0)
        self.assertTrue(app._events.empty())

    def test_actual_theme_draws_unknown_dash_as_distinct_from_off_and_checked(self):
        import theme
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                elements = {}
                root = SimpleNamespace(tk=SimpleNamespace(call=lambda *args: 1.333))
                style = SimpleNamespace(element_create=lambda name, *args, **kwargs: elements.__setitem__(name, args))
                with patch.object(theme.ImageTk, "PhotoImage", side_effect=lambda image, **kwargs: image):
                    prefix = theme._elements(root, style, theme.palette(mode))
                states = elements[prefix+"Check"]
                off = states[1]
                unknown = next(state[-1] for state in states[2:] if state[:-1] == ("alternate",))
                checked = next(state[-1] for state in states[2:] if state[:-1] == ("selected",))
                self.assertNotEqual(unknown.tobytes(), off.tobytes())
                self.assertNotEqual(unknown.tobytes(), checked.tobytes())
                self.assertEqual(unknown.getpixel((12,12))[3], 255)
                self.assertNotEqual(unknown.getpixel((12,12)), off.getpixel((12,12)))

    def test_real_save_reload_uses_committed_preference_even_when_checkbox_is_unknown(self):
        import tkinter as tk
        app = self.app(True)
        for name, value in (("source_var", "中文"), ("target_var", "中文"), ("audio_device_var", "synthetic"),
                            ("translation_var", "仅使用本地翻译")):
            setattr(app, name, tk.StringVar(master=app._test_interpreter, value=value))
        for name, value in (("auto_start_var", False), ("start_minimized_var", False), ("translation_enabled_var", True)):
            setattr(app, name, tk.BooleanVar(master=app._test_interpreter, value=value))
        app._runtime_details_preference = False
        app._theme_preference = "dark"
        app._save_settings = MethodType(Application._save_settings, app)
        with tempfile.TemporaryDirectory() as directory, patch("settings.application_data_dir", return_value=Path(directory)):
            app._mouse_passthrough_initialized = True
            app.overlay.result = MousePassthroughResult(False, True, None, "unknown", 41)
            app._request_mouse_passthrough(True)
            app._save_settings()
            self.assertTrue(Settings.load().overlay_mouse_passthrough)
            self.assertEqual(app.mouse_passthrough_var.get(), -1)
            app.overlay.result = None
            app._restore_mouse_operation()
            app.root.run()
            self.assertFalse(Settings.load().overlay_mouse_passthrough)
            self.assertEqual(json.loads((Path(directory)/"settings.json").read_text(encoding="utf-8"))["overlay_mouse_passthrough"], False)


if __name__ == "__main__":
    unittest.main()
