"""不启动Tk或音频的主窗文案、配置兼容与状态合并回归验证。"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import settings
from ui_guidance import (MODEL_FILTERS, compact_status, empty_transcript_hint, finite_nonnegative,
                         model_group, model_guidance, model_label, runtime_view, keep_readonly_value_visible)


class GuidanceTests(unittest.TestCase):
    def test_readonly_focus_restores_visible_value_without_changing_it(self):
        class Combo:
            value = "Beta：自动跟踪有声音的应用"
            selected, cursor, offset = True, 24, 1.0
            def instate(self, state): return state == ("readonly",)
            def selection_clear(self): self.selected = False
            def icursor(self, value): self.cursor = value
            def xview_moveto(self, value): self.offset = value
        combo = Combo()
        original = combo.value
        self.assertTrue(keep_readonly_value_visible(combo))
        self.assertEqual(combo.value, original)
        self.assertEqual((combo.selected, combo.cursor, combo.offset), (False, 0, 0))

    def test_editable_combo_keeps_its_selection_and_caret(self):
        self.assertFalse(keep_readonly_value_visible(SimpleNamespace(instate=lambda state: False)))

    def test_native_popdown_path_does_not_enter_python_widget_handlers(self):
        from main import Application
        event = SimpleNamespace(widget=".native.popdown.f.l")
        Application._reveal_focused_control(SimpleNamespace(), event)
        self.assertIsNone(Application._keyboard_scroll(SimpleNamespace(), event))

    def test_languages_are_explicit_and_no_speed_promise(self):
        self.assertIn("粤语", model_guidance("sensevoice-int8"))
        self.assertIn("需选定", model_guidance("zipformer-zh-en-int8"))
        self.assertIn("仅识别英语", model_guidance("distil-large-v3"))
        for key in ("tiny", "large-v3", "sensevoice-int8"):
            self.assertNotIn("最快", model_guidance(key))

    def test_model_groups_keep_stable_backend_keys(self):
        expected = {"sensevoice-int8":"CPU 五语", "zipformer-zh-en-int8":"CPU 中英实时",
                    "tiny.en":"英语专用", "distil-large-v3":"英语专用", "large-v3":"Whisper 多语"}
        for key, group in expected.items():
            self.assertEqual(model_group(key), group)
            self.assertIn(group, MODEL_FILTERS)
            self.assertTrue(model_label(key))
        self.assertEqual(model_label("future-key", "未来模型"), "未来模型")

    def test_input_quiet_is_not_speech_detection(self):
        self.assertIn("声音较轻", runtime_view("recognizing", rms=0)[0])
        self.assertEqual(runtime_view("waiting_audio", rms=0)[0], "等待声音")
        self.assertEqual(runtime_view("recognizing", rms=.03)[0], "正在识别")
        self.assertIn("有积压", runtime_view("recognizing", backlog=4)[0])

    def test_lifecycle_actions_are_specific(self):
        self.assertIn("下载", runtime_view("preparing")[1])
        self.assertIn("刷新音源", runtime_view("waiting_audio")[1])
        self.assertIn("释放", runtime_view("stopping")[0])
        self.assertIn("重新开始", runtime_view("error")[1])

    def test_non_finite_metrics_do_not_escape_to_ui(self):
        for value in (None, "0.1", True, float("inf"), float("nan")):
            self.assertIsNone(finite_nonnegative(value))
        self.assertEqual(finite_nonnegative(-1), 0)
        self.assertEqual(finite_nonnegative(.2), .2)

    def test_empty_transcript_next_action_matches_running_state(self):
        self.assertIn("开始字幕", empty_transcript_hint(False))
        self.assertIn("确认上方有输入", empty_transcript_hint(True))

    def test_summary_retains_full_text_when_short(self):
        self.assertEqual(compact_status("  模型\n加载中  "), "模型 加载中")
        self.assertEqual(len(compact_status("甲" * 300)), 96)


class SettingsCompatibilityTests(unittest.TestCase):
    def test_old_preferences_survive_with_new_details_default(self):
        # 用户现有模型、GPU、Beta来源和外观均为兼容性要求。
        original = {"recognition_model":"large-v3", "recognition_device":"NVIDIA GPU（float16）",
                    "loopback_device":"Beta 自动跟踪当前有声音的应用", "overlay_font_size":32,
                    "overlay_geometry":"777x222+333+444", "overlay_topmost":True,
                    "source_language":"中文", "translation_backend":"仅使用本地翻译"}
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, "application_data_dir", return_value=Path(folder)):
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            loaded = settings.Settings.load()
            for key, value in original.items():
                self.assertEqual(getattr(loaded, key), value)
            self.assertFalse(loaded.show_runtime_details)
            loaded.show_runtime_details = True
            loaded.save()
            restored = settings.Settings.load()
            self.assertTrue(restored.show_runtime_details)
            self.assertEqual(restored.recognition_model, "large-v3")

    def test_string_details_boolean_migrates(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, "application_data_dir", return_value=Path(folder)):
            path = Path(folder) / "settings.json"
            for raw, expected in (("false",False),("true",True)):
                path.write_text(json.dumps({"show_runtime_details":raw}),encoding="utf-8")
                self.assertIs(settings.Settings.load().show_runtime_details, expected)


class MainContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Application")
        methods = {"_show_metrics","_set_runtime_phase","_filter_models","_update_model_description","_on_model_selected", "_apply_overlay_focus", "_display_presented"}
        app.body = [node for node in app.body if isinstance(node, ast.FunctionDef) and node.name in methods]
        scope = dict(finite_nonnegative=finite_nonnegative, runtime_view=runtime_view,
                     MODEL_FILTERS=MODEL_FILTERS, model_group=model_group, model_guidance=model_guidance,
                     logging=logging, tk=SimpleNamespace(TclError=RuntimeError))
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future,app],type_ignores=[])), "<ui-contract>", "exec"),scope)
        cls.App = scope["Application"]

    def setUp(self):
        class Var:
            def __init__(self,value=None): self.value=value
            def get(self): return self.value
            def set(self,value): self.value=value
        class Control:
            def configure(self,**kw): self.__dict__.update(kw)
        self.Var,self.Control=Var,Control
        self.ui=self.App.__new__(self.App)
        self.ui._last_metrics={}
        self.ui._stopping=False
        self.ui._last_error=""
        self.ui._runtime_phase="preparing"
        for name in ("runtime_state_var","runtime_guidance_var","input_level_var","input_hint_var","runtime_device_var","runtime_speed_var"):
            setattr(self.ui,name,Var())

    def test_phase_only_update_preserves_actual_device_and_measurements(self):
        self.ui._show_metrics({"phase":"recognizing","input_rms":.02,"inference_seconds":.43,
                               "backlog_seconds":.42,"actual_device":"cuda","actual_compute_type":"float16"})
        old = self.ui.runtime_speed_var.get(),self.ui.runtime_device_var.get()
        self.ui._show_metrics({"phase":"waiting_audio"})
        self.assertEqual(old,(self.ui.runtime_speed_var.get(),self.ui.runtime_device_var.get()))
        self.assertEqual(self.ui.runtime_state_var.get(),"等待声音")
        self.assertNotIn("None",self.ui.runtime_speed_var.get())

    def test_late_metrics_cannot_replace_release_or_error_state(self):
        for phase in ("stopping","error"):
            self.ui._stopping=phase == "stopping"
            self.ui._last_error="真实错误" if phase == "error" else ""
            self.ui._set_runtime_phase(phase)
            previous=self.ui.runtime_state_var.get()
            self.ui._show_metrics({"phase":"recognizing"})
            self.assertEqual(previous,self.ui.runtime_state_var.get())

    def test_filter_never_changes_current_key(self):
        ui=self.ui
        ui.model_filter_var=self.Var("CPU 五语")
        ui.model_var=self.Var("large-v3")
        ui.model_display_var=self.Var()
        ui.model_hint_var=self.Var()
        ui.device_combo=self.Control()
        ui.model_combo=self.Control()
        ui._model_labels={"large-v3":"Whisper Large v3","sensevoice-int8":"CPU 五语"}
        ui._model_definitions={"large-v3":SimpleNamespace(backend="whisper")}
        ui._filter_models()
        self.assertEqual(ui.model_var.get(),"large-v3")
        self.assertEqual(ui.model_display_var.get(),"Whisper Large v3")
        self.assertEqual(ui.model_combo.values,["CPU 五语"])
        self.assertIn("全部模型",ui.model_hint_var.get())
        ui.model_filter_var.set("全部模型")
        ui._filter_models()
        self.assertEqual(len(ui.model_combo.values),2)
        self.assertEqual(ui.model_var.get(),"large-v3")

    def test_overlay_yields_only_while_configuration_has_focus(self):
        calls=[]
        root=SimpleNamespace(state=lambda:"normal")
        root.focus_get=lambda:SimpleNamespace(winfo_toplevel=lambda:root)
        self.ui.root=root
        self.ui.overlay=SimpleNamespace(set_configuration_active=calls.append)
        self.ui._closing=False
        self.ui._apply_overlay_focus()
        self.assertIs(calls[-1],True)
        root.focus_get=lambda:None
        self.ui._apply_overlay_focus()
        self.assertIs(calls[-1],False)
        root.focus_get=lambda:SimpleNamespace(winfo_toplevel=lambda:root)
        root.state=lambda:"withdrawn"
        self.ui._apply_overlay_focus()
        self.assertIs(calls[-1],False)

    def test_trace_records_only_the_update_actually_presented(self):
        ui=self.ui
        first=SimpleNamespace(sequence=1, block_id=101)
        second=SimpleNamespace(sequence=2, block_id=202, audio_end=4.2, measurements=lambda now:{"presented":now})
        ui._pending_presentations={101:first,202:second}
        ui._history=SimpleNamespace(blocks={101:SimpleNamespace(sequence=1),202:SimpleNamespace(sequence=2)})
        ui.overlay=SimpleNamespace(_follow_tail=True)
        calls=[]
        method_globals=self.App._display_presented.__globals__
        method_globals["get_runtime_trace"]=lambda:SimpleNamespace(record_presented=lambda *args:calls.append(args))
        ui._display_presented(123.4,{1})
        self.assertEqual(calls,[(second,123.4,"实时")])
        self.assertIn(101,ui._pending_presentations)
        self.assertNotIn(202,ui._pending_presentations)


if __name__ == "__main__":
    unittest.main()
