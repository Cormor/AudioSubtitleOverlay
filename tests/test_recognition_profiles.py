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
        app.profile_var = variable("均衡 · small")
        app.profile_model_var = tk.BooleanVar(master=interpreter, value=True)
        app.model_var = variable("tiny")
        app.model_path_var = variable("custom-model")
        app.beam_size_var = variable("1")
        app.context_var = variable("启用")
        app.temperature_var = variable(_TEMPERATURE_LABELS["0"])
        app.current_profile_var = variable("")
        settings = Settings()
        app.recognition_vars = {name: variable(str(getattr(settings,"recognition_"+name)))
                                for name in RECOGNITION_NUMERIC_OPTIONS}
        app._save_settings = Mock()
        app._queue_live_configuration = Mock()
        app._set_status = Mock()
        return app

    def test_apply_can_keep_custom_model_while_updating_decoder_parameters(self):
        app = self.application()
        app.profile_model_var.set(False)
        app._apply_recognition_profile()
        self.assertEqual(app.model_path_var.get(),"custom-model")
        self.assertEqual(app.model_var.get(),"tiny")
        self.assertEqual(app.beam_size_var.get(),"3")
        self.assertEqual(app.context_var.get(),"关闭")
        self.assertIn("自定义",app.current_profile_var.get())
        app._queue_live_configuration.assert_called_once()

    def test_apply_full_combination_and_manual_change_does_not_reapply_it(self):
        app = self.application()
        for name in PROFILES:
            app.profile_var.set(name)
            app._apply_recognition_profile()
            self.assertEqual(app.model_var.get(),profile_values(name)["model"])
            self.assertEqual(app.model_path_var.get(),"")
            self.assertIn(name,app.current_profile_var.get())
        app.beam_size_var.set("1")
        app._update_current_profile()
        self.assertIn("自定义",app.current_profile_var.get())
        self.assertEqual(app.model_var.get(),"large-v3")
        self.assertEqual(app.beam_size_var.get(),"1")

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
