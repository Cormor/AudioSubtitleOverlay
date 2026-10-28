import unittest
from application_controller import SessionController


class Pipeline:
    def __init__(self, options, **callbacks):
        self.callbacks = callbacks
        self.stops = 0
    def start(self): pass
    def stop(self): self.stops += 1
    def update_options(self, options): return options


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.queue, self.results = [], []
        self.controller = SessionController(lambda *item: self.queue.append(item), Pipeline)
        self.handlers = {"on_source": lambda *v: self.results.append(v), "on_stopped": lambda: self.results.append("stopped")}
    def drain(self):
        while self.queue:
            callback, *args = self.queue.pop(0)
            callback(*args)
    def test_stop_invalidates_already_queued_source_but_delivers_completion(self):
        p = self.controller.start({}, self.handlers)
        p.callbacks["on_source"]("old")
        self.assertTrue(self.controller.stop())
        self.assertFalse(self.controller.stop())
        p.callbacks["on_stopped"]()
        self.drain()
        self.assertEqual(self.results, ["stopped"])
        self.assertIsNone(self.controller.pipeline)
        self.assertEqual(p.stops, 1)
    def test_restart_rejects_old_completion_and_source(self):
        old = self.controller.start({}, self.handlers)
        self.controller.stop(); old.callbacks["on_stopped"](); self.drain()
        current = self.controller.start({}, self.handlers)
        old.callbacks["on_stopped"](); old.callbacks["on_source"]("late")
        current.callbacks["on_source"]("new"); self.drain()
        self.assertIs(self.controller.pipeline, current)
        self.assertEqual(self.results, ["stopped", ("new",)])
    def test_duplicate_start_and_closed_controller_are_rejected(self):
        self.controller.start({}, self.handlers)
        with self.assertRaises(RuntimeError): self.controller.start({}, self.handlers)
        self.assertTrue(self.controller.shutdown())
        self.assertFalse(self.controller.update_options({}))
        with self.assertRaises(RuntimeError): self.controller.start({}, self.handlers)
    def test_configuration_only_applies_to_active_session(self):
        self.assertFalse(self.controller.update_options("a"))
        self.controller.start({}, self.handlers)
        self.assertEqual(self.controller.update_options("b"), "b")
        self.controller.stop()
        self.assertFalse(self.controller.update_options("c"))
