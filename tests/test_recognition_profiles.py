import json
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from main import Application, _TEMPERATURE_LABELS
from recognition_profiles import PROFILES, profile_values
from settings import Settings, RECOGNITION_NUMERIC_OPTIONS


class RecognitionProfileTests(unittest.TestCase):
    def application(self):
        interpreter = tk.Tcl()
        app = Application.__new__(Application)
        app._test_interpreter = interpreter
        def variable(value):
            return tk.StringVar(master=interpreter, value=value)
        app.model_var = variable("tiny")
        app.model_path_var = variable("custom-model")
        app.beam_size_var = variable("1")
        app.context_var = variable("启用")
        app.temperature_var = variable(_TEMPERATURE_LABELS["0"])
        app.current_profile_var = variable("")
        app.profile_hint_var = variable("")
        app.pipeline = None
        app._closing = False
        app._live_update_after_id = None
        app._test_scheduled = {}
        def after(_delay, callback):
            handle = len(app._test_scheduled) + 1
            app._test_scheduled[handle] = callback
            return handle
        app.root = SimpleNamespace(after=after, after_cancel=lambda handle: app._test_scheduled.pop(handle, None))
        settings = Settings()
        app.recognition_vars = {name: variable(str(getattr(settings,"recognition_"+name)))
                                for name in RECOGNITION_NUMERIC_OPTIONS}
        app._save_settings = Mock()
        app._set_status = Mock()
        return app

    def test_each_preset_preserves_model_custom_path_and_device(self):
        app = self.application()
        app.device_var = tk.StringVar(master=app._test_interpreter, value="CPU（int8）")
        for name in PROFILES:
            app._apply_recognition_profile(name)
            self.assertEqual(app.model_path_var.get(), "custom-model")
            self.assertEqual(app.model_var.get(), "tiny")
            self.assertEqual(app.device_var.get(), "CPU（int8）")
            self.assertIn(name, app.current_profile_var.get())
            values = profile_values(name)
            for key, variable in app.recognition_vars.items():
                self.assertEqual(float(variable.get()), values[key])
            self.assertNotIn("model", values)

    def test_changing_model_preserves_preset_and_manual_tuning_stays_custom(self):
        app = self.application()
        app._apply_recognition_profile("均衡")
        before = {key: variable.get() for key, variable in app.recognition_vars.items()}
        app.model_var.set("large-v3")
        app.model_path_var.set("different-model")
        app._update_current_profile()
        self.assertIn("均衡", app.current_profile_var.get())
        self.assertEqual(before, {key: variable.get() for key, variable in app.recognition_vars.items()})
        app.beam_size_var.set("5")
        app._update_current_profile()
        self.assertIn("自定义", app.current_profile_var.get())
        self.assertEqual(app.model_var.get(), "large-v3")
        self.assertEqual(app.beam_size_var.get(), "5")

    def test_preset_traces_publish_only_one_complete_configuration(self):
        app = self.application()
        published = []
        app.pipeline = SimpleNamespace(update_options=lambda options: published.append(options) or True)
        app._pipeline_options_from_ui = lambda: {
            "beam": app.beam_size_var.get(), "context": app.context_var.get(),
            **{key: variable.get() for key, variable in app.recognition_vars.items()}}
        for variable in (app.beam_size_var, app.context_var, app.temperature_var, *app.recognition_vars.values()):
            variable.trace_add("write", app._queue_live_configuration)
            variable.trace_add("write", app._schedule_settings_save)
        app._apply_recognition_profile("长上下文")
        self.assertEqual(len(app._test_scheduled), 1)
        self.assertFalse(published)
        app._test_scheduled.popitem()[1]()
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["beam"], "5")
        self.assertEqual(published[0]["context"], "启用")
        self.assertEqual(published[0]["window_seconds"], "16")
        app._save_settings.assert_called_once()

    def test_numeric_options_roundtrip_and_invalid_values_fall_back(self):
        with tempfile.TemporaryDirectory() as temporary, patch("settings.application_data_dir", return_value=Path(temporary)):
            settings=Settings()
            for name, choices in RECOGNITION_NUMERIC_OPTIONS.items():
                setattr(settings,"recognition_"+name,choices[-1])
            settings.save()
            self.assertEqual(Settings.load(),settings)
            path=Path(temporary)/"settings.json"
            path.write_text(json.dumps({"recognition_"+name:"nan" for name in RECOGNITION_NUMERIC_OPTIONS}),encoding="utf-8")
            loaded=Settings.load()
            for name in RECOGNITION_NUMERIC_OPTIONS:
                self.assertEqual(getattr(loaded,"recognition_"+name),getattr(Settings(),"recognition_"+name))
