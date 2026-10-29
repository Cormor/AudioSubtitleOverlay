"""Numeric policy, persistence, decoder transport and real Main/Tcl edit events.

Tk widgets use object fixtures; final native input remains a separate acceptance.
"""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from recognition_parameters import (
    PARAMETERS, ParameterValueError, numeric_value, parse_numeric_text,
    number_text, temperature_values, temperature_text, parse_temperature_text,
)
from settings import Settings, SettingsLoadError
from transcription import WhisperRecognizer


CUSTOM = {
    "beam_size": 2, "window_seconds": 10, "update_interval": .3,
    "repetition_penalty": 1.135713, "no_repeat_ngram_size": 4,
    "no_speech_threshold": .53, "vad_silence_ms": 350,
}


class NumericContractTests(unittest.TestCase):
    def test_boundaries_and_nonrecommended_values_are_typed(self):
        for name, spec in PARAMETERS.items():
            with self.subTest(name=name):
                for value in (spec.minimum, spec.maximum, CUSTOM[name]):
                    parsed = parse_numeric_text(name, " " + number_text(value) + " ")
                    self.assertEqual(parsed, value)
                    self.assertIs(type(parsed), int if spec.integer else float)
                with self.assertRaises(ParameterValueError):
                    parse_numeric_text(name, number_text(spec.maximum + 1))
                with self.assertRaises(ParameterValueError):
                    parse_numeric_text(name, number_text(spec.minimum - 1))
        self.assertNotIn(2, PARAMETERS["beam_size"].recommended)

    def test_edit_lexemes_reject_incomplete_scientific_and_nonfinite_values(self):
        for name in PARAMETERS:
            for text in ("", " ", "-", "+", ".", "1.", "1e0", "NaN", "Inf", "-Infinity", "true"):
                with self.subTest(name=name, text=text), self.assertRaises(ParameterValueError):
                    parse_numeric_text(name, text)
        self.assertEqual(parse_numeric_text("no_speech_threshold", " +.5 "), .5)
        self.assertEqual(parse_numeric_text("beam_size", "+02"), 2)
        for text in ("2.0", "２", "2e0"):
            with self.subTest(text=text), self.assertRaises(ParameterValueError):
                parse_numeric_text("beam_size", text)

    def test_typed_json_integer_is_not_coerced_and_real_exponent_value_survives(self):
        for value in (True, False, 2.0, 2.1, "2", None):
            with self.subTest(value=value), self.assertRaises(ParameterValueError):
                numeric_value("beam_size", value)
        self.assertEqual(numeric_value("no_speech_threshold", 1e-7), 1e-7)
        for value in (float("nan"), float("inf"), float("-inf"), True, "0.3"):
            with self.subTest(value=value), self.assertRaises(ParameterValueError):
                numeric_value("no_speech_threshold", value)

    def test_temperature_sequence_preserves_order_precision_and_locale_separators(self):
        values = (.7135713, .150123456789, .45)
        self.assertEqual(temperature_values(".7135713， .150123456789、.45"), values)
        self.assertEqual(temperature_values(temperature_text(values)), values)
        self.assertEqual(temperature_text((1e-7, .45)), "0.0000001,0.45")

    def test_temperature_rejects_duplicates_invalid_items_and_excessive_work(self):
        for value in ("", "0,", ",0", "0,,.2", "0,0.0", ".2,.2", "1.1", "-.1", "0,NaN", "0,1e-1", "0,1.",
                      [0, False], [0, float("nan")], list(range(9)), "0" * 513):
            with self.subTest(value=value), self.assertRaises(ParameterValueError):
                temperature_values(value)
        self.assertEqual(temperature_values("0,.1,.2,.3,.4,.5,.6,.7"), (0, .1, .2, .3, .4, .5, .6, .7))
        with self.assertRaises(ParameterValueError):
            parse_numeric_text("no_speech_threshold", "0." + "0" * 64 + "1")
        with self.assertRaises(ParameterValueError):
            numeric_value("vad_silence_ms", 0)

    def test_every_accepted_temperature_edit_has_a_reparseable_canonical_form(self):
        for zeroes in (0, 1, 59, 60, 61, 62, 63):
            raw = "." + "0" * zeroes + "1"
            with self.subTest(zeroes=zeroes):
                if zeroes >= 62:
                    # 64 raw characters can expand to65 after adding leading0.
                    with self.assertRaises(ParameterValueError):
                        parse_temperature_text(raw)
                    continue
                values = parse_temperature_text(raw)
                canonical = temperature_text(values)
                self.assertEqual(parse_temperature_text(canonical), values)
                self.assertEqual(temperature_values(canonical), values)

    def test_committed_temperature_text_has_independent_bounded_codec_space(self):
        raw = "." + "0" * 62 + "1"
        self.assertEqual(len(raw), 64)
        values = temperature_values(raw)
        canonical = temperature_text(values)
        self.assertEqual(len(canonical), 65)
        self.assertEqual(temperature_values(canonical), values)
        with self.assertRaises(ParameterValueError):
            temperature_values("0" * 4097)
        # A nonzero decimal cannot silently underflow to the valid number0.
        with self.assertRaises(ParameterValueError):
            temperature_values("." + "0" * 400 + "1")


class ParameterPersistenceTests(unittest.TestCase):
    def test_tiny_typed_temperature_survives_save_reload_and_decoder_transport(self):
        values = (5e-324, 1e-300, .45)
        with tempfile.TemporaryDirectory() as folder, patch("settings.application_data_dir", return_value=Path(folder)):
            settings = Settings()
            settings.recognition_temperature_schedule = values
            settings.save()
            loaded = Settings.load()
            self.assertIsNone(Settings.last_load_warning)
            self.assertGreater(len(loaded.recognition_temperature_schedule), 512)
            self.assertEqual(temperature_values(loaded.recognition_temperature_schedule), values)
            received = {}
            def decode(_audio, **options):
                received.update(options)
                return iter([]), SimpleNamespace(language="zh")
            item = WhisperRecognizer("tiny", "CPU（int8）")
            item._model = SimpleNamespace(transcribe=decode)
            item.transcribe(np.zeros(1600, dtype=np.float32), "zh",
                            temperature_schedule=loaded.recognition_temperature_schedule)
            self.assertEqual(received["temperature"], values)

    def test_nonrecommended_values_and_scientific_json_number_reload_without_default_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch("settings.application_data_dir", return_value=Path(folder)):
            settings = Settings()
            for name, value in CUSTOM.items():
                setattr(settings, "recognition_" + name, value)
            settings.recognition_no_speech_threshold = 1e-7
            settings.recognition_temperature_schedule = ".15，.45"
            settings.overlay_mouse_passthrough = True
            settings.save()
            raw = (Path(folder) / "settings.json").read_text(encoding="utf-8")
            self.assertIn("e-", raw)
            loaded = Settings.load()
            for name, value in CUSTOM.items():
                if name != "no_speech_threshold":
                    self.assertEqual(getattr(loaded, "recognition_" + name), value)
            self.assertEqual(loaded.recognition_no_speech_threshold, 1e-7)
            self.assertEqual(loaded.recognition_temperature_schedule, "0.15,0.45")
            self.assertTrue(loaded.overlay_mouse_passthrough)
            loaded.save()
            self.assertEqual(Settings.load(), loaded)

    def test_invalid_numeric_configuration_is_preserved_and_cannot_overwrite_itself(self):
        cases = [
            {"recognition_beam_size": 2.5}, {"recognition_beam_size": True},
            {"recognition_beam_size": "2"}, {"recognition_update_interval": "nan"},
            {"recognition_no_speech_threshold": 2}, {"recognition_vad_silence_ms": 0},
            {"recognition_temperature_schedule": "0,0.0"},
        ]
        with tempfile.TemporaryDirectory() as folder, patch("settings.application_data_dir", return_value=Path(folder)):
            path = Path(folder) / "settings.json"
            for payload in cases:
                with self.subTest(payload=payload):
                    raw = json.dumps(payload)
                    path.write_text(raw, encoding="utf-8")
                    loaded = Settings.load()
                    self.assertTrue(Settings.last_load_warning)
                    with self.assertRaises(SettingsLoadError):
                        loaded.save()
                    self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_programmatic_invalid_values_cannot_replace_a_valid_saved_configuration(self):
        with tempfile.TemporaryDirectory() as folder, patch("settings.application_data_dir", return_value=Path(folder)):
            settings = Settings()
            settings.save()
            path = Path(folder) / "settings.json"
            before = path.read_bytes()
            settings.recognition_beam_size = 2.5
            with self.assertRaises(SettingsLoadError):
                settings.save()
            self.assertEqual(path.read_bytes(), before)


class ParameterDecoderTransportTests(unittest.TestCase):
    def recognizer(self):
        class Decoder:
            calls = 0
            options = None
            def transcribe(self, audio, **options):
                self.calls += 1
                self.options = options
                return iter([SimpleNamespace(text="合成测试", start=0., end=1., words=None)]), SimpleNamespace(language="zh")
        item = WhisperRecognizer("tiny", "CPU（int8）")
        item._model = Decoder()
        return item

    def test_custom_values_reach_both_real_whisper_call_paths_without_whitelist_or_rounding(self):
        for realtime in (False, True):
            with self.subTest(realtime=realtime):
                item = self.recognizer()
                result = item.transcribe(
                    np.zeros(1600, dtype=np.float32), "zh", realtime=realtime,
                    beam_size=2, temperature_schedule=".7135713,.150123456789,.45",
                    repetition_penalty=1.135713, no_repeat_ngram_size=4,
                    no_speech_threshold=.53, vad_silence_ms=350,
                )
                self.assertEqual(result.text, "合成测试")
                options = item._model.options
                self.assertEqual((options["beam_size"], options["best_of"]), (2, 2))
                self.assertEqual(options["temperature"], (.7135713, .150123456789, .45))
                self.assertEqual(options["repetition_penalty"], 1.135713)
                self.assertEqual(options["no_repeat_ngram_size"], 4)
                self.assertEqual(options["no_speech_threshold"], .53)
                if not realtime:
                    self.assertEqual(options["vad_parameters"]["min_silence_duration_ms"], 350)

    def test_illegal_values_fail_before_any_decoder_call(self):
        for options in ({"beam_size": 17}, {"beam_size": 2.0}, {"temperature_schedule": "0,0"},
                        {"repetition_penalty": float("nan")}, {"no_repeat_ngram_size": 1.5},
                        {"no_speech_threshold": 1.1}, {"vad_silence_ms": 0}):
            with self.subTest(options=options):
                item = self.recognizer()
                with self.assertRaises(ParameterValueError):
                    item.transcribe(np.zeros(1600, dtype=np.float32), "zh", **options)
                self.assertEqual(item._model.calls, 0)

class _EditorWidget:
    def __init__(self, path):
        self.path, self.exists, self.flags = path, True, set()
    def __str__(self): return self.path
    def winfo_exists(self): return self.exists
    def state(self, flags):
        for flag in flags:
            self.flags.discard(flag[1:]) if flag.startswith("!") else self.flags.add(flag)
        return tuple(self.flags)
    def instate(self, flags):
        return all((flag[1:] not in self.flags) if flag.startswith("!") else flag in self.flags for flag in flags)
    def configure(self, **options): self.options = options


class _EditorRoot:
    def __init__(self):
        self.jobs, self.serial, self.focused, self.alive = {}, 0, None, True
        self._shengmu_choices = SimpleNamespace(popup=None)
        self.deiconify_calls = 0
    def after(self, delay, callback):
        self.serial += 1
        self.jobs[self.serial] = (delay, callback)
        return self.serial
    def after_idle(self, callback): return self.after(0, callback)
    def after_cancel(self, token): self.jobs.pop(token, None)
    def winfo_exists(self): return self.alive
    def focus_get(self): return self.focused
    def withdraw(self): pass
    def destroy(self): self.alive = False
    def deiconify(self): self.deiconify_calls += 1
    def lift(self): pass
    def run(self):
        while self.jobs:
            token = min(self.jobs, key=lambda k: (self.jobs[k][0], k))
            self.jobs.pop(token)[1]()


class MainParameterEditingTests(unittest.TestCase):
    def application(self, **custom):
        import tkinter as tk
        import queue
        from main import Application, _TEMPERATURE_LABELS
        from pipeline import PipelineOptions
        app = Application.__new__(Application)
        app.root = _EditorRoot()
        app._test_interpreter = tk.Tcl()
        app.settings = Settings(recognition_model="tiny", recognition_beam_size=3, **custom)
        def var(value):
            return tk.StringVar(master=app._test_interpreter, value=value)
        app.model_var, app.device_var = var("tiny"), var("CPU（int8）")
        app.model_path_var, app.context_var = var(""), var("关闭")
        app.beam_size_var = var(str(app.settings.recognition_beam_size))
        value = app.settings.recognition_temperature_schedule
        app.temperature_var = var(_TEMPERATURE_LABELS.get(value, value))
        app.recognition_vars = {name: var(str(getattr(app.settings, "recognition_" + name)))
                                for name in PARAMETERS if name != "beam_size"}
        app.current_profile_var, app.profile_hint_var = var(""), var("")
        app._closing = app._exit_requested = app._stopping = app._applying_profile = False
        app._live_update_after_id = app._settings_save_after_id = None
        app._overlay_focus_after_id = app._drain_after_id = app._theme_poll_id = None
        app._tray = app._model_manager_window = None
        app._events = queue.SimpleQueue()
        app._test_published, app._test_saved = [], []
        app._set_status = lambda _: None
        app._set_runtime_phase = lambda _: None
        app._display = SimpleNamespace(awaiting_translation_context=False)
        app._initialize_parameter_drafts(master=app._test_interpreter)
        app._parameter_widgets = {name: _EditorWidget("." + name) for name in app._parameter_drafts}
        app._parameter_hint_labels = {name: _EditorWidget("." + name + "_hint") for name in app._parameter_drafts}
        def options():
            return app._effective_parameter_options(PipelineOptions(
                source_language="en", target_language="zh", model_size=app.model_var.get(),
                device_mode="CPU（int8）", model_path="", audio_device="synthetic",
                translation_backend="仅使用本地翻译",
                beam_size=app._parameter_value("beam_size"),
                temperature_schedule=temperature_text(app._parameter_value("temperature_schedule")),
                **{name: app._parameter_value(name) for name in PARAMETERS if name != "beam_size"}))
        app.pipeline = None
        app.pipeline = SimpleNamespace(options=options())
        def update(options):
            if options == app.pipeline.options: return False
            app.pipeline.options = options
            app._test_published.append(options)
            return True
        app._controller = SimpleNamespace(update_options=update, shutdown=lambda: True)
        app._pipeline_options_from_ui = options
        app._save_settings = lambda: app._test_saved.append(
            {name: app._parameter_value(name) for name in app._parameter_drafts})
        app.overlay = SimpleNamespace(close=lambda: None, persist_geometry=lambda: None)
        for variable in (app.beam_size_var, app.temperature_var, *app.recognition_vars.values()):
            variable.trace_add("write", app._queue_live_configuration)
            variable.trace_add("write", app._schedule_settings_save)
        return app

    def test_partial_edit_invalid_commit_and_cancel_never_publish_or_save(self):
        app = self.application()
        for raw in ("", "-", "2.", "NaN", "999"):
            app._parameter_inputs["beam_size"].set(raw)
            self.assertEqual(app.beam_size_var.get(), "3")
            self.assertEqual(app.root.jobs, {})
            app._commit_parameter("beam_size")
            self.assertTrue(app._parameter_drafts["beam_size"].error)
            self.assertIn("invalid", app._parameter_widgets["beam_size"].flags)
            self.assertEqual(app._parameter_inputs["beam_size"].get(), raw)
        self.assertEqual(app._test_published, [])
        self.assertEqual(app._test_saved, [])
        self.assertEqual(app._cancel_parameter("beam_size"), "break")
        self.assertEqual(app._parameter_inputs["beam_size"].get(), "3")
        self.assertFalse(app._parameter_drafts["beam_size"].error)

    def test_disabled_dirty_field_rejects_late_submit_cancel_focus_and_reenables_normally(self):
        for entry in ("Return", "ComboboxSelected", "FocusOut", "pending FocusOut", "Escape"):
            with self.subTest(entry=entry):
                app = self.application()
                field = app._parameter_widgets["beam_size"]
                app._parameter_inputs["beam_size"].set("2")
                if entry == "pending FocusOut":
                    app._parameter_focus_out("beam_size", owner=field)
                field.state(["disabled"])
                if entry in ("Return", "ComboboxSelected"):
                    app._commit_parameter("beam_size", owner=field)
                elif entry == "Escape":
                    app._cancel_parameter("beam_size", owner=field)
                else:
                    app._parameter_focus_out("beam_size", owner=field)
                app.root.run()
                self.assertEqual(app._parameter_value("beam_size"), 3)
                self.assertEqual(app._parameter_inputs["beam_size"].get(), "2")
                self.assertFalse(app._test_published)
                self.assertFalse(app._test_saved)
                field.state(["!disabled"])
                app._commit_parameter("beam_size", owner=field)
                app.root.run()
                self.assertEqual([item.beam_size for item in app._test_published], [2])
                self.assertEqual(len(app._test_saved), 1)

    def test_enter_then_focusout_submits_one_typed_value_and_saves_once(self):
        app = self.application()
        app._parameter_inputs["beam_size"].set("2")
        app._commit_parameter("beam_size")
        app._parameter_focus_out("beam_size")
        app.root.run()
        self.assertEqual([x.beam_size for x in app._test_published], [2])
        self.assertEqual(len(app._test_saved), 1)
        app._parameter_inputs["beam_size"].set("+02")
        app._commit_parameter("beam_size")
        app.root.run()
        self.assertEqual(len(app._test_published), 1)
        self.assertEqual(len(app._test_saved), 1)
        self.assertEqual(app._parameter_inputs["beam_size"].get(), "2")

    def test_popup_focus_cancel_then_field_escape_preserves_committed_until_cancel(self):
        app = self.application()
        field = app._parameter_widgets["beam_size"]
        app._parameter_inputs["beam_size"].set("2")
        popup_window = _EditorWidget(".popup")
        app.root._shengmu_choices.popup = SimpleNamespace(owner=field, window=popup_window)
        app.root.focused = _EditorWidget(".popup.canvas")
        app._parameter_focus_out("beam_size", owner=field)
        app.root.run()
        self.assertEqual(app._parameter_value("beam_size"), 3)
        self.assertEqual(app._parameter_inputs["beam_size"].get(), "2")
        app.root._shengmu_choices.popup = None
        app.root.focused = field
        app._parameter_focus_out("beam_size", owner=field)  # popup closed callback
        app.root.run()
        self.assertEqual(app._test_saved, [])
        app._cancel_parameter("beam_size", owner=field)
        self.assertEqual(app._parameter_inputs["beam_size"].get(), "3")

    def test_recommendation_replaces_draft_without_committing_intermediate_value(self):
        app = self.application()
        field = app._parameter_widgets["beam_size"]
        app._parameter_inputs["beam_size"].set("2")
        app.root._shengmu_choices.popup = SimpleNamespace(owner=field, window=_EditorWidget(".popup"))
        app.root.focused = _EditorWidget(".popup.canvas")
        app._parameter_focus_out("beam_size", owner=field)
        app.root.run()
        self.assertFalse(app._test_published)
        app.root._shengmu_choices.popup = None
        app.root.focused = field
        app._parameter_inputs["beam_size"].set("5")
        app._commit_parameter("beam_size", owner=field)
        app._parameter_focus_out("beam_size", owner=field)
        app.root.run()
        self.assertEqual([x.beam_size for x in app._test_published], [5])
        self.assertEqual(len(app._test_saved), 1)

    def test_true_popup_departure_commits_but_destroyed_or_replaced_owner_does_not(self):
        app = self.application()
        field = app._parameter_widgets["beam_size"]
        app._parameter_inputs["beam_size"].set("2")
        app.root.focused = _EditorWidget(".other")
        app._parameter_focus_out("beam_size", owner=field)
        app.root.run()
        self.assertEqual([x.beam_size for x in app._test_published], [2])
        app._parameter_inputs["beam_size"].set("4")
        app._parameter_focus_out("beam_size", owner=field)
        app._parameter_field_destroyed("beam_size", field)
        app.root.run()
        self.assertEqual([x.beam_size for x in app._test_published], [2])
        self.assertFalse(app._parameter_drafts["beam_size"].dirty)
        replacement = _EditorWidget(".replacement")
        app._parameter_widgets["beam_size"] = replacement
        app._parameter_inputs["beam_size"].set("5")
        app._commit_parameter("beam_size", owner=field)  # stale owner
        self.assertEqual(app._parameter_value("beam_size"), 2)

    def test_actual_choice_manager_selection_and_cancel_keep_one_commit_boundary(self):
        from choice_popup import ChoicePopupManager
        app = self.application()
        field = app._parameter_widgets["beam_size"]
        field.instate = lambda flags: False
        field.cget = lambda key: ("1", "3", "5")
        field.current = lambda index: app._parameter_inputs["beam_size"].set(("1", "3", "5")[index])
        field.event_generate = lambda *args, **kwargs: app.root.after(0, lambda: app._commit_parameter("beam_size", owner=field))
        field._shengmu_choice_closed = lambda: app._parameter_focus_out("beam_size", owner=field)
        manager = ChoicePopupManager.__new__(ChoicePopupManager)
        manager.root, manager.popup = app.root, None
        app.root._shengmu_choices = manager
        app._parameter_inputs["beam_size"].set("2")
        manager.popup = SimpleNamespace(owner=field, window=_EditorWidget(".popup"), values=("1","3","5"),
                                        close=lambda restore: setattr(app.root, "focused", field))
        app.root.focused = _EditorWidget(".popup.canvas")
        app._parameter_focus_out("beam_size", owner=field)
        app.root.run()
        manager.dismiss()
        app.root.run()
        self.assertEqual(app._parameter_inputs["beam_size"].get(), "2")
        self.assertEqual(app._parameter_value("beam_size"), 3)
        self.assertFalse(app._test_saved)
        popup = SimpleNamespace(owner=field, window=_EditorWidget(".popup2"), values=("1","3","5"),
                                close=lambda restore: setattr(app.root, "focused", field))
        manager.popup = popup
        app.root.focused = _EditorWidget(".popup2.canvas")
        manager.commit(popup, 2)
        app.root.run()
        self.assertEqual([item.beam_size for item in app._test_published], [5])
        self.assertEqual(len(app._test_saved), 1)

    def test_unchanged_long_committed_values_leave_without_edit_limit_errors(self):
        app = self.application(recognition_no_speech_threshold=1e-300,
                               recognition_temperature_schedule=temperature_text((5e-324, 1e-300, .45)))
        for name, limit in (("no_speech_threshold", 64), ("temperature_schedule", 512)):
            self.assertGreater(len(app._parameter_inputs[name].get()), limit)
            app._parameter_focus_out(name)
        app.root.run()
        self.assertFalse(app._test_published)
        self.assertFalse(app._test_saved)
        self.assertTrue(all(not draft.error for draft in app._parameter_drafts.values()))

    def test_unused_backend_preference_is_saved_without_reset_and_applies_when_model_changes(self):
        from dataclasses import replace
        app = self.application()
        app.model_var.set("sensevoice-int8")
        app.pipeline.options = replace(app.pipeline.options, model_size="sensevoice-int8")
        app._parameter_inputs["beam_size"].set("2")
        app._commit_parameter("beam_size")
        app.root.run()
        self.assertFalse(app._test_published)
        self.assertEqual(app._test_saved[-1]["beam_size"], 2)
        app.model_var.set("tiny")
        app._queue_live_configuration()
        app.root.run()
        self.assertEqual(app._test_published[-1].beam_size, 2)

    def test_streaming_source_and_translation_updates_do_not_touch_draft_or_focus(self):
        from tests.test_streaming_display import DisplayOverlayIntegrationTests, source, translated
        app = self.application()
        helper = DisplayOverlayIntegrationTests().app()
        for key, value in vars(helper).items():
            if not callable(value): setattr(app, key, value)
        app._apply_translation_visibility = lambda _: None
        field = app._parameter_widgets["beam_size"]
        app.root.focused = field
        app._parameter_inputs["beam_size"].set("2")
        for sequence, text in ((1, "hello"), (2, "hello world")):
            update = source(sequence, text)
            app._show_timed_source(update)
            app._show_translation_update(translated(update))
            app._flush_display()
        self.assertEqual(app.source_display_var.value, "hello world")
        self.assertEqual(app._parameter_inputs["beam_size"].get(), "2")
        self.assertIs(app.root.focus_get(), field)
        self.assertEqual(app._parameter_value("beam_size"), 3)
        self.assertFalse(app._test_saved)

    def test_close_discards_draft_and_rejects_late_same_batch_and_focus_callbacks(self):
        app = self.application()
        app.pipeline = None
        app._parameter_inputs["beam_size"].set("2")
        app._parameter_focus_out("beam_size")
        app._flush_display = lambda: self.fail("flush after close")
        app._events.put((app.close, ()))
        app._events.put((app._show_settings, ()))
        app._events.put((lambda: self.fail("late action"), ()))
        app._drain_events()
        self.assertTrue(app._exit_requested)
        self.assertEqual(app._test_saved[-1]["beam_size"], 3)
        self.assertEqual(len(app._test_saved), 1)
        self.assertEqual(app.root.deiconify_calls, 0)
        app._parameter_focus_out("beam_size")
        self.assertFalse(app.root.jobs)


class InvalidParameterAppearanceTests(unittest.TestCase):
    def test_production_entry_paints_error_and_focus_together_and_disabled_overrides_error(self):
        import theme
        from PIL import ImageColor
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                palette = theme.palette(mode)
                root = SimpleNamespace(tk=SimpleNamespace(call=lambda *args: 1.333))
                elements = {}
                style = SimpleNamespace(element_create=lambda name, *args, **kwargs: elements.__setitem__(name, args))
                with patch.object(theme.ImageTk, "PhotoImage", side_effect=lambda image, **kwargs: image):
                    prefix = theme._elements(root, style, palette)
                states = elements[prefix + "Entry"]
                image = next(item[-1] for item in states[2:] if isinstance(item, tuple) and item[:-1] == ("focus", "invalid"))
                # Theme assets may be precomposited RGB; compare the displayed
                # color contract with an explicit opaque alpha channel.
                colors = set(image.convert("RGBA").getdata())
                for token in ("danger", "focus"):
                    rgb = ImageColor.getrgb(palette[token])
                    background = ImageColor.getrgb(palette["surface"])
                    # The original assertion accepted edge coverage >=230/255.
                    # Compare those same displayed colors after precompositing,
                    # while retaining the original 8-channel tolerance.
                    expected = [tuple(round((rgb[i]*alpha + background[i]*(255-alpha))/255)
                                      for i in range(3)) for alpha in range(230, 256)]
                    distance = min(max(abs(color[i]-target[i]) for i in range(3))
                                   for color in colors if color[3] >= 230 for target in expected)
                    self.assertLessEqual(distance, 8, token)
                disabled = states[2][-1]
                self.assertNotIn((*ImageColor.getrgb(palette["danger"]), 255), set(disabled.convert("RGBA").getdata()))


class _WrappingLabel:
    def __init__(self, width, wrap=780, manager="grid", slot_width=None, padx=(0,)):
        self.width, self.wrap, self.manager, self.configures = width, wrap, manager, []
        self.slot_width = width if slot_width is None else slot_width
        self.padx = padx
        self.tk = SimpleNamespace(splitlist=lambda text: tuple(str(text).split()))
    def cget(self, name): return self.wrap
    def winfo_width(self): return self.width
    def winfo_manager(self): return self.manager
    def grid_info(self):
        return dict(column=0,row=self.row,columnspan=1,rowspan=1,padx=self.padx)
    def winfo_pixels(self, value): return int(value)
    def configure(self, **options):
        self.configures.append(options)
        self.wrap = options["wraplength"]


class _WrappingFrame:
    def __init__(self, labels):
        self.labels, self.exists = labels, True
        for row,label in enumerate(labels): label.row = row
    def winfo_exists(self): return self.exists
    def winfo_children(self): return self.labels
    def winfo_width(self): return 940
    def grid_bbox(self, column, row, last_column, last_row):
        return (0,0,self.labels[row].slot_width,40)


class MainLabelWrappingTests(unittest.TestCase):
    def test_reflow_uses_allocated_width_after_layout_and_preserves_editing_state(self):
        import main
        app = MainParameterEditingTests().application()
        app._parameter_inputs["beam_size"].set("2")
        wide = _WrappingLabel(742)
        narrow = _WrappingLabel(140)
        plain = _WrappingLabel(500, wrap=0)
        packed = _WrappingLabel(500, manager="pack")
        nonfill = _WrappingLabel(500, slot_width=644, padx=(4,6))
        frame = _WrappingFrame([wide,narrow,plain,packed,nonfill])
        with patch.object(main.ttk,"Label",_WrappingLabel), patch.object(main.ttk,"LabelFrame",_WrappingFrame):
            app._resize_card_labels(SimpleNamespace(widget=frame,width=940))
            app._resize_card_labels(SimpleNamespace(widget=frame,width=940))
            self.assertEqual(len(app.root.jobs),1)
            narrow.width = narrow.slot_width = 120  # allocator settles after Configure
            app.root.run()
            self.assertLessEqual(wide.wrap,742)
            self.assertLessEqual(narrow.wrap,120)
            self.assertGreater(narrow.wrap,0)
            self.assertEqual(plain.wrap,0)
            self.assertGreater(packed.wrap,packed.width)  # available frame slot, not intrinsic width
            packed.width = 400
            self.assertGreater(nonfill.wrap,nonfill.width)
            self.assertLessEqual(nonfill.wrap,634)  # allocated slot minus external left/right padding
            nonfill.width = 400
            self.assertEqual(len(wide.configures),1)
            app._resize_card_labels(SimpleNamespace(widget=frame,width=940))
            app.root.run()
            self.assertEqual(len(wide.configures),1)  # no same-width loop
            self.assertEqual(len(packed.configures),1)  # repeated Configure cannot shrink packed text
            self.assertEqual(len(nonfill.configures),1)  # sticky=w grid uses cell allocation, not requested width
        self.assertEqual(app._parameter_inputs["beam_size"].get(),"2")
        self.assertEqual(app._parameter_value("beam_size"),3)
        self.assertFalse(app._test_published)
        self.assertFalse(app._test_saved)

    def test_destroyed_frame_and_close_reject_queued_and_late_configure_work(self):
        import main
        for destroyed in (True,False):
            with self.subTest(destroyed=destroyed):
                app = MainParameterEditingTests().application()
                label = _WrappingLabel(634)
                frame = _WrappingFrame([label])
                with patch.object(main.ttk,"Label",_WrappingLabel), patch.object(main.ttk,"LabelFrame",_WrappingFrame):
                    app._resize_card_labels(SimpleNamespace(widget=frame,width=940))
                    callback = next(iter(app.root.jobs.values()))[1]
                    if destroyed:
                        frame.exists = False
                        app.root.run()
                    else:
                        app.pipeline = None
                        app.close()
                        self.assertFalse(app.root.jobs)
                        callback()  # a stale callback already retrieved by Tcl
                        app._resize_card_labels(SimpleNamespace(widget=frame,width=940))
                    self.assertFalse(label.configures)
                    self.assertFalse(app._label_wrap_jobs)


if __name__ == "__main__":
    unittest.main()
