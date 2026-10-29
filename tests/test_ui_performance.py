"""Regression coverage for Tk input setup and cooperative event dispatch."""
import queue
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from main import Application


class EventDispatchTests(unittest.TestCase):
    def application(self):
        app = Application.__new__(Application)
        app._closing = app._exit_requested = False
        app._events = queue.SimpleQueue()
        self.scheduled = []
        app.root = SimpleNamespace(after=lambda delay, callback: self.scheduled.append((delay, callback)))
        self.flushed = []
        app._flush_display = lambda: self.flushed.append(True)
        return app

    def test_slow_batch_yields_without_dropping_or_reordering_messages(self):
        app = self.application()
        consumed = []
        for value in range(5):
            app._events.put((consumed.append, (value,)))
        # Two callbacks exhaust the budget. The remaining messages stay FIFO.
        with patch("main.time.perf_counter", side_effect=[0, .003, .007]):
            app._drain_events()
        self.assertEqual(consumed, [0, 1])
        self.assertEqual(self.scheduled[0][0], 1)
        self.assertEqual(len(self.flushed), 1)
        with patch("main.time.perf_counter", return_value=1):
            app._drain_events()
        self.assertEqual(consumed, list(range(5)))
        self.assertEqual(self.scheduled[-1][0], 10)

    def test_close_in_batch_rejects_late_work_and_does_not_flush(self):
        app = self.application()
        app._events.put((lambda: setattr(app, "_exit_requested", True), ()))
        app._events.put((lambda: self.fail("late callback"), ()))
        app._drain_events()
        self.assertFalse(self.scheduled)
        self.assertFalse(self.flushed)


@unittest.skipUnless(sys.platform == "win32", "Owned Windows Tk widget lifecycle")
class InputSetupTests(unittest.TestCase):
    def test_dynamic_widgets_and_remapped_page_keep_input_contract(self):
        import tkinter as tk
        from tkinter import ttk
        from theme import _notebook_input_mode, palette
        from choice_popup import ChoicePopupManager
        root = tk.Tk()
        errors = []
        root.report_callback_exception = lambda *error: errors.append(str(error[1]))
        try:
            _notebook_input_mode(root)
            manager = ChoicePopupManager(root, palette("dark"))
            notebook = ttk.Notebook(root)
            notebook.pack()
            frames = [ttk.Frame(notebook) for _ in range(2)]
            for frame in frames:
                notebook.add(frame, text="page")
            combo = ttk.Combobox(frames[1], values=("a", "b"), state="readonly")
            combo.pack()
            root.update()
            notebook.select(1)
            root.update()
            tags = combo.bindtags()
            self.assertIn(manager._tag, tags)
            self.assertTrue(any(tag.startswith("ShengmuTabInput") for tag in tags))
            notebook.focus_force()
            notebook.event_generate("<KeyPress-Right>")
            root.update()
            self.assertTrue(root._shengmu_tab_input["keyboard"])
            self.assertTrue(notebook.instate(["user1"]))
            for _ in range(3):
                notebook.select(0)
                root.update()
                notebook.select(1)
                root.update()
            self.assertEqual(combo.bindtags(), tags)
            notebook.event_generate("<ButtonPress-1>", x=-1, y=-1)
            self.assertFalse(root._shengmu_tab_input["keyboard"])
            self.assertTrue(notebook.instate(["!user1"]))
            selected = []
            combo.bind("<<ComboboxSelected>>", lambda _event: selected.append(combo.get()))
            combo.current(0)
            combo.focus_force()
            root.update()
            combo.event_generate("<KeyPress-F4>")
            root.update()
            self.assertIsNotNone(manager.popup)
            manager.popup.window.event_generate("<KeyPress-Escape>")
            root.update()
            self.assertIsNone(manager.popup)
            self.assertEqual(combo.get(), "a")
            combo.event_generate("<KeyPress-F4>")
            root.update()
            manager.popup.window.event_generate("<KeyPress-Down>")
            manager.popup.window.event_generate("<KeyPress-Return>")
            root.update()
            self.assertEqual(selected, ["b"])
            self.assertEqual(combo.get(), "b")
            manager.dismiss()
            self.assertFalse(errors)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
