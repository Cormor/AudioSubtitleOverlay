"""Display-free acceptance checks for popup geometry and readable row layout."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tkinter as tk

import choice_popup
from choice_popup import (Rect, layout_rows, navigation_index, place_popup,
                          row_at, visible_offset, wrap_text, ChoicePopup,
                          ChoicePopupManager)


class ChoiceLayoutTests(unittest.TestCase):
    def test_negative_monitor_origin_and_taskbar_stay_inside_work_area(self):
        work = Rect(-1920, -200, 1920, 1040)
        result = place_popup(Rect(-80, 760, 70, 36), 420, 300, work)
        self.assertTrue(result.above)
        self.assertEqual(result.y + result.height, 756)
        self.assertGreaterEqual(result.x, -1920)
        self.assertLessEqual(result.x + result.width, 0)
        self.assertLessEqual(result.y + result.height, 840)

    def test_narrow_work_area_limits_width_and_uses_more_spacious_side(self):
        work = Rect(20, 30, 230, 200)
        result = place_popup(Rect(190, 150, 90, 40), 560, 310, work)
        self.assertEqual((result.x, result.width), (20, 230))
        self.assertTrue(result.above)
        self.assertEqual((result.y, result.height), (30, 116))

    def test_bottom_placement_has_four_pixel_gap(self):
        result = place_popup(Rect(40, 60, 200, 38), 200, 120, Rect(0, 0, 800, 600))
        self.assertEqual(result, type(result)(40, 102, 200, 120, False))

    def test_offscreen_anchor_still_produces_visible_nonempty_popup(self):
        result = place_popup(Rect(1100, 1200, 300, 38), 400, 300, Rect(0, 0, 300, 200))
        self.assertGreater(result.height, 0)
        self.assertGreaterEqual(result.y, 0)
        self.assertLessEqual(result.x + result.width, 300)
        self.assertLessEqual(result.y + result.height, 200)

    def test_cjk_and_unbroken_names_keep_every_character(self):
        text = "耳机设备" * 18 + "USB_Audio_1234567890" * 8
        lines = wrap_text(text, 60, lambda value: len(value) * 10)
        self.assertEqual("".join(lines), text)
        self.assertTrue(all(len(line) <= 6 for line in lines))

    def test_explicit_newlines_and_empty_choice_are_preserved(self):
        self.assertEqual(wrap_text("第一行\n\n最后", 100, len), ("第一行", "", "最后"))
        self.assertEqual(wrap_text("", 100, len), ("",))

    def test_large_font_and_wrapping_increase_height_without_reducing_text(self):
        values = ("短", "带空格的 long device " * 9, "末尾")
        small = layout_rows(values, 140, 18, lambda value: len(value) * 9)
        large = layout_rows(values, 140, 34, lambda value: len(value) * 17)
        self.assertGreater(large[1].bottom - large[1].top, small[1].bottom - small[1].top)
        self.assertGreaterEqual(large[0].bottom, 50)
        self.assertEqual("".join(large[1].lines), values[1])
        self.assertEqual(large[2].top, large[1].bottom)

    def test_variable_rows_have_unambiguous_hit_boundaries(self):
        rows = layout_rows(("first", "x" * 80, "last"), 50, 20, lambda value: len(value) * 10)
        self.assertIsNone(row_at(rows, -0.1))
        self.assertEqual(row_at(rows, rows[0].bottom - 0.1), 0)
        self.assertEqual(row_at(rows, rows[0].bottom), 1)
        self.assertEqual(row_at(rows, rows[-1].bottom - 0.1), 2)
        self.assertIsNone(row_at(rows, rows[-1].bottom))

    def test_first_and_last_items_reachable_with_tall_middle_row(self):
        rows = layout_rows(("first", "x" * 80, "last"), 50, 20, lambda value: len(value) * 10)
        self.assertEqual(navigation_index(0, 3, "Next", rows, 72), 1)
        self.assertEqual(navigation_index(1, 3, "Next", rows, 72), 2)
        self.assertEqual(navigation_index(2, 3, "End", rows, 72), 2)
        self.assertEqual(navigation_index(2, 3, "Home", rows, 72), 0)
        self.assertEqual(navigation_index(0, 3, "Up"), 0)
        self.assertEqual(navigation_index(2, 3, "Down"), 2)
        self.assertEqual(navigation_index(5, 0, "Home"), -1)

    def test_scrolling_can_expose_last_line_and_return_to_first_item(self):
        rows = layout_rows(("first", "x" * 80, "last"), 50, 20, lambda value: len(value) * 10)
        total = rows[-1].bottom
        offset = visible_offset(rows[-1], 0, 72, total)
        self.assertEqual(offset + 72, total)
        self.assertEqual(visible_offset(rows[0], offset, 72, total), 0)
        self.assertEqual(visible_offset(rows[1], offset, 72, total), rows[1].top)


class ChoiceLifetimeTests(unittest.TestCase):
    def setUp(self):
        self._interpreters = []

    def new_interpreter(self):
        interpreter = tk.Tcl()
        self._interpreters.append(interpreter)
        return interpreter

    def tearDown(self):
        # Mock graphs can retain an interpreter until an unrelated worker runs
        # cyclic GC. Reclaim those graphs on Tcl's creating thread instead.
        import gc
        for interpreter in self._interpreters:
            if interpreter.tk.call("info", "commands", "destroy_owner"):
                interpreter.tk.deletecommand("destroy_owner")
        gc.collect()
        self._interpreters.clear()
        gc.collect()

    """Real Tcl scripts plus simulated widgets; these do not create Tk windows."""

    def make_manager(self):
        manager = ChoicePopupManager.__new__(ChoicePopupManager)
        manager.root = Mock()
        manager.palette = {}
        manager.popup = None
        manager._posting = False
        manager._readonly = lambda combo: True
        return manager

    def make_combo(self, interpreter, script):
        combo = Mock()
        combo.tk = interpreter
        combo.cget.side_effect = lambda key: (
            script if key == "postcommand" else interpreter.getvar("choices"))
        combo.instate.return_value = False
        combo.winfo_exists.return_value = True
        combo.winfo_ismapped.return_value = True
        return combo

    def test_postcommand_executes_complete_global_tcl_script_before_values_read(self):
        interpreter = self.new_interpreter()
        interpreter.setvar("choices", ("old",))
        combo = self.make_combo(interpreter, "set ::ready 1; set ::choices [list {new first} 第二项]")
        manager = self.make_manager()
        with patch.object(choice_popup, "ChoicePopup") as popup_type:
            popup = manager.post(combo, keyboard=True)
        self.assertEqual(interpreter.getvar("ready"), "1")
        popup_type.assert_called_once_with(manager, combo, ("new first", "第二项"), True)
        self.assertIs(manager.popup, popup)
        popup.show.assert_called_once()
        manager.root.report_callback_exception.assert_not_called()

    def test_postcommand_can_rebuild_owner_without_creating_stale_popup(self):
        interpreter = self.new_interpreter()
        interpreter.setvar("choices", ("old",))
        combo = self.make_combo(interpreter, "destroy_owner; set ::choices [list fresh]")
        interpreter.createcommand("destroy_owner", lambda: setattr(combo.winfo_exists, "return_value", False))
        manager = self.make_manager()
        with patch.object(choice_popup, "ChoicePopup") as popup_type:
            self.assertIsNone(manager.post(combo))
        popup_type.assert_not_called()
        self.assertIsNone(manager.popup)
        self.assertFalse(manager._posting)
        manager.root.report_callback_exception.assert_not_called()

    def test_partial_construction_reclaims_window_bindings_and_idle_work(self):
        manager, owner, window, bound_widget = Mock(), Mock(), Mock(), Mock()
        owner.current.return_value = 0
        owner.grab_current.return_value = None
        manager.root.grab_current.return_value = window

        def broken_build(popup):
            popup.window = window
            popup._bindings.append((bound_widget, "<Configure>", "binding-token"))
            popup._after_ids.add("idle-token")
            raise tk.TclError("window build failed")

        with patch.object(ChoicePopup, "_build", broken_build):
            with self.assertRaisesRegex(tk.TclError, "window build failed"):
                ChoicePopup(manager, owner, ("first",), False)
        window.after_cancel.assert_called_once_with("idle-token")
        bound_widget.unbind.assert_called_once_with("<Configure>", "binding-token")
        window.grab_release.assert_called_once()
        window.destroy.assert_called_once()
        owner.focus_set.assert_not_called()

    def test_commit_releases_popup_before_value_change_and_emits_one_queued_event(self):
        manager = self.make_manager()
        order = []
        owner = Mock()
        owner.instate.return_value = False
        owner.cget.return_value = ("first", "second")
        owner.current.side_effect = lambda index: order.append(("value", index))
        owner.event_generate.side_effect = lambda *args, **kwargs: order.append(("event", args, kwargs))
        popup = SimpleNamespace(owner=owner, values=("first", "second"),
                                close=lambda restore: order.append(("closed", restore)))
        manager.popup = popup
        manager.commit(popup, 1)
        manager.commit(popup, 1)
        self.assertEqual(order, [("closed", True), ("value", 1),
                                 ("event", ("<<ComboboxSelected>>",), {"when": "tail"})])
        self.assertIsNone(manager.popup)

    def test_device_refresh_does_not_commit_new_value_at_old_index(self):
        manager = self.make_manager()
        owner = Mock()
        owner.instate.return_value = False
        owner.cget.return_value = ("first", "different device")
        popup = SimpleNamespace(owner=owner, values=("first", "second"), close=Mock())
        manager.popup = popup
        manager.commit(popup, 1)
        owner.current.assert_not_called()
        owner.event_generate.assert_not_called()
        self.assertIsNone(manager.popup)

    def test_closing_does_not_steal_grab_from_newer_modal(self):
        popup = ChoicePopup.__new__(ChoicePopup)
        popup.closed = False
        popup._after_ids, popup._bindings = set(), []
        popup.window, popup.owner = Mock(), Mock()
        popup.manager = Mock()
        popup._previous_grab, popup._previous_grab_status = Mock(), "local"
        popup.manager.root.grab_current.return_value = Mock()
        popup.close(restore_focus=True)
        popup.window.grab_release.assert_not_called()
        popup._previous_grab.grab_set.assert_not_called()
        popup.owner.focus_set.assert_not_called()
        popup.window.destroy.assert_called_once()

    def test_cancel_restores_native_owner_focus_only_while_popup_has_input(self):
        popup = ChoicePopup.__new__(ChoicePopup)
        popup.closed = False
        popup._after_ids, popup._bindings = set(), []
        popup.window, popup.owner, popup.manager = Mock(), Mock(), Mock()
        popup._previous_grab, popup._previous_grab_status = None, None
        popup.manager.root.grab_current.return_value = popup.window
        popup.manager.root.focus_displayof.return_value = popup.window
        popup.close(restore_focus=True)
        popup.owner.focus_force.assert_called_once()

    def test_cancel_after_switching_application_does_not_restore_owner_focus(self):
        popup = ChoicePopup.__new__(ChoicePopup)
        popup.closed = False
        popup._after_ids, popup._bindings = set(), []
        popup.window, popup.owner, popup.manager = Mock(), Mock(), Mock()
        popup._previous_grab, popup._previous_grab_status = None, None
        popup.manager.root.grab_current.return_value = popup.window
        popup.manager.root.focus_displayof.return_value = None
        popup.close(restore_focus=True)
        popup.window.grab_release.assert_called_once()
        popup.owner.focus_force.assert_not_called()

    def test_unknown_native_focus_is_cleaned_without_restoring_focus(self):
        popup = ChoicePopup.__new__(ChoicePopup)
        popup.closed = False
        popup._after_ids, popup._bindings = set(), []
        popup.window, popup.owner, popup.manager = Mock(), Mock(), Mock()
        popup._previous_grab, popup._previous_grab_status = None, None
        popup.manager.root.focus_displayof.side_effect = KeyError(".native.dialog")
        popup.manager.root.grab_current.return_value = popup.window
        popup.close(restore_focus=True)
        popup.window.grab_release.assert_called_once()
        popup.window.destroy.assert_called_once()
        popup.owner.focus_force.assert_not_called()

    def test_unknown_native_grab_does_not_skip_window_destruction(self):
        popup = ChoicePopup.__new__(ChoicePopup)
        popup.closed = False
        popup._after_ids, popup._bindings = set(), []
        popup.window, popup.owner, popup.manager = Mock(), Mock(), Mock()
        popup._previous_grab, popup._previous_grab_status = Mock(), "local"
        popup.manager.root.grab_current.side_effect = KeyError(".native.dialog")
        popup.close(restore_focus=True)
        popup.window.grab_release.assert_called_once()
        popup.window.destroy.assert_called_once()
        popup._previous_grab.grab_set.assert_not_called()
        popup.owner.focus_force.assert_not_called()

    def test_external_popup_destruction_restores_valid_previous_modal(self):
        popup = ChoicePopup.__new__(ChoicePopup)
        popup.closed = False
        popup._after_ids, popup._bindings = set(), []
        popup.window, popup.owner, popup.manager = Mock(), Mock(), Mock()
        popup._previous_grab, popup._previous_grab_status = Mock(), "local"
        popup.manager.root.grab_current.return_value = None
        popup.window.destroy.side_effect = tk.TclError("already destroyed")
        popup.close(restore_focus=False)
        popup._previous_grab.grab_set.assert_called_once()


if __name__ == "__main__":
    unittest.main()
