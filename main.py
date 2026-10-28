"""Windows 音频识别翻译悬浮窗应用入口。"""

from __future__ import annotations

import argparse
from dataclasses import replace
from dataclasses import asdict
import logging
from log_retention import 保留日志处理器
import queue
import multiprocessing
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from brand import APP_NAME, APP_DESCRIPTION, APP_ID, asset_path, app_version, build_info
from tray_control import TrayController
from application_views import build_workspace
from theme import THEME_LABELS, apply_theme, system_theme, theme_window
from application_controller import SessionController
from diagnostics import export_diagnostics
from recognition_profiles import PROFILES, profile_values
from audio_capture import SystemAudioCapture
from process_audio_capture import PROCESS_AUDIO_DEVICE
from display_module import DisplayEvent, DisplayModule
from subtitle_events import SourceUpdate, TranslationContext, TranslationUpdate
from languages import (
    language_code,
    language_name,
    source_language_names,
    target_language_names,
)
from model_manager import MODEL_DEFINITIONS, ModelManagerWindow, validate_model_language
from runtime_trace import get_runtime_trace, add_trace_arguments
from ui_guidance import (DEVICE_FILTERS, DEVICE_MODE_LABELS, MODEL_FILTERS,
                         device_mode_label, device_mode_value, device_mode_guidance,
                         compact_status, empty_transcript_hint, finite_nonnegative,
                         model_group, model_guidance, model_label, runtime_view)
from overlay import OverlayWindow
from pipeline import LivePipeline, PipelineOptions
from recognition_parameters import (PARAMETERS, ParameterDraft, numeric_value, number_text,
    temperature_values, temperature_text, parameter_is_consumed, RECOGNITION_TEMPERATURE_SCHEDULES)
from settings import (
    RECOGNITION_BEAM_SIZES, RECOGNITION_NUMERIC_OPTIONS,
    Settings,
    application_data_dir,
)


_TEMPERATURE_LABELS = {
    "0": "仅使用 0",
    "0,0.2,0.4": "低温回退（0、0.2、0.4）",
}
_TEMPERATURE_VALUES_BY_LABEL = {
    label: value for value, label in _TEMPERATURE_LABELS.items()
}


class Application:
    """应用主窗口和后台流水线控制器。"""

    def __init__(self, root: tk.Tk, *, autostart=None, tray=True) -> None:
        self.root = root
        self.settings = Settings.load()
        self.pipeline: LivePipeline | None = None
        self._session = 0
        self._pending_presentations: dict[int, SourceUpdate] = {}
        self._trace_versions: dict[int, SourceUpdate] = {}
        self._events = queue.SimpleQueue()
        self._controller = SessionController(self._post, LivePipeline)
        self._display = DisplayModule(
            language_code(self.settings.source_language),
            language_code(self.settings.target_language, allow_auto=False) or "zh",
            translation_enabled=self.settings.translation_enabled,
        )
        # 保留旧验证脚本使用的只读入口；显示状态的实际变更只在 DisplayModule 内完成。
        self._history = self._display.history
        self._in_event_drain = False
        self._translation_visible = None
        self._overlay_source_text = ""
        self._overlay_source_label = "源语言"
        self._closing = False
        self._exit_requested = False
        self._stopping = False
        self._last_error = ""
        self._runtime_phase = "idle"
        self._last_metrics = {}
        self._model_manager_window: ModelManagerWindow | None = None
        self._live_update_after_id = None
        self._drain_after_id = None
        self._overlay_focus_after_id = None

        self.root.title(f"{APP_NAME} · {APP_DESCRIPTION}")
        try:
            self.root.iconbitmap(str(asset_path("icon.ico")))
        except tk.TclError:
            pass
        # 首次打开尽量多展示内容；屏幕较小时由滚动区域承载剩余内容。
        # 预留标题栏和任务栏，避免高 DPI 下窗口被撑出屏幕底部。
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        initial_width = max(460, min(980, screen_width - 48))
        initial_height = max(360, min(780, screen_height - 80))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(min(760, initial_width), min(520, initial_height))
        # 关设置窗只隐藏，不退出；退出走悬浮窗菜单「退出程序」。
        self.root.protocol("WM_DELETE_WINDOW", self._hide_settings)
        self._theme_preference = getattr(self.settings, "ui_theme", "system")
        self._palette = apply_theme(self.root, self._theme_preference)
        self._theme_poll_id = None
        self._create_variables()
        self._build_ui()
        if Settings.last_load_warning:
            self.transcript_notice_var.set(Settings.last_load_warning)
        self.root.report_callback_exception = self._report_ui_exception
        self.overlay = OverlayWindow(
            self.root,
            self.settings,
            self._show_settings,
            self.close,
        )
        self.overlay.on_presented = self._display_presented
        self._connect_mouse_passthrough()
        self._refresh_devices()
        self._apply_overlay_settings()
        self._on_language_changed()
        # 启动后立即显示悬浮窗，使识别结果始终有可见的承载窗口。
        self._show_overlay()
        # 托盘操作只投递主线程消息；启动识别由用户或显式启动选项决定。
        self._tray = TrayController(
            self._post, show_settings=self._show_settings, show_overlay=self._show_overlay,
            hide_overlay=self.overlay.hide, start=self._start, stop=self._stop, close=self.close,
            restore_mouse=self._restore_mouse_operation,
        ) if tray else None
        if self._tray:
            self._tray.start()
        self.root.after_idle(self._start_mouse_passthrough)
        self.root.bind("<Unmap>", self._on_settings_unmap, add="+")
        self.root.bind("<FocusIn>", self._queue_overlay_focus, add="+")
        self.root.bind("<FocusOut>", self._queue_overlay_focus, add="+")
        self._queue_overlay_focus()
        self.root.bind("<Control-Return>", self._toggle_recognition)
        self.root.bind("<Control-q>", lambda _: self.close())
        self.root.bind("<Escape>", lambda _: self._hide_settings())
        if self.settings.auto_start if autostart is None else autostart:
            self.root.after_idle(self._start)
        if self.settings.start_minimized:
            self.root.after(200, self._hide_settings_on_startup)
        self._drain_after_id = self.root.after(10, self._drain_events)
        self._theme_poll_id = self.root.after(1500, self._poll_theme)

    def _create_variables(self) -> None:
        self.theme_label_var = tk.StringVar(value=THEME_LABELS[self._theme_preference])
        self.source_var = tk.StringVar(value=self.settings.source_language)
        self.target_var = tk.StringVar(value=self.settings.target_language)
        self.model_var = tk.StringVar(value=self.settings.recognition_model)
        self.device_var = tk.StringVar(value=self.settings.recognition_device)
        self.device_display_var = tk.StringVar(value=device_mode_label(self.settings.recognition_device))
        self.device_hint_var = tk.StringVar()
        self.beam_size_var = tk.StringVar(value=str(self.settings.recognition_beam_size))
        self.context_var = tk.StringVar(
            value="启用" if self.settings.recognition_condition_on_previous_text else "关闭"
        )
        temperature_value = self.settings.recognition_temperature_schedule
        self.temperature_var = tk.StringVar(
            value=_TEMPERATURE_LABELS.get(temperature_value, temperature_value)
        )
        self.recognition_vars = {
            name: tk.StringVar(value=str(getattr(self.settings, "recognition_" + name)))
            for name in RECOGNITION_NUMERIC_OPTIONS
        }
        self.profile_hint_var = tk.StringVar()
        self.current_profile_var = tk.StringVar()
        self.model_path_var = tk.StringVar(value=self.settings.model_path)
        self.audio_device_var = tk.StringVar(value=self.settings.loopback_device)
        self.audio_default_var = tk.StringVar(value="Windows 默认播放：读取中……")
        self.audio_selection_var = tk.StringVar()
        self.audio_device_var.trace_add("write", self._update_audio_selection)
        self._update_audio_selection()
        self.auto_start_var = tk.BooleanVar(value=self.settings.auto_start)
        self.start_minimized_var = tk.BooleanVar(value=self.settings.start_minimized)
        self.translation_enabled_var = tk.BooleanVar(value=self.settings.translation_enabled)
        self.translation_var = tk.StringVar(value=self.settings.translation_backend)
        self.status_var = tk.StringVar(value="选择正在播放声音的设备，然后开始字幕。")
        self.runtime_state_var = tk.StringVar(value="准备就绪")
        self.input_level_var = tk.DoubleVar(value=0)
        self.input_hint_var = tk.StringVar(value="等待输入")
        self.runtime_device_var = tk.StringVar(value="实际设备 · 尚未启动")
        self.runtime_speed_var = tk.StringVar(value="处理 / 积压 · 开始后测量")
        self.runtime_guidance_var = tk.StringVar(value=runtime_view("idle")[1])
        self._runtime_details_preference = self.settings.show_runtime_details
        self.runtime_details_var = tk.BooleanVar(value=self._runtime_details_preference)
        self.model_filter_var = tk.StringVar(value=MODEL_FILTERS[0])
        self.model_device_filter_var = tk.StringVar(value=DEVICE_FILTERS[0])
        self.transcript_notice_var = tk.StringVar(value="最近 64 段可复制或导出；实时草稿会继续修订。")
        self.model_hint_var = tk.StringVar()
        self.translation_hint_var = tk.StringVar()
        self.translation_status_var = tk.StringVar(value="译文随原文更新。")
        self.model_display_var = tk.StringVar()
        self._model_labels = {
            item.key: f"[{'CPU/GPU' if 'cuda' in item.supported_devices else '仅 CPU'}] {model_label(item.key, item.display_name)}"
            for item in MODEL_DEFINITIONS
        }
        self._model_definitions = {item.key: item for item in MODEL_DEFINITIONS}
        self.source_display_var = tk.StringVar(value="等待识别")
        self.translation_display_var = tk.StringVar(value="等待翻译")
        self._initialize_parameter_drafts()
        self._initialize_mouse_passthrough()

    def _update_audio_selection(self, *_args) -> None:
        """选择项变化只更新选择状态，不冒充已打开的采集设备。"""
        self.audio_selection_var.set(f"已选择：{self.audio_device_var.get() or '未选择'}")

    def _scroll_page(self, notebook, title):
        page = ttk.Frame(notebook)
        notebook.add(page, text=title)
        canvas = tk.Canvas(page, highlightthickness=0, background=self._palette["canvas"])
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=bar.set)
        content = ttk.Frame(canvas, padding=(0, 6), style="Surface.TFrame")
        slot = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(slot, width=e.width))
        self._scroll_pages.append(canvas)
        return content

    def _build_ui(self) -> None:
        build_workspace(self)

    def _protect_control_wheel(self, parent) -> None:
        for child in parent.winfo_children():
            if isinstance(child, (ttk.Combobox, ttk.Scale)):
                child.bind("<MouseWheel>", self._scroll_over_control)
                child.bind("<Button-4>", self._scroll_over_control)
                child.bind("<Button-5>", self._scroll_over_control)
            self._protect_control_wheel(child)

    def _scroll_over_control(self, event):
        canvas = self._scroll_canvas_for(event.widget)
        if canvas is not None:
            delta = getattr(event, "delta", 0)
            direction = -1 if delta > 0 or getattr(event, "num", None) == 4 else 1
            canvas.yview_scroll(direction * max(1, abs(int(delta / 120))), "units")
        return "break"

    def _bind_card_wrapping(self, parent) -> None:
        for child in parent.winfo_children():
            if isinstance(child, ttk.LabelFrame):
                child.bind("<Configure>", self._resize_card_labels, add="+")
            self._bind_card_wrapping(child)

    def _resize_card_labels(self, event) -> None:
        frame = event.widget
        if not self._controls_alive() or not isinstance(frame, ttk.LabelFrame):
            return
        jobs = getattr(self, "_label_wrap_jobs", None)
        if jobs is None:
            self._label_wrap_jobs = jobs = {}
        if frame in jobs:
            return
        token = None
        def reflow():
            if jobs.get(frame) != token:
                return
            jobs.pop(frame, None)
            if not self._controls_alive():
                return
            try:
                if not frame.winfo_exists():
                    return
                for label in frame.winfo_children():
                    if not isinstance(label, ttk.Label):
                        continue
                    current = float(label.cget("wraplength") or 0)
                    manager = label.winfo_manager()
                    if current <= 0 or manager not in {"grid", "pack"}:
                        continue
                    if manager == "grid":
                        info = label.grid_info()
                        column, row = int(info["column"]), int(info["row"])
                        span = int(info["columnspan"])
                        width = frame.grid_bbox(column, row, column + span - 1,
                                                row + int(info["rowspan"]) - 1)[2]
                        if width <= 1:
                            continue
                        padx = info.get("padx", 0)
                        pads = padx if isinstance(padx, (tuple, list)) else label.tk.splitlist(str(padx))
                        left = label.winfo_pixels(str(pads[0])) if pads else 0
                        right = label.winfo_pixels(str(pads[-1])) if pads else 0
                        available = max(1, width - left - right - 4)
                    else:
                        # A non-filling packed label reports its own requested
                        # width. Use the enclosing slot so repeated layouts do
                        # not shrink that label a little further each time.
                        width = frame.winfo_width()
                        if width <= 1:
                            continue
                        available = max(1, width - 40)
                    if current != available:
                        label.configure(wraplength=available)
            except tk.TclError:
                return
        token = self.root.after_idle(reflow)
        jobs[frame] = token

    def _resize_preview(self, width: int) -> None:
        for label in (self.source_preview, self.translation_preview):
            label.configure(wraplength=max(250, width - 40))

    def _on_model_selected(self, _event=None) -> None:
        selected = self.model_display_var.get()
        key = next((key for key, label in self._model_labels.items() if label == selected), None)
        if key is not None:
            self.model_var.set(key)

    def _filter_models(self, _event=None) -> None:
        selected_group = self.model_filter_var.get()
        # 保留当前值在框内；筛选只改变可选列表，不触发配置写入。
        values = [label for key, label in self._model_labels.items()
                  if (selected_group == MODEL_FILTERS[0] or model_group(key) == selected_group)
                  and self._matches_device_filter(key)]
        self.model_combo.configure(values=values)
        self._update_model_description()
        if not values:
            self.model_hint_var.set("没有同时符合模型分类和设备筛选的模型。" + self.model_hint_var.get())

    def _on_device_selected(self, _event=None) -> None:
        self.device_var.set(device_mode_value(self.device_display_var.get()))

    def _matches_device_filter(self, key: str) -> bool:
        selected = self.model_device_filter_var.get()
        definition = self._model_definitions.get(key)
        if selected == DEVICE_FILTERS[0]:
            return True
        if definition is None:
            return False
        devices = definition.supported_devices
        return ((selected == "仅 CPU" and devices == ("cpu",))
                or (selected == "支持 CPU" and "cpu" in devices)
                or (selected == "支持 NVIDIA GPU" and "cuda" in devices))

    def _update_model_description(self, *_args) -> None:
        key = self.model_var.get()
        self.model_display_var.set(self._model_labels.get(key, key))
        definition = self._model_definitions.get(key)
        cpu_only = bool(definition and definition.backend != "whisper")
        self.device_combo.configure(state="disabled" if cpu_only else "readonly")
        self.device_display_var.set(device_mode_label("CPU（int8）" if cpu_only else self.device_var.get()))
        self.device_hint_var.set(device_mode_guidance(self.device_var.get(), cpu_only=cpu_only))
        description = f"支持设备：{definition.device_label}。{model_guidance(key)}" if definition else "当前保存的模型不在列表中；请打开管理确认模型。"
        selected_group = self.model_filter_var.get()
        if ((selected_group != MODEL_FILTERS[0] and model_group(key) != selected_group)
                or not self._matches_device_filter(key)):
            description = "当前模型保留；不在筛选结果中，可调整模型分类或设备筛选查看。 " + description
        self.model_hint_var.set(description)

    def _toggle_runtime_details(self) -> None:
        self._runtime_details_preference = self.runtime_details_var.get()
        if self.runtime_details_var.get():
            self._runtime_details_frame.pack(fill="x")
        else:
            self._runtime_details_frame.pack_forget()
        self._schedule_settings_save()

    def _resize_runtime(self, event) -> None:
        width = max(200, event.width - 20)
        for label in (self._status_label, self._guidance_label):
            label.configure(wraplength=width)
        self._speed_label.configure(wraplength=max(180, width - 180))
        for label in self._runtime_details_frame.winfo_children():
            if isinstance(label, ttk.Label) and float(label.cget("wraplength") or 0):
                label.configure(wraplength=width)

    def _set_runtime_phase(self, phase: str) -> None:
        self._runtime_phase = phase
        state, guidance = runtime_view(phase, self._last_metrics.get("input_rms"), self._last_metrics.get("backlog_seconds"))
        self.runtime_state_var.set(state)
        self.runtime_guidance_var.set(guidance)

    def _reveal_focused_control(self, event) -> None:
        widget = event.widget
        # Tk's native combobox popdown can report a Tcl pathname rather than
        # a registered Python widget. Its own bindings handle focus/scrolling.
        if not hasattr(widget, "winfo_class"):
            return
        # Tk also sends FocusIn to every ancestor of the focused control.
        # Revealing those containers scrolls a long settings page back to its top.
        if self.root.focus_get() is not widget:
            return
        if widget.winfo_class() == "TCombobox":
            self.root.after_idle(lambda: self._settle_combo_focus(widget))
        canvas = self._scroll_canvas_for(widget)
        if canvas is None or widget is canvas:
            return
        self.root.after_idle(lambda: self._ensure_control_visible(widget, canvas))

    @staticmethod
    def _settle_combo_focus(widget):
        from ui_guidance import keep_readonly_value_visible
        try:
            if widget.winfo_exists():
                keep_readonly_value_visible(widget)
        except tk.TclError:
            pass

    def _ensure_control_visible(self, widget, canvas) -> None:
        try:
            # Focus can move again before this idle callback runs.
            if self.root.focus_get() is not widget or not canvas.winfo_ismapped():
                return
            top = widget.winfo_rooty() - canvas.winfo_rooty()
            bottom = top + widget.winfo_height()
            viewport = canvas.winfo_height()
            bounds = canvas.bbox("all")
            if not bounds or bounds[3] <= viewport:
                return
            offset = min(top - 8, 0) if top < 8 else max(bottom - viewport + 8, 0)
            if offset:
                canvas.yview_moveto(max(0.0, (canvas.canvasy(0) + offset) / bounds[3]))
        except tk.TclError:
            pass

    def _keyboard_scroll(self, event):
        if not hasattr(event.widget, "winfo_class"):
            return
        if event.widget.winfo_class() in {"TCombobox", "TSpinbox", "Entry", "TEntry", "TScale"}:
            return
        canvas = self._scroll_canvas_for(event.widget)
        if canvas is not None:
            canvas.yview_scroll(-1 if event.keysym == "Prior" else 1, "pages")
            return "break"

    def _update_translation_notice(self, *_args) -> None:
        if self.translation_var.get() == "仅使用本地翻译":
            text = "识别文本留在本机。首次翻译需要下载对应语言模型，下载完成后可离线使用。"
        elif self.translation_var.get() == "本地优先，失败转在线":
            text = "本地翻译失败时，MyMemory 将接收识别文本并返回译文。"
        else:
            text = "MyMemory 将接收识别文本并返回译文，需要联网。"
        if self.source_var.get() in ("粤语", "自动检测"):
            text += " 粤语识别文字使用中文翻译模型，口语译文需留意。"
        self.translation_hint_var.set(text)

    def _build_help(self, parent) -> None:
        first = ttk.LabelFrame(parent, text="开始使用", padding=16)
        first.pack(fill="x", pady=(0, 10))
        ttk.Label(first, text="1  播放视频或游戏，在实时字幕页选择对应的“系统播放”设备。\n"
                  "2  选择声音的语言，再选支持该语言的模型；未下载时打开模型管理。\n"
                  "3  点击“开始字幕”。输入指示有声音后等待出字；字幕挡住设置时点顶部“暂藏字幕”，配置好后点“字幕窗”。",
                  justify="left", wraplength=720).pack(anchor="w")
        ttk.Button(first, text="打开模型管理", command=self._show_model_manager).pack(anchor="w", pady=(12, 0))
        trouble = ttk.LabelFrame(parent, text="怎么没有字幕？", padding=16)
        trouble.pack(fill="x", pady=(0, 10))
        for title, detail in (
            ("一直显示“等待声音”", "先播放带人声的视频，再看输入指示。显示“输入很低”时，点击下方刷新音源，回到实时字幕页选择实际播放的设备；切换耳机后也需要重新选择。"),
            ("卡在“正在准备”或出现错误", "展开运行详情查看下载、加载或音源的具体消息。模型未下载时打开模型管理；语言不匹配时换成支持该语言的模型。修改完成、停止释放结束后点击重新开始。"),
            ("有声音输入，但字幕慢", "看积压是否持续上升；上升说明模型正在追赶输入。点击停止，等待释放完成，再通过筛选选择适合 CPU 或较小的模型；已有 NVIDIA GPU 时也可核对实际设备。"),
            ("字幕找不到或挡住设置", "找不到时点击“找回字幕窗”移回当前屏幕；挡住设置时点顶部“暂藏字幕”，完成后点“字幕窗”。这不会停止识别。"),
            ("只有原文没有译文", "在实时字幕页启用翻译并选择不同的目标语言。首次本地翻译需下载模型，在线方式会发送识别文本。"),
        ):
            ttk.Label(trouble, text=title, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
            ttk.Label(trouble, text=detail, style="Muted.TLabel", wraplength=720, justify="left").pack(anchor="w")
        actions = ttk.Frame(trouble)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="刷新音源", command=self._refresh_devices).pack(side="left")
        ttk.Button(actions, text="显示字幕窗", command=self._show_overlay).pack(side="left", padx=8)
        ttk.Button(actions, text="找回字幕窗", command=lambda: self.overlay.restore_position()).pack(side="left")
        ttk.Button(actions, text="管理模型", command=self._show_model_manager).pack(side="left", padx=8)
        notes = ttk.LabelFrame(parent, text="读懂实时状态", padding=16)
        notes.pack(fill="x")
        ttk.Label(notes, text="输入电平反映采集到的声音大小，不判断是否有人说话。推理耗时是模型一次处理的实测时间，"
                  "积压是尚待处理的音频；两者均不等于从声卡到屏幕的完整延迟。\n"
                  "实时字幕可能先显示草稿再修订，最近 64 段可以回看、复制或导出 TXT。",
                  style="Muted.TLabel", wraplength=720, justify="left").pack(anchor="w")

    def _recent_transcript(self) -> str:
        return "\n\n".join("\n".join(text for text in (source, translation) if text)
                             for source, translation in self._history.entries()
                             if source or translation)

    def _copy_transcript(self) -> None:
        text = self._recent_transcript()
        if not text:
            self.transcript_notice_var.set(empty_transcript_hint(self.pipeline is not None))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.transcript_notice_var.set("最近字幕已复制。")

    def _export_transcript(self) -> None:
        text = self._recent_transcript()
        if not text:
            self.transcript_notice_var.set(empty_transcript_hint(self.pipeline is not None))
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="导出最近字幕", defaultextension=".txt",
                                          initialfile="声幕-最近字幕.txt", filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig") as output:
                output.write(text + "\n")
        except OSError as error:
            self.transcript_notice_var.set(f"导出失败：{error}。请选择可写入的位置后重试。")
        else:
            self.transcript_notice_var.set(f"最近字幕已导出：{path}")

    def _show_metrics(self, metrics: dict) -> None:
        if "translation_phase" in metrics and hasattr(self, "translation_status_var"):
            if not self.translation_enabled_var.get():
                self.translation_status_var.set("翻译已关闭。")
            else:
                self.translation_status_var.set({"translating":"正在翻译当前内容…", "overloaded":"翻译有积压，原文继续更新。", "error":"翻译暂不可用，原文继续；下一段会重新尝试。", "ready":"译文已更新"}.get(metrics["translation_phase"], "等待翻译"))
        """合并局部指标；phase来自管线，不解析状态字符串。"""
        self._last_metrics.update(metrics)
        measured = self._last_metrics
        if not self._stopping and not self._last_error:
            phase = metrics.get("phase")
            if phase in {"preparing", "waiting_audio", "recognizing"}:
                self._set_runtime_phase(phase)
            elif self._runtime_phase in {"waiting_audio", "recognizing"}:
                self._set_runtime_phase(self._runtime_phase)
        rms = finite_nonnegative(measured.get("input_rms"))
        if rms is not None:
            rms = min(1.0, rms)
            self.input_level_var.set(100 * rms ** .5)
            self.input_hint_var.set("输入很低" if rms < .001 else "有声音输入")
        device, compute = measured.get("actual_device"), measured.get("actual_compute_type")
        if device:
            self.runtime_device_var.set(f"实际设备 · {device}" + (f" / {compute}" if compute else ""))
        parts = []
        inference = finite_nonnegative(measured.get("inference_seconds"))
        backlog = finite_nonnegative(measured.get("backlog_seconds"))
        if inference is not None:
            parts.append(f"处理 {inference:.2f} 秒")
        if backlog is not None:
            parts.append(f"积压 {backlog:.2f} 秒")
        if parts:
            self.runtime_speed_var.set("  ·  ".join(parts))

        if getattr(self, "_controller", None) is not None:
            import time
            now = time.monotonic()
            if now - getattr(self, "_last_health_at", 0) >= 30:
                self._last_health_at = now
                logging.info("运行状态：phase=%s，input_rms=%s，backlog_seconds=%s，inference_seconds=%s，translation=%s",
                             self._runtime_phase, self._last_metrics.get("input_rms"),
                             self._last_metrics.get("backlog_seconds"), self._last_metrics.get("inference_seconds"),
                             self._controller.state.translation_phase)

    def _update_scroll_region(self, _event=None) -> None:
        """根据内容高度更新主窗口的可滚动范围。"""
        self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))

    def _resize_scroll_content(self, event) -> None:
        """让内容宽度跟随窗口，避免出现不必要的横向滚动。"""
        self._scroll_canvas.itemconfigure(self._scroll_window, width=event.width)

    def _scroll_canvas_for(self, widget):
        while widget is not None:
            if widget in self._scroll_pages:
                return widget
            # A popup may be parented to a field for ownership, but its focus
            # and wheel input belong to a separate window, not the form behind it.
            if isinstance(widget, tk.Toplevel):
                return None
            widget = getattr(widget, "master", None)
        return None

    def _in_scroll_content(self, widget):
        return self._scroll_canvas_for(widget) is not None

    def _on_settings_mousewheel(self, event):
        canvas = self._scroll_canvas_for(event.widget)
        if canvas is None:
            return
        if event.widget.winfo_class() in {"TCombobox", "TSpinbox", "Entry", "TEntry", "TScale"}:
            return
        delta = int(event.delta / 120)
        canvas.yview_scroll(-delta if delta else (-1 if event.delta < 0 else 1), "units")
        return "break"

    def _on_settings_scroll_button(self, event):
        canvas = self._scroll_canvas_for(event.widget)
        if canvas is not None and event.widget.winfo_class() not in {"TCombobox", "TSpinbox", "Entry", "TEntry", "TScale"}:
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
            return "break"

    def _toggle_recognition(self, _event=None):
        self._stop() if self.pipeline else self._start()
        return "break"

    def _update_current_profile(self, *_):
        matched = None
        for name in PROFILES:
            values = profile_values(name)
            if (self.beam_size_var.get() == str(values["beam"])
                and (self.context_var.get() == "启用") == values["context"]
                and self.temperature_var.get() == _TEMPERATURE_LABELS[values["temperature"]]
                and all(float(self.recognition_vars[key].get()) == values[key] for key in self.recognition_vars)):
                matched = name
                break
        self.current_profile_var.set(f"当前识别参数：{matched or '自定义'}；模型单独选择")
        self.profile_hint_var.set((PROFILES[matched]["description"] if matched else
                                  "点击预设可统一改参；模型、设备、语言和音频来源不变。")
                                 + " 当前引擎使用的参数自动应用；其余保留为对应引擎偏好。")
        for name, button in getattr(self, "_profile_buttons", {}).items():
            button.configure(text=("✓ " if name == matched else "") + name,
                             style="SelectedProfile.TButton" if name == matched else "TButton")

    def _apply_recognition_profile(self, name):
        values = profile_values(name)
        self._applying_profile = True
        try:
            self.beam_size_var.set(str(values["beam"]))
            self.temperature_var.set(_TEMPERATURE_LABELS[values["temperature"]])
            self.context_var.set("启用" if values["context"] else "关闭")
            for key, variable in self.recognition_vars.items():
                variable.set(str(values[key]))
        finally:
            self._applying_profile = False
        # Publish one complete set; variable traces cannot send intermediate combinations.
        self._queue_live_configuration()
        self._save_settings()
        self._update_current_profile()
        if self.pipeline is None:
            self._set_status(f"已选择“{name}”参数预设；模型保持不变。")

    def _build_overlay_controls(self, parent) -> None:
        parent.columnconfigure(1, weight=1)
        toggles = ttk.Frame(parent)
        toggles.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0,10))
        self._topmost_var = tk.BooleanVar(value=self.settings.overlay_topmost)
        self._auto_height_var = tk.BooleanVar(value=self.settings.overlay_auto_height)
        self._animation_var = tk.BooleanVar(value=self.settings.overlay_animation)
        for label, variable in (("窗口置顶",self._topmost_var),
                                ("自动调整浮窗高度",self._auto_height_var),
                                ("滚动动效",self._animation_var)):
            ttk.Checkbutton(toggles, text=label, variable=variable,
                            command=self._apply_overlay_settings).pack(side="left", padx=(0,12))
        presets = ttk.Frame(parent)
        presets.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0,10))
        for name in ("深色高对比", "浅色清晰", "透明描边"):
            ttk.Button(presets, text=name, command=lambda n=name: self._apply_appearance_preset(n)).pack(side="left", padx=(0,8))
        colors = ttk.Frame(parent)
        colors.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0,12))
        for index, (label, callback) in enumerate((
            ("背景颜色",self._choose_background), ("原文颜色",self._choose_text_color),
            ("原文描边",self._choose_outline), ("译文颜色",self._choose_translation_text_color),
            ("译文描边",self._choose_translation_outline),
        )):
            ttk.Button(colors, text=label, command=callback).grid(row=index//3,column=index%3,sticky="w",padx=(0,8),pady=4)
        specs = (
            ("原文字号", "font_scale", 14,48,self.settings.overlay_font_size,self._font_scale_changed,False),
            ("译文字号", "translation_font_scale",12,48,self.settings.overlay_translation_font_size,self._translation_font_scale_changed,False),
            ("背景不透明度", "background_opacity_scale",0,1,self.settings.overlay_background_opacity,self._background_opacity_scale_changed,True),
            ("文字不透明度", "text_opacity_scale",0,1,self.settings.overlay_text_opacity,self._text_opacity_scale_changed,True),
        )
        for row, (label, attr, lo, hi, value, callback, percent) in enumerate(specs,3):
            text = tk.StringVar(value=f"{value:.0%}" if percent else str(round(value)))
            ttk.Label(parent,text=label).grid(row=row,column=0,sticky="w",padx=(0,20),pady=12)
            def changed(value, output=text, apply=callback, pct=percent):
                output.set(f"{float(value):.0%}" if pct else str(round(float(value))))
                apply(value)
            control = ttk.Scale(parent,from_=lo,to=hi,value=value,command=changed)
            control.grid(row=row,column=1,sticky="ew",pady=12)
            setattr(self,attr,control)
            ttk.Label(parent,textvariable=text,width=6,anchor="e").grid(row=row,column=2,padx=(12,0))
        ttk.Label(parent,text="在字幕上拖动可移动窗口；右下角可调整大小，右键可回到最新或导出 TXT。",wraplength=680).grid(row=8,column=0,columnspan=3,sticky="w",pady=(16,0))

    def _apply_appearance_preset(self, name):
        presets = {
            "深色高对比": ("#10243a","#ffffff","#081420","#60e4f4","#081420",0.88),
            "浅色清晰": ("#f4f8fd","#142c43","#ffffff","#005b85","#ffffff",0.94),
            "透明描边": ("#10243a","#ffffff","#000000","#60e4f4","#000000",0.0),
        }
        values = presets[name]
        for key, value in zip(("overlay_background","overlay_text_color","overlay_outline",
                               "overlay_translation_text_color","overlay_translation_outline",
                               "overlay_background_opacity"),values):
            setattr(self.settings,key,value)
        self.background_opacity_scale.set(values[-1])
        self._apply_overlay_settings()

    def _add_labeled_combo(
        self,
        parent,
        row,
        label,
        variable,
        values,
        width=20,
        readonly=True,
    ):
        ttk.Label(parent, text=label).grid(row=row, column=0, padx=(0, 8), pady=5, sticky="w")
        combo = ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            width=width,
            state="readonly" if readonly else "normal",
        )
        combo.grid(row=row, column=1, columnspan=1, pady=5, sticky="ew")
        return combo

    def _parameter_variable(self, name):
        return self.temperature_var if name == "temperature_schedule" else (
            self.beam_size_var if name == "beam_size" else self.recognition_vars[name])

    def _parameter_value(self, name):
        drafts = getattr(self, "_parameter_drafts", {})
        if name in drafts:
            return drafts[name].committed
        text = self._parameter_variable(name).get()
        if name == "temperature_schedule":
            return temperature_values(_TEMPERATURE_VALUES_BY_LABEL.get(text, text))
        spec = PARAMETERS[name]
        return numeric_value(name, int(text) if spec.integer else float(text))

    def _initialize_parameter_drafts(self, master=None):
        master = self.root if master is None else master
        self._parameter_drafts = {
            name: ParameterDraft(name, getattr(self.settings, "recognition_" + name))
            for name in (*PARAMETERS, "temperature_schedule")
        }
        self._parameter_inputs = {
            name: tk.StringVar(master=master, value=draft.current_text)
            for name, draft in self._parameter_drafts.items()
        }
        self._parameter_hints = {name: tk.StringVar(master=master) for name in self._parameter_drafts}
        self._parameter_widgets = {}
        self._parameter_hint_labels = {}
        self._parameter_focus_jobs = set()
        self._syncing_parameter_input = False
        for name, variable in self._parameter_inputs.items():
            variable.trace_add("write", lambda *_, key=name: self._parameter_input_changed(key))
            self._parameter_variable(name).trace_add(
                "write", lambda *_, key=name: self._parameter_committed_changed(key))
            self._refresh_parameter_hint(name)

    def _controls_alive(self):
        if self._closing or getattr(self, "_exit_requested", False):
            return False
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _parameter_help(self, name):
        if name == "temperature_schedule":
            text = "每项0–1，1–8项；用逗号分隔。推荐0或0,0.2,0.4。仅Whisper。"
        else:
            spec = PARAMETERS[name]
            kind = "整数" if spec.integer else "数值"
            details = {
                "beam_size": "推荐1 / 3 / 5；数值越大通常越耗时。仅Whisper。",
                "window_seconds": "语音上下文上限；越长越耗时。仅Whisper。",
                "update_interval": "仅Whisper；CPU实际不低于0.5秒，CUDA不低于0.1秒。",
                "repetition_penalty": "1不额外惩罚；过高可能漏掉正常重复。仅Whisper。",
                "no_repeat_ngram_size": "0关闭；非零可能抑制正常重复内容。仅Whisper。",
                "no_speech_threshold": "过低可能跳过轻声片段。仅Whisper。",
                "vad_silence_ms": "Whisper / SenseVoice；太短易切碎，数值越大分段越慢。",
            }
            text = f"{spec.allowed_text}{kind}。{details[name]}"
        if not parameter_is_consumed(self.model_var.get(), name):
            text += " 当前引擎不使用，保留为对应引擎偏好。"
        return text

    def _refresh_parameter_hint(self, name):
        if not hasattr(self, "_parameter_drafts"):
            return
        draft = self._parameter_drafts[name]
        text = self._parameter_help(name)
        if draft.error:
            text = f"未应用：{draft.error}；当前值{draft.current_text}。{self._parameter_help(name)}"
        elif draft.dirty:
            text += f" 输入中，当前值{draft.current_text}；Enter或离开字段应用，Esc还原。"
        self._parameter_hints[name].set(text)
        widget = self._parameter_widgets.get(name)
        label = self._parameter_hint_labels.get(name)
        if widget is not None:
            try:
                if widget.winfo_exists():
                    widget.state(["invalid"] if draft.error else ["!invalid"])
            except tk.TclError:
                pass
        if label is not None:
            try:
                if label.winfo_exists():
                    label.configure(style="Danger.TLabel" if draft.error else "Muted.TLabel")
            except tk.TclError:
                pass

    def _refresh_parameter_hints(self, *_):
        for name in getattr(self, "_parameter_drafts", {}):
            self._refresh_parameter_hint(name)

    def _sync_parameter_input(self, name):
        self._syncing_parameter_input = True
        try:
            self._parameter_inputs[name].set(self._parameter_drafts[name].draft_text)
        finally:
            self._syncing_parameter_input = False
        self._refresh_parameter_hint(name)

    def _parameter_input_changed(self, name):
        if self._syncing_parameter_input or self._closing or getattr(self, "_exit_requested", False):
            return
        self._parameter_drafts[name].edit(self._parameter_inputs[name].get())
        self._refresh_parameter_hint(name)

    def _parameter_committed_changed(self, name):
        if self._closing or getattr(self, "_exit_requested", False):
            return
        text = self._parameter_variable(name).get()
        try:
            if name == "temperature_schedule":
                value = temperature_values(_TEMPERATURE_VALUES_BY_LABEL.get(text, text))
            else:
                spec = PARAMETERS[name]
                value = numeric_value(name, int(text) if spec.integer else float(text))
        except (ValueError, TypeError) as error:
            logging.error("Committed recognition parameter invalid: %s", name)
            self._parameter_drafts[name].error = str(error)
            self._refresh_parameter_hint(name)
            return
        self._parameter_drafts[name].set_committed(value)
        self._sync_parameter_input(name)

    def _parameter_widget_alive(self, name, owner=None):
        if not self._controls_alive():
            return False
        widget = self._parameter_widgets.get(name)
        if widget is None or (owner is not None and widget is not owner):
            return False
        try:
            return bool(widget.winfo_exists()) and not widget.instate(["disabled"])
        except tk.TclError:
            return False

    def _commit_parameter(self, name, _event=None, owner=None):
        if not self._parameter_widget_alive(name, owner):
            return "break"
        draft = self._parameter_drafts[name]
        changed = draft.submit()
        self._sync_parameter_input(name)
        if changed:
            text = draft.current_text
            if name == "temperature_schedule":
                text = _TEMPERATURE_LABELS.get(text, text)
            self._parameter_variable(name).set(text)
        return "break"

    def _cancel_parameter(self, name, _event=None, owner=None):
        if self._parameter_widget_alive(name, owner):
            self._parameter_drafts[name].cancel()
            self._sync_parameter_input(name)
        return "break"

    def _parameter_focus_out(self, name, _event=None, owner=None):
        if not self._parameter_widget_alive(name, owner):
            return
        widget = self._parameter_widgets[name]
        token = None
        def commit_if_outside():
            self._parameter_focus_jobs.discard(token)
            if not self._parameter_widget_alive(name, widget):
                return
            manager = getattr(self.root, "_shengmu_choices", None)
            popup = getattr(manager, "popup", None)
            try:
                focused = self.root.focus_get()
                if focused is widget:
                    return
                if popup is not None and popup.owner is widget and focused is not None:
                    top = popup.window
                    if focused is top or str(focused).startswith(str(top) + "."):
                        return
            except tk.TclError:
                return
            self._commit_parameter(name, owner=widget)
        token = self.root.after_idle(commit_if_outside)
        self._parameter_focus_jobs.add(token)

    def _add_parameter_combo(self, parent, row, name, width=18):
        draft = self._parameter_drafts[name]
        self._sync_parameter_input(name)
        label = "解码温度序列" if name == "temperature_schedule" else PARAMETERS[name].label
        values = (RECOGNITION_TEMPERATURE_SCHEDULES if name == "temperature_schedule"
                  else tuple(number_text(value) for value in PARAMETERS[name].recommended))
        widget = self._add_labeled_combo(parent, row, label, self._parameter_inputs[name],
                                         values, width=width, readonly=False)
        self._parameter_widgets[name] = widget
        widget._shengmu_parameter = True
        widget._shengmu_choice_value = lambda: draft.current_text
        widget._shengmu_choice_header = lambda: (
            f"当前值 {draft.current_text} · " + ("推荐" if draft.current_text in values else "自定义"))
        widget._shengmu_choice_closed = lambda: self._parameter_focus_out(name, owner=widget)
        widget.bind("<Return>", lambda event: self._commit_parameter(name, event, owner=widget))
        widget.bind("<KP_Enter>", lambda event: self._commit_parameter(name, event, owner=widget))
        widget.bind("<Control-Return>", lambda _event: None)
        widget.bind("<Escape>", lambda event: self._cancel_parameter(name, event, owner=widget))
        widget.bind("<FocusOut>", lambda event: self._parameter_focus_out(name, event, owner=widget))
        widget.bind("<<ComboboxSelected>>", lambda event: self._commit_parameter(name, event, owner=widget))
        widget.bind("<Destroy>", lambda event: self._parameter_field_destroyed(name, widget)
                    if event.widget is widget else None, add="+")
        hint = ttk.Label(parent, textvariable=self._parameter_hints[name], style="Muted.TLabel",
                         wraplength=620, justify="left")
        hint.grid(row=row+1, column=1, columnspan=2, sticky="ew", pady=(0,10))
        self._parameter_hint_labels[name] = hint
        self._refresh_parameter_hint(name)
        return widget

    def _parameter_field_destroyed(self, name, widget):
        if self._parameter_widgets.get(name) is widget:
            self._parameter_widgets.pop(name, None)
            self._parameter_hint_labels.pop(name, None)
            self._parameter_drafts[name].cancel()

    def _effective_parameter_options(self, options):
        current = getattr(getattr(self, "pipeline", None), "options", None)
        if current is None or current.model_size != options.model_size:
            return options
        inactive = {
            name: getattr(current, name)
            for name in (*PARAMETERS, "temperature_schedule", "condition_on_previous_text")
            if not parameter_is_consumed(options.model_size, name)
        }
        return replace(options, **inactive) if inactive else options


    def _refresh_devices(self) -> None:
        try:
            try:
                devices = SystemAudioCapture.list_audio_devices()
            except Exception as error:
                devices = []
                logging.warning("枚举系统音频设备失败：%s", error)
            choices = [*devices, PROCESS_AUDIO_DEVICE]
            self.audio_combo["values"] = choices
            self.audio_combo.configure(state="readonly")
            try:
                default_raw = SystemAudioCapture.default_loopback_device()
            except Exception as error:
                default_raw = ""
                logging.warning("读取 Windows 默认播放设备失败：%s", error)
            default_name = f"系统播放：{default_raw}" if default_raw else ""
            current = self.audio_device_var.get()
            if current not in choices:
                compatible_name = f"系统播放：{current}"
                self.audio_device_var.set(
                    compatible_name if current and compatible_name in choices
                    else default_name if default_name in choices
                    else devices[0] if devices
                    else current
                )
            self.audio_default_var.set(
                f"Windows 默认播放：{default_raw or '未读取'}"
            )
            self._set_status(
                "音源已刷新，确认选择后即可开始字幕。" if devices and self.pipeline is None
                else "音源已刷新。当前识别会应用所选音源。" if devices
                else "没有读取到普通音频设备。请检查 Windows 播放设备后刷新，或尝试 Beta 应用音频。"
            )
        except Exception as error:
            self.audio_combo.configure(state="readonly")
            self.audio_default_var.set(f"Windows 默认播放：读取失败（{error}）")
            self._set_status(f"音频设备不可用：{error}。请在 Windows Python 环境运行并安装 soundcard。")

    def _on_theme_selected(self, _event=None):
        self._theme_preference = next((key for key, value in THEME_LABELS.items()
                                       if value == self.theme_label_var.get()), "system")
        self._refresh_theme()
        self._schedule_settings_save()

    def _refresh_theme(self):
        self._palette = apply_theme(self.root, self._theme_preference)
        for canvas in self._scroll_pages:
            canvas.configure(background=self._palette["canvas"])
        manager = self._model_manager_window
        if manager is not None and not manager._closed:
            manager._scroll_canvas.configure(background=self._palette["surface"])

    def _poll_theme(self):
        self._theme_poll_id = None
        if self._closing:
            return
        if self._theme_preference == "system" and system_theme() != self._palette["theme"]:
            self._refresh_theme()
        self._theme_poll_id = self.root.after(1500, self._poll_theme)

    def _export_diagnostics(self):
        filename = filedialog.asksaveasfilename(parent=self.root, title="保存诊断报告", defaultextension=".json",
                                               initialfile="声幕-诊断.json", filetypes=[("JSON", "*.json")])
        if not filename:
            return
        try:
            export_diagnostics(filename, version=app_version(), settings=self.settings,
                               metrics=self._last_metrics, state=self._controller.state, build=build_info())
        except OSError as error:
            messagebox.showerror("无法导出", str(error), parent=self.root)
            return
        self.transcript_notice_var.set("诊断报告已保存；不含录音或字幕正文。")

    def _start(self) -> None:
        if self.pipeline or self._stopping:
            return
        self._save_settings()
        options = self._pipeline_options_from_ui()
        if options is None:
            return
        source = options.source_language
        target = options.target_language
        self._pending_presentations.clear()
        self._trace_versions.clear()
        self._display.reset(source, target)
        self.overlay.reset_content()
        self._flush_display()
        # 每次启动建立新的会话；旧会话的排队回调不更新新会话。
        self._session += 1
        session = self._session
        self._last_error = ""
        self.runtime_details_var.set(self._runtime_details_preference)
        if self._runtime_details_preference:
            self._runtime_details_frame.pack(fill="x")
        else:
            self._runtime_details_frame.pack_forget()
        self._has_source = False
        self._last_metrics = {}
        self._set_runtime_phase("preparing")
        self.transcript_notice_var.set("最近 64 段可复制或导出；实时草稿会继续修订。")
        self.input_level_var.set(0)
        self.input_hint_var.set("等待输入")
        self.runtime_device_var.set("实际设备 · 等待模型加载")
        self.runtime_speed_var.set("处理 / 积压 · 等待测量")
        self._set_status(f"正在准备模型与音频：{self.audio_device_var.get()}")
        handlers = {
            "on_status": self._set_status,
            "on_source": self._show_source,
            "on_translation": self._show_translation,
            "on_translation_update": self._show_translation_update,
            "on_translation_context": self._show_translation_context,
            "on_error": self._show_error,
            "on_stopped": self._pipeline_stopped,
            "on_update": self._show_timed_source,
            "on_reset": self._reset_runtime_display,
            "on_metrics": self._show_metrics,
        }
        try:
            self.pipeline = self._controller.start(options, handlers)
        except Exception as error:
            logging.exception("字幕会话启动失败")
            self._show_error(f"无法开始字幕：{error}")
            self._pipeline_stopped()
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._show_overlay()
        if getattr(self, "_tray", None):
            self._tray.set_running(True)

    def _stop(self) -> None:
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
            self._live_update_after_id = None
        pipeline = self.pipeline
        if pipeline and not self._stopping:
            self._stopping = True
            self._set_runtime_phase("stopping")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self._set_status("正在释放识别资源，请稍候……")
            self._controller.stop()

    def _pipeline_stopped(self) -> None:
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
            self._live_update_after_id = None
        self.pipeline = None
        self._stopping = False
        if getattr(self, "_tray", None):
            self._tray.set_running(False)
        if not self._closing:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self._set_runtime_phase("error" if self._last_error else "stopped")
            self.start_button.configure(text="重新开始")
            self.input_level_var.set(0)
            self.input_hint_var.set("输入已停止")
            if self._last_error:
                self._set_status(self._last_error)
            else:
                self._set_status("字幕已停止，可调整音源或模型后重新开始。")

    def _apply_display_event(self, event: DisplayEvent) -> None:
        """把一个后台结果交给显示模块；悬浮窗更新统一延迟到批次结束。"""
        if not self._display.apply(event):
            return False
        if not self._in_event_drain:
            self._flush_display()
        return True

    def _show_timed_source(self, update: SourceUpdate) -> None:
        """源文保留完整身份，计时仍只登记真正进入显示模块的版本。"""
        if not self._display.apply_update(update):
            return
        self._has_source = True
        if not self._stopping and not self._last_error:
            self._set_runtime_phase("recognizing")
        self._pending_presentations[update.block_id] = update
        if get_runtime_trace().enabled:
            self._trace_versions[update.block_id] = update
            while len(self._trace_versions) > 65:
                self._trace_versions.pop(next(iter(self._trace_versions)))
        if not self._in_event_drain:
            self._flush_display()

    def _display_presented(self, presented_at: float, source_indices: set[int]) -> None:
        """仅记录实际绘制的原文段落；提交完成不等同于显示器扫描完成。"""
        fragments_by_entry = getattr(getattr(self, "overlay", None), "visible_translation_fragments", {})
        block_ids = list(self._history.blocks)
        visible_translations = []
        for index, fragments in fragments_by_entry.items():
            if not 0 <= index < len(block_ids):
                continue
            block_id = block_ids[index]
            block = self._history.blocks[block_id]
            version = getattr(block, "translation_version", None)
            if version is None or not block.translation:
                continue
            visible_translations.append(dict(
                entry=index, pipeline_id=version.pipeline_id, session_id=version.session_id,
                source_id=version.source_id, block_id=block_id, sequence=version.sequence,
                revision=version.revision, final=version.final,
                translation_epoch=self._display.translation_epoch,
                source_text=version.text, source_sequence=block.sequence, source_final=block.final,
                translation_state=block.translation_state,
                pending=block.translation_state == "updating",
                fragments=[{"text": text} for text in fragments],
            ))
        signature = tuple((item["block_id"], item["sequence"], item["translation_epoch"],
                           item["translation_state"], tuple(f["text"] for f in item["fragments"]))
                          for item in visible_translations)
        if signature and signature != getattr(self, "_last_translation_frame_signature", None):
            logging.info("译文窗口已提交：段落数=%s，字符数=%s", len(visible_translations),
                         sum(len(f["text"]) for item in visible_translations for f in item["fragments"]))
            get_runtime_trace().record("translation_frame", presented_at=presented_at,
                                       visible=visible_translations,
                                       meaning="Visible translated lines submitted to Windows compositor")
        self._last_translation_frame_signature = signature
        if getattr(get_runtime_trace(), "enabled", False):
            self._record_visible_frame(presented_at)
        if not self._pending_presentations:
            return
        block_ids = list(self._history.blocks)
        preview_id = getattr(self._history, "_preview_block_id", None)
        if preview_id is not None and preview_id not in block_ids:
            block_ids.append(preview_id)
        for index in source_indices:
            if index >= len(block_ids):
                continue
            block_id = block_ids[index]
            update = self._pending_presentations.pop(block_id, None)
            displayed_sequence = (self._history._preview_sequence if block_id == preview_id
                                  else self._history.blocks[block_id].sequence)
            if update is None or displayed_sequence != update.sequence:
                continue
            measurements = update.measurements(presented_at)
            reading_mode = "实时" if getattr(getattr(self, "overlay", None), "_follow_tail", True) else "历史"
            get_runtime_trace().record_presented(update, presented_at, reading_mode)
            logging.info(
                "字幕窗口已提交：序号=%s，段落=%s，音频终点=%.3f秒，阅读模式=%s，耗时=%s",
                update.sequence, block_id, update.audio_end,
                reading_mode, measurements,
            )
        # 阅读停留期间末尾可能尚未进入视口，不能拿旧的可见前缀冒充新结果。
        # 每段只保留最新版本；离开有界历史的段落才丢弃记录。
        for block_id in set(self._pending_presentations) - set(block_ids):
            self._pending_presentations.pop(block_id, None)

    def _record_visible_frame(self, presented_at):
        """Record only the actual lines drawn, never an off-screen paragraph tail."""
        block_ids = list(self._history.blocks)
        preview_id = self._history._preview_block_id
        if preview_id is not None and preview_id not in block_ids:
            block_ids.append(preview_id)
        visible = []
        for index, fragments in self.overlay.visible_source_fragments.items():
            if not 0 <= index < len(block_ids):
                continue
            block_id = block_ids[index]
            update = self._trace_versions.get(block_id)
            sequence = (self._history._preview_sequence if block_id == preview_id
                        else self._history.blocks[block_id].sequence)
            if update is not None and sequence == update.sequence:
                visible.append(dict(pipeline_id=update.pipeline_id, sequence=sequence,
                                    block_id=block_id, fragments=fragments))
        mode = "live" if self.overlay._follow_tail else "history"
        get_runtime_trace().record_frame(presented_at, mode, visible)

    def _show_source(
        self,
        sequence: int,
        text: str,
        language: str | None,
        block_id: int | None = None,
    ) -> None:
        self._has_source = True
        if not self._stopping and not self._last_error:
            self._set_runtime_phase("recognizing")
        self._apply_display_event(
            DisplayEvent("source", sequence, text, language, block_id)
        )

    def _show_preview(self, sequence: int, text: str, language: str | None, block_id: int) -> None:
        """显示短窗口即时草稿，完整识别结果到达后覆盖当前文字。"""
        self._has_source = True
        if not self._stopping and not self._last_error:
            self._set_runtime_phase("recognizing")
        self._apply_display_event(
            DisplayEvent("preview", sequence, text, language, block_id)
        )

    def _show_translation(self, sequence: int, text: str, language: str) -> None:
        if self._apply_display_event(DisplayEvent("translation", sequence, text, language)):
            self.translation_status_var.set("译文已更新")
            logging.info("译文已接收：序号=%s，语言=%s，字符数=%s", sequence, language, len(text))
            get_runtime_trace().record("translation", sequence=sequence, language=language, text=text)

    def _show_translation_context(self, context: TranslationContext) -> None:
        if self._display.apply_translation_context(context):
            get_runtime_trace().record("translation_context", **asdict(context))
            if not self._in_event_drain:
                self._flush_display()

    def _show_translation_update(self, update: TranslationUpdate) -> None:
        changed = self._display.apply_translation_update(update)
        if changed:
            # 当前配置的结果确已进入显示，结束此前的翻译错误或积压提示。
            self._last_metrics["translation_phase"] = "ready"
            logging.info("译文已接收：序号=%s，语言=%s，字符数=%s，定稿=%s",
                         update.sequence, update.target_language, len(update.text), update.final)
        get_runtime_trace().record("translation" if changed else "translation_not_applied",
                                   **asdict(update), display_applied=changed)
        if not self._in_event_drain:
            self._flush_display()

    def _show_error(self, text: str) -> None:
        if text.startswith("翻译暂不可用"):
            if not self.translation_enabled_var.get():
                logging.debug("翻译已关闭，略过排队的翻译错误提示。")
                self.translation_status_var.set("翻译已关闭。")
                return
            self.translation_status_var.set(text)
            self._set_status(text)
            return
        self._last_error = text
        if not self._stopping:
            self._set_runtime_phase("error")
        self._set_status(text)
        self.runtime_details_var.set(True)
        self._runtime_details_frame.pack(fill="x")

    def _report_ui_exception(self, exception_type, value, traceback):
        logging.error("界面操作回调失败", exc_info=(exception_type, value, traceback))
        get_runtime_trace().record("ui_error", exception_type=exception_type.__name__)
        self._show_error("界面操作未完成，字幕内容仍保留。可重试操作，或停止后重新开始。")

    def _on_translation_enabled_changed(self, *_args) -> None:
        self._display.set_translation_enabled(self.translation_enabled_var.get())
        # 新开关状态重新等待本轮翻译，不继承前一轮错误或拥塞提示。
        self._last_metrics.pop("translation_phase", None)
        self._flush_display()
        self._queue_live_configuration()

    def _on_language_changed(self, *_args) -> None:
        self._update_translation_notice()
        self._update_translation_visibility()
        self._queue_live_configuration()

    def _pipeline_options_from_ui(self) -> PipelineOptions | None:
        """读取并校验当前控件值，供启动和运行中切换共用。"""
        source = language_code(self.source_var.get())
        target = language_code(self.target_var.get(), allow_auto=False)
        try:
            validate_model_language(self.model_var.get(), source)
            if (self.model_var.get().startswith("zipformer-") and source is None
                    and self.translation_enabled_var.get()):
                raise ValueError("该流式模型不判断声音的语言；启用翻译时请选择中文或英语，或关闭翻译使用中英混合识别。")
        except ValueError as error:
            self._show_error(str(error))
            if self.pipeline is None:
                messagebox.showerror("模型与语言不匹配", str(error), parent=self.root)
            return None
        if not target:
            if self.pipeline is None:
                messagebox.showerror("配置错误", "目标语言不能为空。")
            else:
                self._set_status("目标语言不能为空，未应用这次修改。")
            return None
        audio_device = self.audio_device_var.get()
        available_devices = tuple(self.audio_combo["values"])
        if not audio_device or audio_device not in available_devices:
            self._set_status("音频来源不是当前枚举设备，请刷新设备后重新选择。")
            return None
        beam_size = self._parameter_value("beam_size")
        options = PipelineOptions(
            source_language=source,
            target_language=target,
            model_size=self.model_var.get(),
            device_mode=self.device_var.get(),
            model_path=self.model_path_var.get(),
            audio_device=audio_device,
            translation_backend=self.translation_var.get(),
            translation_enabled=self.translation_enabled_var.get(),
            beam_size=beam_size,
            condition_on_previous_text=self.context_var.get() == "启用",
            temperature_schedule=temperature_text(self._parameter_value("temperature_schedule")),
            **{name: self._parameter_value(name) for name in RECOGNITION_NUMERIC_OPTIONS},
        )
        return self._effective_parameter_options(options)

    def _queue_live_configuration(self, *_args) -> None:
        """把连续的界面修改合并，交给识别线程应用。"""
        if self.pipeline is None or self._closing or getattr(self, "_exit_requested", False) or getattr(self, "_stopping", False) or getattr(self, "_applying_profile", False):
            return
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
        self._live_update_after_id = self.root.after(120, self._apply_live_configuration)

    def _apply_live_configuration(self) -> None:
        self._live_update_after_id = None
        pipeline = self.pipeline
        if self._closing or getattr(self, "_exit_requested", False) or pipeline is None or getattr(self, "_stopping", False):
            return
        options = self._pipeline_options_from_ui()
        if options is None:
            return
        if options == pipeline.options and not getattr(getattr(self, "_display", None), "awaiting_translation_context", False):
            return
        translation_changed = any(
            getattr(pipeline.options, key) != getattr(options, key)
            for key in ("source_language", "target_language", "translation_backend", "translation_enabled")
        )
        if translation_changed:
            self._display.invalidate_translation_context()
            self._flush_display()
        if self._controller.update_options(options):
            self._set_status("正在应用新的设置……")
        elif self._display.awaiting_translation_context:
            # UI设置来回后实际配置可能未变；仍切新epoch并重投当前源文。
            self._controller.refresh_translation_context()

    def _reset_runtime_display(self) -> None:
        """切换设置后清空上一组字幕，避免新旧内容混在一起。"""
        source = language_code(self.source_var.get())
        target = language_code(self.target_var.get(), allow_auto=False) or "zh"
        self._pending_presentations.clear()
        self._display.reset(source, target)
        self.overlay.reset_content()
        self._flush_display()

    def _update_translation_visibility(self) -> None:
        source = language_code(self.source_var.get())
        target = language_code(self.target_var.get(), allow_auto=False) or "zh"
        changed = self._display.set_languages(source, target)
        if not changed:
            return
        self._flush_display()

    def _apply_translation_visibility(self, visible: bool) -> None:
        """只处理 Tk 控件和悬浮窗的可见性，不参与字幕状态计算。"""
        if self._translation_visible == visible:
            return
        self._translation_visible = visible
        if visible:
            self.translation_preview.pack(anchor="w", fill="x", pady=(8, 0), before=self.preview_separator)
        else:
            self.translation_preview.pack_forget()

    def _choose_text_color(self) -> None:
        selected = colorchooser.askcolor(color=self.settings.overlay_text_color, title="选择文字颜色")
        if selected[1]:
            self.settings.overlay_text_color = selected[1]
            self._apply_overlay_settings()

    def _choose_background(self) -> None:
        selected = colorchooser.askcolor(color=self.settings.overlay_background, title="选择悬浮窗背景颜色")
        if selected[1]:
            self.settings.overlay_background = selected[1]
            self._apply_overlay_settings()

    def _choose_outline(self) -> None:
        selected = colorchooser.askcolor(color=self.settings.overlay_outline, title="选择文字描边颜色")
        if selected[1]:
            self.settings.overlay_outline = selected[1]
            self._apply_overlay_settings()

    def _choose_translation_text_color(self) -> None:
        selected = colorchooser.askcolor(
            color=self.settings.overlay_translation_text_color,
            title="选择译文颜色",
        )
        if selected[1]:
            self.settings.overlay_translation_text_color = selected[1]
            self._apply_overlay_settings()

    def _choose_translation_outline(self) -> None:
        selected = colorchooser.askcolor(
            color=self.settings.overlay_translation_outline,
            title="选择译文描边颜色",
        )
        if selected[1]:
            self.settings.overlay_translation_outline = selected[1]
            self._apply_overlay_settings()

    def _font_scale_changed(self, value) -> None:
        self.settings.overlay_font_size = int(float(value))
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()
        self._schedule_settings_save()

    def _translation_font_scale_changed(self, value) -> None:
        self.settings.overlay_translation_font_size = int(float(value))
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()
        self._schedule_settings_save()

    def _background_opacity_scale_changed(self, value) -> None:
        self.settings.overlay_background_opacity = float(value)
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()
        self._schedule_settings_save()

    def _text_opacity_scale_changed(self, value) -> None:
        self.settings.overlay_text_opacity = float(value)
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()
        self._schedule_settings_save()

    def _apply_overlay_settings(self) -> None:
        if hasattr(self, "_topmost_var"):
            self.settings.overlay_topmost = self._topmost_var.get()
        if hasattr(self, "_auto_height_var"):
            self.settings.overlay_auto_height = self._auto_height_var.get()
        if hasattr(self, "_animation_var"):
            self.settings.overlay_animation = self._animation_var.get()
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()
        self._schedule_settings_save()

    def _show_overlay(self) -> None:
        if not self._closing and not getattr(self, "_exit_requested", False):
            self.overlay.show()

    def _queue_overlay_focus(self, _event=None) -> None:
        if self._closing:
            return
        if self._overlay_focus_after_id is not None:
            self.root.after_cancel(self._overlay_focus_after_id)
        self._overlay_focus_after_id = self.root.after_idle(self._apply_overlay_focus)

    def _apply_overlay_focus(self) -> None:
        self._overlay_focus_after_id = None
        if self._closing:
            return
        try:
            focused = self.root.focus_get()
            top = focused.winfo_toplevel() if focused is not None else None
            active = (top is not None and top is not getattr(self.overlay, "window", None)
                      and (top is not self.root or self.root.state() not in {"withdrawn", "iconic"}))
            self.overlay.set_configuration_active(active)
        except (tk.TclError, KeyError):
            pass

    def _initialize_mouse_passthrough(self, master=None):
        master = self.root if master is None else master
        self._controls_thread_id = threading.get_ident()
        self._mouse_passthrough_committed = self.settings.overlay_mouse_passthrough
        self._mouse_passthrough_effective = None
        self._mouse_passthrough_initialized = False
        self._mouse_passthrough_startup_failed = False
        self._mouse_passthrough_owner = None
        self._mouse_passthrough_request = None
        self.mouse_passthrough_var = tk.IntVar(master=master, value=-1)
        self.mouse_passthrough_status_var = tk.StringVar(master=master, value="尚未确认鼠标操作状态。")

    def _sync_mouse_passthrough_control(self):
        widget = getattr(self, "mouse_passthrough_check", None)
        if widget is None or not self._controls_alive():
            return
        try:
            if widget.winfo_exists():
                effective = self._mouse_passthrough_effective
                widget.state(["alternate", "!selected"] if effective is None else
                             ["!alternate", "selected" if effective else "!selected"])
        except tk.TclError:
            pass

    def _layout_mouse_controls(self, _event=None):
        if not self._controls_alive():
            return
        frame = self._mouse_passthrough_frame
        try:
            if not frame.winfo_exists():
                return
            stacked = (self.mouse_passthrough_check.winfo_reqwidth()
                       + self._mouse_restore_button.winfo_reqwidth()+32 > frame.winfo_width())
            if stacked == getattr(self, "_mouse_controls_stacked", None):
                return
            self._mouse_controls_stacked = stacked
            self._mouse_restore_button.grid_configure(row=1 if stacked else 0, column=0 if stacked else 1,
                                                       columnspan=2 if stacked else 1,
                                                       sticky="w" if stacked else "e", pady=(8,0) if stacked else 0)
            self._mouse_status_label.grid_configure(row=2 if stacked else 1)
            self._mouse_help_label.grid_configure(row=3 if stacked else 2)
        except tk.TclError:
            pass

    def _connect_mouse_passthrough(self):
        owner = self.overlay
        self._mouse_passthrough_owner = owner
        owner.on_mouse_passthrough_changed = lambda result: self._mouse_passthrough_changed(owner, result)

    def _start_mouse_passthrough(self):
        if not self._controls_alive():
            return
        # The settings restore button already exists, and tray setup has run.
        # Keep the taskbar settings entry reachable when no tray is available.
        self._mouse_passthrough_initialized = True
        self._request_mouse_passthrough(self._mouse_passthrough_committed)

    def _on_mouse_passthrough_toggle(self):
        if self._controls_alive():
            self._request_mouse_passthrough(self.mouse_passthrough_var.get() == 1)

    def _restore_mouse_operation(self):
        self._request_mouse_passthrough(False)

    def _request_mouse_passthrough(self, enabled):
        if (threading.get_ident() != self._controls_thread_id or not self._controls_alive()
                or type(enabled) is not bool):
            return
        owner = getattr(self, "overlay", None)
        if owner is not self._mouse_passthrough_owner:
            return
        # The native setter publishes synchronously; only the callback commits.
        request = (owner, enabled)
        self._mouse_passthrough_request = request
        try:
            owner.set_mouse_passthrough(enabled)
        finally:
            if self._mouse_passthrough_request is request:
                self._mouse_passthrough_request = None

    def _mouse_passthrough_changed(self, owner, result):
        if (threading.get_ident() != self._controls_thread_id or not self._controls_alive()
                or owner is not getattr(self, "overlay", None)
                or owner is not self._mouse_passthrough_owner):
            return
        effective = result.effective
        if effective is not None and type(effective) is not bool:
            return
        self._mouse_passthrough_effective = effective
        self.mouse_passthrough_var.set(-1 if effective is None else int(effective))
        self._sync_mouse_passthrough_control()
        request = self._mouse_passthrough_request
        explicit = request is not None and request[0] is owner and request[1] is result.requested
        if (explicit and result.success
                and type(result.requested) is bool and effective is result.requested):
            changed = self._mouse_passthrough_committed is not effective
            self._mouse_passthrough_committed = effective
            self.settings.overlay_mouse_passthrough = effective
            if changed:
                self._schedule_settings_save()
        if effective is True:
            text = "已穿透：鼠标直接操作下方窗口。请从本页或托盘恢复鼠标操作。"
        elif effective is False:
            text = "可操作：字幕窗支持拖动、右键菜单和历史滚动。"
        else:
            text = "状态未确认：请点“恢复鼠标操作”重新核实；保存偏好保持不变。"
        if not result.success:
            self._mouse_passthrough_startup_failed = True
            text = "切换未成功。" + text
            if effective is not None:
                text += " 保存偏好保持不变。"
            self._show_settings()
            self._set_status("字幕窗鼠标状态未按请求确认，可在字幕外观页恢复鼠标操作。")
        self.mouse_passthrough_status_var.set(text)

    def _hide_settings_on_startup(self):
        if self._controls_alive() and not self._mouse_passthrough_startup_failed:
            self._hide_settings()

    def _hide_settings(self) -> None:
        """关闭设置窗＝隐藏；识别和悬浮窗继续运行。"""
        if self._closing or getattr(self, "_exit_requested", False):
            return
        self._schedule_settings_save()
        self.overlay.set_configuration_active(False)
        try:
            if self._tray is not None and self._tray.available:
                self.root.withdraw()
            else:
                self.root.iconify()
                self._set_status("托盘图标暂不可用，已保留任务栏入口。")
        except tk.TclError:
            pass

    def _on_settings_unmap(self, event):
        if event.widget is self.root and not self._closing and self.root.state() == "iconic":
            self.root.after_idle(self._hide_settings)

    def _show_settings(self) -> None:
        if self._closing or getattr(self, "_exit_requested", False):
            return
        self.root.deiconify()
        self.root.lift()
        self._queue_overlay_focus()

    def _show_model_manager(self) -> None:
        """打开模型下载和管理窗口。"""
        if self._model_manager_window is not None:
            try:
                if self._model_manager_window._window.winfo_exists():
                    self._model_manager_window._window.deiconify()
                    self._model_manager_window._window.lift()
                    return
            except tk.TclError:
                pass
        self._model_manager_window = ModelManagerWindow(self.root)
        theme_window(self._model_manager_window._window, self._palette)

    def _save_settings(self) -> None:
        self.settings.source_language = self.source_var.get()
        self.settings.target_language = self.target_var.get()
        self.settings.recognition_model = self.model_var.get()
        self.settings.recognition_device = self.device_var.get()
        self.settings.recognition_beam_size = self._parameter_value("beam_size")
        self.settings.recognition_condition_on_previous_text = self.context_var.get() == "启用"
        self.settings.recognition_temperature_schedule = temperature_text(self._parameter_value("temperature_schedule"))
        for name in RECOGNITION_NUMERIC_OPTIONS:
            setattr(self.settings, "recognition_" + name, self._parameter_value(name))
        self.settings.model_path = self.model_path_var.get()
        self.settings.loopback_device = self.audio_device_var.get()
        self.settings.auto_start = self.auto_start_var.get()
        self.settings.start_minimized = self.start_minimized_var.get()
        self.settings.overlay_mouse_passthrough = getattr(self, "_mouse_passthrough_committed", self.settings.overlay_mouse_passthrough)
        self.settings.show_runtime_details = self._runtime_details_preference
        self.settings.translation_enabled = self.translation_enabled_var.get()
        self.settings.translation_backend = self.translation_var.get()
        if hasattr(self, "overlay"):
            self.overlay.persist_geometry()
        self.settings.ui_theme = self._theme_preference
        try:
            self.settings.save()
        except (OSError, ValueError) as error:
            logging.warning("设置未保存：%s", error)
            self.transcript_notice_var.set(f"设置未保存：{error}")

    def _schedule_settings_save(self, *_args) -> None:
        """设置变更后防抖保存，避免每次拖动都写盘。"""
        if self._closing or getattr(self, "_exit_requested", False) or getattr(self, "_applying_profile", False):
            return
        if getattr(self, "_settings_save_after_id", None) is not None:
            try:
                self.root.after_cancel(self._settings_save_after_id)
            except tk.TclError:
                pass
        try:
            self._settings_save_after_id = self.root.after(400, self._settings_save_debounced)
        except tk.TclError:
            pass

    def _settings_save_debounced(self) -> None:
        self._settings_save_after_id = None
        if self._closing or getattr(self, "_exit_requested", False):
            return
        try:
            self._save_settings()
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        if getattr(self, "_runtime_phase", "idle") == "preparing":
            self.runtime_guidance_var.set(compact_status(text))

    def _post(self, callback, *args) -> None:
        if not self._closing and not getattr(self, "_exit_requested", False):
            self._events.put((callback, args))

    def _dispatch_session(self, session, callback, *args) -> None:
        if (session == self._session and
                (not getattr(self, "_stopping", False) or callback == self._pipeline_stopped)):
            callback(*args)

    def _drain_events(self) -> None:
        """只在主线程调用 Tk，后台线程只投递普通队列消息。"""
        self._drain_after_id = None
        if self._closing:
            return
        self._in_event_drain = True
        try:
            for _ in range(100):
                try:
                    callback, args = self._events.get_nowait()
                except queue.Empty:
                    break
                if self._closing or getattr(self, "_exit_requested", False):
                    break
                callback(*args)
        finally:
            self._in_event_drain = False
            # 一个定时批次只提交一次悬浮窗快照，避免识别频率直接决定重绘频率。
            if not self._closing and not getattr(self, "_exit_requested", False):
                self._flush_display()
                if not self._closing and not getattr(self, "_exit_requested", False):
                    self._drain_after_id = self.root.after(10, self._drain_events)

    def _flush_display(self) -> None:
        """将显示模块的脏状态一次性提交给主窗口和悬浮窗。"""
        snapshot = self._display.take_snapshot()
        if snapshot is None:
            return
        self.source_display_var.set(snapshot.latest_source or "字幕会显示在这里。选择音源和语言，点击“开始字幕”。")
        self.translation_display_var.set(snapshot.latest_translation or "译文随原文更新。")
        if not snapshot.translation_visible:
            self.translation_status_var.set("当前原文与目标语言相同，无需翻译。"
                                            if self.translation_enabled_var.get() else "翻译已关闭。")
        elif not snapshot.latest_source:
            self.translation_status_var.set("译文随原文更新。")
        elif self._last_metrics.get("translation_phase") not in {"error", "overloaded"}:
            if snapshot.detected_language == snapshot.target_language:
                self.translation_status_var.set("当前原文与目标语言相同，无需翻译。")
            else:
                self.translation_status_var.set({
                    "waiting": "正在翻译当前内容…",
                    "draft": "译文草稿 · 会继续修订",
                    "updating": "原文已更新，正在更新译文…",
                    "final": "本段译文已定稿",
                }[snapshot.latest_translation_state])
        self._apply_translation_visibility(snapshot.translation_visible)
        self.overlay.set_translation_visible(snapshot.translation_visible)
        self.overlay.set_translation_context(
            snapshot.source_language or snapshot.detected_language, snapshot.target_language,
            automatic=snapshot.source_language is None,
            translation_activity=snapshot.translation_activity,
        )
        self.overlay.set_entries(snapshot.entries, snapshot.revision_modes, snapshot.translation_pending,
                                 snapshot.translation_states)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._exit_requested = True
        for token in getattr(self, "_label_wrap_jobs", {}).values():
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        getattr(self, "_label_wrap_jobs", {}).clear()
        if getattr(self, "overlay", None) is getattr(self, "_mouse_passthrough_owner", None):
            self.overlay.on_mouse_passthrough_changed = None
        for token in getattr(self, "_parameter_focus_jobs", set()).copy():
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        getattr(self, "_parameter_focus_jobs", set()).clear()
        for draft in getattr(self, "_parameter_drafts", {}).values():
            draft.cancel()
        if self._overlay_focus_after_id is not None:
            self.root.after_cancel(self._overlay_focus_after_id)
            self._overlay_focus_after_id = None
        if getattr(self, "_tray", None):
            self._tray.close()
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
            self._live_update_after_id = None
        if self._drain_after_id is not None:
            self.root.after_cancel(self._drain_after_id)
            self._drain_after_id = None
        if self._theme_poll_id is not None:
            self.root.after_cancel(self._theme_poll_id)
            self._theme_poll_id = None
        if self.pipeline:
            self.root.withdraw()
            if not self._controller.shutdown():
                logging.error("退出时后台流水线尚未释放；继续等待实际退出")
                self._closing = False
                self.root.after(250, self.close)
                return
            self.pipeline = None
        self._save_settings()
        if self._model_manager_window is not None:
            self._model_manager_window.close()
        self.overlay.close()
        self.root.destroy()


def argument_parser():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · {APP_DESCRIPTION}")
    parser.add_argument("--no-autostart", action="store_true", help="本次启动不开始识别，不改变保存的启动选项")
    add_trace_arguments(parser)
    parser.add_argument("--quit-after", type=float, help="本次诊断运行若干秒后退出；不改变保存的启动设置")
    return parser


def main() -> None:
    parser = argument_parser()
    args = parser.parse_args()
    if args.quit_after is not None and not 0 < args.quit_after <= 86400:
        parser.error("quit-after 必须在0到86400秒之间")
    log_dir = application_data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s pid=%(process)d %(message)s",
                        handlers=[保留日志处理器(log_dir / "app.log")])
    logging.info("应用启动：名称=%s，版本=%s，可执行文件=%s，构建=%s", APP_NAME, app_version(), sys.executable, build_info())
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    root = tk.Tk()
    app = Application(root, autostart=False if args.no_autostart else None)
    if args.quit_after is not None:
        root.after(round(args.quit_after * 1000), app.close)
    root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
