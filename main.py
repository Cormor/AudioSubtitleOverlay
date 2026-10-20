"""Windows 音频识别翻译悬浮窗应用入口。"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import queue
import sys
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from brand import APP_NAME, APP_DESCRIPTION, APP_ID, asset_path, app_version, build_info
from tray_control import TrayController
from recognition_profiles import PROFILES, profile_values
from audio_capture import SystemAudioCapture
from process_audio_capture import PROCESS_AUDIO_DEVICE
from display_module import DisplayEvent, DisplayModule
from subtitle_events import SourceUpdate
from languages import (
    language_code,
    language_name,
    source_language_names,
    target_language_names,
)
from model_manager import ModelManagerWindow
from overlay import OverlayWindow
from pipeline import LivePipeline, PipelineOptions
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
        self._events = queue.SimpleQueue()
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
        self._model_manager_window: ModelManagerWindow | None = None
        self._live_update_after_id = None
        self._drain_after_id = None

        self.root.title(f"{APP_NAME} · {APP_DESCRIPTION}")
        try:
            self.root.iconbitmap(str(asset_path("icon.ico")))
        except tk.TclError:
            pass
        # 首次打开尽量多展示内容；屏幕较小时由滚动区域承载剩余内容。
        # 预留标题栏和任务栏，避免高 DPI 下窗口被撑出屏幕底部。
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        usable_height = max(520, int(screen_height * 0.82))
        initial_width = max(760, min(920, screen_width - 80))
        initial_height = max(600, min(780, usable_height))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(760, 600)
        # 关设置窗只隐藏，不退出；退出走悬浮窗菜单「退出程序」。
        self.root.protocol("WM_DELETE_WINDOW", self._hide_settings)
        self._create_variables()
        self._build_ui()
        self.overlay = OverlayWindow(
            self.root,
            self.settings,
            self._show_settings,
            self.close,
        )
        self.overlay.on_presented = self._display_presented
        self._refresh_devices()
        self._apply_overlay_settings()
        self._on_language_changed()
        # 启动后立即显示悬浮窗，使识别结果始终有可见的承载窗口。
        self._show_overlay()
        # 托盘操作只投递主线程消息；启动识别由用户或显式启动选项决定。
        self._tray = TrayController(
            self._post, show_settings=self._show_settings, show_overlay=self._show_overlay,
            hide_overlay=self.overlay.hide, start=self._start, stop=self._stop, close=self.close,
        ) if tray else None
        if self._tray:
            self._tray.start()
        self.root.bind("<Unmap>", self._on_settings_unmap, add="+")
        self.root.bind("<Control-Return>", self._toggle_recognition)
        self.root.bind("<Control-q>", lambda _: self.close())
        self.root.bind("<Escape>", lambda _: self._hide_settings())
        if self.settings.auto_start if autostart is None else autostart:
            self.root.after_idle(self._start)
        if self.settings.start_minimized:
            self.root.after(200, self._hide_settings)
        self._drain_after_id = self.root.after(10, self._drain_events)

    def _create_variables(self) -> None:
        self.source_var = tk.StringVar(value=self.settings.source_language)
        self.target_var = tk.StringVar(value=self.settings.target_language)
        self.model_var = tk.StringVar(value=self.settings.recognition_model)
        self.device_var = tk.StringVar(value=self.settings.recognition_device)
        self.beam_size_var = tk.StringVar(value=str(self.settings.recognition_beam_size))
        self.context_var = tk.StringVar(
            value="启用" if self.settings.recognition_condition_on_previous_text else "关闭"
        )
        temperature_value = self.settings.recognition_temperature_schedule
        self.temperature_var = tk.StringVar(
            value=_TEMPERATURE_LABELS.get(temperature_value, _TEMPERATURE_LABELS["0"])
        )
        self.recognition_vars = {
            name: tk.StringVar(value=str(getattr(self.settings, "recognition_" + name)))
            for name in RECOGNITION_NUMERIC_OPTIONS
        }
        self.profile_var = tk.StringVar(value="均衡 · small")
        self.profile_hint_var = tk.StringVar()
        self.current_profile_var = tk.StringVar()
        self.profile_model_var = tk.BooleanVar(value=True)
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
        self.status_var = tk.StringVar(value="准备就绪")
        self.source_display_var = tk.StringVar(value="等待识别")
        self.translation_display_var = tk.StringVar(value="等待翻译")

    def _update_audio_selection(self, *_args) -> None:
        """选择项变化只更新选择状态，不冒充已打开的采集设备。"""
        self.audio_selection_var.set(f"已选择：{self.audio_device_var.get() or '未选择'}")

    def _scroll_page(self, notebook, title):
        page = ttk.Frame(notebook)
        notebook.add(page, text=title)
        canvas = tk.Canvas(page, highlightthickness=0, background="#eef3f9")
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=bar.set)
        content = ttk.Frame(canvas, padding=12)
        slot = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(slot, width=e.width))
        self._scroll_pages.append(canvas)
        return content

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground="#142c43")
        style.configure("TFrame", background="#eef3f9")
        style.configure("TLabel", background="#eef3f9")
        style.configure("TLabelframe", background="#eef3f9", bordercolor="#cbd9e6")
        style.configure("TLabelframe.Label", background="#eef3f9", foreground="#184968", font=("Microsoft YaHei UI",11,"bold"))
        style.configure("TButton", padding=(12, 8), background="#e0eaf4", borderwidth=0)
        style.map("TButton", background=[("active", "#cbdfee")])
        style.configure("Primary.TButton", background="#087eaa", foreground="white")
        style.map("Primary.TButton", background=[("active", "#06688e"), ("disabled", "#b9cbd6")])
        style.configure("TCheckbutton", background="#eef3f9", padding=4)
        style.configure("TNotebook", background="#eef3f9", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 10))
        style.map("TNotebook.Tab", background=[("selected", "#d7eff6")], foreground=[("selected", "#055b79")])
        style.configure("Hero.TFrame", background="#10243a")
        style.configure("Hero.TLabel", background="#10243a", foreground="#f1f7ff")
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        hero = ttk.Frame(outer, style="Hero.TFrame", padding=(18, 12))
        hero.pack(fill="x", pady=(0, 12))
        self._brand_image = tk.PhotoImage(file=str(asset_path("icon.png"))).subsample(4, 4)
        ttk.Label(hero, image=self._brand_image, style="Hero.TLabel").pack(side="left", padx=(0, 14))
        names = ttk.Frame(hero, style="Hero.TFrame")
        names.pack(side="left")
        ttk.Label(names, text=APP_NAME, font=("Microsoft YaHei UI",22,"bold"), style="Hero.TLabel").pack(anchor="w")
        ttk.Label(names, text=APP_DESCRIPTION, style="Hero.TLabel").pack(anchor="w")
        ttk.Label(hero, text=f"v{app_version()}", style="Hero.TLabel").pack(side="right")
        action_frame = ttk.Frame(outer)
        action_frame.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(action_frame, text="开始识别", command=self._start, style="Primary.TButton")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            action_frame,
            text="停止识别",
            command=self._stop,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(action_frame, text="显示悬浮窗", command=self._show_overlay).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(action_frame, text="模型管理", command=self._show_model_manager).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(action_frame, text="收进托盘", command=self._hide_settings).pack(side="right")

        ttk.Label(outer, textvariable=self.status_var, foreground="#075d80", wraplength=840).pack(side="bottom", anchor="w", fill="x", pady=(10, 0))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self._scroll_pages = []
        container = self._scroll_page(notebook, "识别")
        appearance = self._scroll_page(notebook, "字幕外观")
        advanced = self._scroll_page(notebook, "高级")
        self._scroll_canvas = self._scroll_pages[0]
        model_details = ttk.LabelFrame(advanced, text="本地模型与 Beta 音频来源", padding=12)
        model_details.columnconfigure(1, weight=1)
        self.root.bind_all("<MouseWheel>", self._on_settings_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_settings_scroll_button, add="+")
        self.root.bind_all("<Button-5>", self._on_settings_scroll_button, add="+")
        ttk.Label(container, textvariable=self.audio_default_var).pack(anchor="w")
        ttk.Label(container, textvariable=self.audio_selection_var).pack(anchor="w", pady=(0, 6))
        audio_frame = ttk.LabelFrame(container, text="音频与识别", padding=12)
        audio_frame.pack(fill="x", pady=(0, 10))
        self.audio_combo = self._add_labeled_combo(
            audio_frame,
            0,
            "音频来源",
            self.audio_device_var,
            [],
            width=48,
            readonly=True,
        )
        ttk.Button(audio_frame, text="刷新设备", command=self._refresh_devices).grid(
            row=0, column=2, padx=(8, 0), sticky="w"
        )
        self._add_labeled_combo(
            audio_frame,
            1,
            "源语言",
            self.source_var,
            source_language_names(),
            width=20,
        )
        self._add_labeled_combo(
            audio_frame,
            2,
            "目标语言",
            self.target_var,
            target_language_names(),
            width=20,
        )
        self._add_labeled_combo(
            audio_frame,
            3,
            "模型大小",
            self.model_var,
            ["tiny", "base", "small", "medium", "large-v3"],
            width=20,
        )
        self._add_labeled_combo(
            audio_frame,
            4,
            "计算设备",
            self.device_var,
            ["自动（优先GPU）", "CPU（int8）", "NVIDIA GPU（float16）"],
            width=28,
        )
        ttk.Label(model_details, text="本地模型目录（可选）").grid(
            row=5, column=0, padx=(0, 8), pady=5, sticky="w"
        )
        self.model_path_entry = ttk.Entry(model_details, textvariable=self.model_path_var, width=52)
        self.model_path_entry.grid(
            row=5, column=1, columnspan=2, pady=5, sticky="ew"
        )
        self.model_path_entry.bind("<Return>", self._queue_live_configuration)
        self.model_path_entry.bind("<FocusOut>", self._queue_live_configuration)
        ttk.Label(
            model_details,
            text="可在“模型管理”中下载或查看本地模型。",
            foreground="#5c6773", wraplength=650,
        ).grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Label(
            model_details,
            text="Beta 会尝试跟踪当前有声音的应用；需要 Windows 10 build 20348 或更高版本，部分应用可能不支持。",
            foreground="#5c6773", wraplength=650,
        ).grid(row=7, column=1, columnspan=2, sticky="w", pady=(2, 0))
        audio_frame.columnconfigure(1, weight=1)

        translation_frame = ttk.LabelFrame(container, text="翻译", padding=12)
        translation_frame.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(translation_frame, text="启用翻译（同语言自动停用）",
                        variable=self.translation_enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self._add_labeled_combo(
            translation_frame,
            1,
            "翻译后端",
            self.translation_var,
            ["本地优先，失败转在线", "仅使用本地翻译", "仅使用免费在线 MyMemory"],
            width=32,
        )
        ttk.Label(
            translation_frame,
            text="本地翻译首次使用时会下载所需语言模型；下载后可离线使用。在线后端会把识别文本发送到公共接口。",
            foreground="#5c6773",
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 0))

        preview_frame = ttk.LabelFrame(container, text="最近结果", padding=12)
        preview_frame.pack(fill="both", expand=True)
        ttk.Label(
            preview_frame,
            textvariable=self.source_display_var,
            wraplength=740,
            justify="left",
        ).pack(anchor="w", fill="x")
        self.translation_preview = ttk.Label(
            preview_frame,
            textvariable=self.translation_display_var,
            wraplength=740,
            justify="left",
        )
        self.translation_preview.pack(anchor="w", fill="x", pady=(8, 0))
        self.preview_separator = ttk.Separator(preview_frame)
        self.preview_separator.pack(fill="x", pady=10)
        overlay_frame = ttk.LabelFrame(appearance, text="悬浮窗外观", padding=12)
        overlay_frame.pack(fill="x", pady=(0, 10))
        self._build_overlay_controls(overlay_frame)

        profile = ttk.LabelFrame(container, text="推荐组合 · 从快到准", padding=12)
        profile.pack(fill="x", pady=(0,10), before=translation_frame)
        picker = ttk.Frame(profile)
        picker.pack(fill="x")
        ttk.Combobox(picker, textvariable=self.profile_var, values=list(PROFILES), state="readonly", width=28).pack(side="left")
        ttk.Button(picker, text="应用组合", command=self._apply_recognition_profile).pack(side="left", padx=8)
        ttk.Label(profile, textvariable=self.profile_hint_var, wraplength=680).pack(anchor="w", pady=(8,0))
        ttk.Checkbutton(profile, text="同时切换模型（清空自定义模型目录；缺少模型时可能下载）", variable=self.profile_model_var).pack(anchor="w", pady=(4,0))
        ttk.Label(profile, textvariable=self.current_profile_var, foreground="#155575", wraplength=680).pack(anchor="w", pady=(4,0))
        ttk.Label(profile, text="仅选择不会应用；设备、语言和翻译设置保持不变。各档是调参起点，不保证越大越准。", wraplength=680).pack(anchor="w", pady=(4,0))
        self.profile_var.trace_add("write", self._update_profile_hint)
        self._update_profile_hint()
        self._update_current_profile()
        startup = ttk.LabelFrame(advanced, text="启动与托盘", padding=12)
        startup.pack(fill="x", pady=(0,10))
        ttk.Checkbutton(startup, text="启动后自动开始识别", variable=self.auto_start_var).pack(anchor="w")
        ttk.Checkbutton(startup, text="启动后收进右下角托盘", variable=self.start_minimized_var).pack(anchor="w")
        ttk.Label(startup, text="关闭或最小化收进托盘。Ctrl+Enter 开始／停止，Esc 收进托盘，Ctrl+Q 退出。", wraplength=680).pack(anchor="w", pady=(6,0))
        quality_frame = ttk.LabelFrame(advanced, text="识别质量", padding=12)
        quality_frame.pack(fill="x", pady=(0, 10))
        self._add_labeled_combo(
            quality_frame,
            0,
            "搜索范围",
            self.beam_size_var,
            [str(value) for value in RECOGNITION_BEAM_SIZES],
            width=12,
        )
        ttk.Label(
            quality_frame,
            text="数值越大通常越准，但推理更慢。",
            foreground="#5c6773",
        ).grid(row=0, column=2, padx=(10, 0), pady=5, sticky="w")
        self._add_labeled_combo(
            quality_frame,
            1,
            "参考前文",
            self.context_var,
            ["关闭", "启用"],
            width=12,
        )
        ttk.Label(
            quality_frame,
            text="结合当前音频窗口内的前文；出现重复时可关闭。",
            foreground="#5c6773",
        ).grid(row=1, column=2, padx=(10, 0), pady=5, sticky="w")
        self._add_labeled_combo(
            quality_frame,
            2,
            "解码回退",
            self.temperature_var,
            list(_TEMPERATURE_LABELS.values()),
            width=28,
        )
        ttk.Label(
            quality_frame,
            text="遇到低概率或重复结果时再尝试备用温度，可能增加耗时。",
            foreground="#5c6773",
        ).grid(row=2, column=2, padx=(10, 0), pady=5, sticky="w")
        quality_frame.columnconfigure(1, weight=1)

        # 参数说明另起一行，缩窄窗口时不挤出卡片。
        for child in quality_frame.winfo_children():
            info = child.grid_info()
            row, column = int(info["row"]), int(info["column"])
            if column == 2:
                child.configure(wraplength=620)
                child.grid_configure(row=row * 2 + 1, column=1, columnspan=2, padx=0, pady=(0,10))
            else:
                child.grid_configure(row=row * 2)
        tuning = ttk.LabelFrame(advanced, text="更多模型参数", padding=12)
        tuning.pack(fill="x", pady=(0,10))
        descriptions = {
            "window_seconds": ("最大音频窗口（秒）", "保留的语音上下文上限；越长越费时。已确认的停顿仍会分窗。"),
            "update_interval": ("最短更新间隔（秒）", "更短会更频繁尝试识别，不保证能按时出字；CPU 最短仍为 0.5 秒。"),
            "repetition_penalty": ("重复惩罚", "1 为不额外惩罚；调高可能减少重复，也可能漏掉正常重复用词。"),
            "no_repeat_ngram_size": ("禁止重复词元组", "0 为关闭；非零可能抑制模型重复，也可能删掉本来应该重复的内容。"),
            "no_speech_threshold": ("无语音判定阈值", "越低越容易跳过疑似无语音片段；过低可能漏掉轻声。"),
            "vad_silence_ms": ("语音分段停顿（毫秒）", "检测到多长停顿时切分语音；太短可能切碎说话，不是等待整句的时间。"),
        }
        for row, (name, choices) in enumerate(RECOGNITION_NUMERIC_OPTIONS.items()):
            label, hint = descriptions[name]
            self._add_labeled_combo(tuning, row*2, label, self.recognition_vars[name], [str(value) for value in choices], width=18)
            ttk.Label(tuning, text=hint, foreground="#5c6773", wraplength=620).grid(row=row*2+1, column=1, columnspan=2, sticky="w", pady=(0,10))
        tuning.columnconfigure(1, weight=1)
        model_details.pack(fill="x", pady=(0,10))
        for variable in self.recognition_vars.values():
            variable.trace_add("write", self._queue_live_configuration)
            variable.trace_add("write", self._schedule_settings_save)
        for variable in (*self.recognition_vars.values(), self.model_var, self.beam_size_var,
                         self.context_var, self.temperature_var, self.model_path_var):
            variable.trace_add("write", self._update_current_profile)

        self.translation_enabled_var.trace_add("write", self._on_translation_enabled_changed)
        for variable in (self.source_var, self.target_var):
            variable.trace_add("write", self._on_language_changed)
        for variable in (
            self.audio_device_var,
            self.model_var,
            self.device_var,
            self.beam_size_var,
            self.context_var,
            self.temperature_var,
            self.translation_var,
        ):
            variable.trace_add("write", self._queue_live_configuration)
        # 任何设置变化都自动落盘，进程被外部结束也能保住配置。
        for variable in (
            self.source_var,
            self.target_var,
            self.model_var,
            self.device_var,
            self.beam_size_var,
            self.context_var,
            self.temperature_var,
            self.model_path_var,
            self.audio_device_var,
            self.translation_enabled_var,
            self.translation_var,
            self.auto_start_var,
            self.start_minimized_var,
        ):
            variable.trace_add("write", self._schedule_settings_save)

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
            widget = getattr(widget, "master", None)
        return None

    def _in_scroll_content(self, widget):
        return self._scroll_canvas_for(widget) is not None

    def _on_settings_mousewheel(self, event):
        canvas = self._scroll_canvas_for(event.widget)
        if canvas is None:
            return
        if event.widget.winfo_class() in {"TCombobox", "TSpinbox", "Entry", "TScale"}:
            return
        delta = int(event.delta / 120)
        canvas.yview_scroll(-delta if delta else (-1 if event.delta < 0 else 1), "units")
        return "break"

    def _on_settings_scroll_button(self, event):
        canvas = self._scroll_canvas_for(event.widget)
        if canvas is not None:
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
            return "break"

    def _toggle_recognition(self, _event=None):
        self._stop() if self.pipeline else self._start()
        return "break"

    def _update_profile_hint(self, *_):
        profile = PROFILES[self.profile_var.get()]
        self.profile_hint_var.set(f"{profile['description']} 搜索 {profile['beam']}，窗口 {profile['window']} 秒，最短间隔 {profile['interval']} 秒。")

    def _update_current_profile(self, *_):
        matched = "自定义"
        for name in PROFILES:
            values = profile_values(name)
            if (not self.model_path_var.get().strip()
                and self.model_var.get() == values["model"]
                and self.beam_size_var.get() == str(values["beam"])
                and self.context_var.get() == "关闭"
                and self.temperature_var.get() == _TEMPERATURE_LABELS[values["temperature"]]
                and all(float(self.recognition_vars[key].get()) == values[key] for key in self.recognition_vars)):
                matched = name
                break
        self.current_profile_var.set(f"当前参数：{matched}；模型 {self.model_var.get()}，搜索 {self.beam_size_var.get()}")

    def _apply_recognition_profile(self):
        values = profile_values(self.profile_var.get())
        if self.profile_model_var.get():
            self.model_path_var.set("")
            self.model_var.set(values["model"])
        self.beam_size_var.set(str(values["beam"]))
        self.temperature_var.set(_TEMPERATURE_LABELS[values["temperature"]])
        self.context_var.set("关闭")
        for key, variable in self.recognition_vars.items():
            variable.set(str(values[key]))
        self._queue_live_configuration()
        self._save_settings()
        self._update_current_profile()
        self._set_status("已应用推荐组合；模型已切换。" if self.profile_model_var.get() else "已应用解码参数；当前模型和自定义目录保持不变。")

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
        readonly=False,
    ):
        ttk.Label(parent, text=label).grid(row=row, column=0, padx=(0, 8), pady=5, sticky="w")
        combo = ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            width=width,
            state="readonly" if values or readonly else "normal",
        )
        combo.grid(row=row, column=1, columnspan=1, pady=5, sticky="ew")
        return combo

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
                "准备就绪" if devices
                else "普通音频设备未读取；仍可尝试 Beta 自动跟踪模式。"
            )
        except Exception as error:
            self.audio_combo.configure(state="readonly")
            self.audio_default_var.set(f"Windows 默认播放：读取失败（{error}）")
            self._set_status(f"音频设备不可用：{error}。请在 Windows Python 环境运行并安装 soundcard。")

    def _start(self) -> None:
        if self.pipeline:
            return
        self._save_settings()
        options = self._pipeline_options_from_ui()
        if options is None:
            return
        source = options.source_language
        target = options.target_language
        self._pending_presentations.clear()
        self._display.reset(source, target)
        self.overlay.reset_content()
        self._flush_display()
        # 每次启动建立新的会话；旧会话的排队回调不更新新会话。
        self._session += 1
        session = self._session
        def post(callback, *args):
            self._post(self._dispatch_session, session, callback, *args)
        self.pipeline = LivePipeline(
            options,
            on_status=lambda text: post(self._set_status, text),
            on_source=lambda sequence, text, code, block_id: post(
                self._show_source, sequence, text, code, block_id
            ),
            on_translation=lambda sequence, text, code: post(
                self._show_translation, sequence, text, code
            ),
            on_error=lambda text: post(self._show_error, text),
            on_stopped=lambda: post(self._pipeline_stopped),
            on_update=lambda update: post(self._show_timed_source, update),
            on_reset=lambda: post(self._reset_runtime_display),
        )
        self.pipeline.start()
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
        if pipeline:
            self._set_status("正在停止识别……")
            pipeline.stop()

    def _pipeline_stopped(self) -> None:
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
            self._live_update_after_id = None
        self.pipeline = None
        if getattr(self, "_tray", None):
            self._tray.set_running(False)
        if not self._closing:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self._set_status("识别已停止。")

    def _apply_display_event(self, event: DisplayEvent) -> None:
        """把一个后台结果交给显示模块；悬浮窗更新统一延迟到批次结束。"""
        if not self._display.apply(event):
            return
        if not self._in_event_drain:
            self._flush_display()

    def _show_timed_source(self, update: SourceUpdate) -> None:
        """保留各段最新结果的时间记录，不把被覆盖的中间版本算作上屏。"""
        self._pending_presentations[update.block_id] = update
        self._show_source(update.sequence, update.text, update.language, update.block_id)

    def _display_presented(self, presented_at: float, source_indices: set[int]) -> None:
        """仅记录实际绘制的原文段落；提交完成不等同于显示器扫描完成。"""
        if not self._pending_presentations:
            return
        block_ids = list(self._history.blocks)
        for index in source_indices:
            if index >= len(block_ids):
                continue
            block_id = block_ids[index]
            update = self._pending_presentations.pop(block_id, None)
            if update is None or self._history.blocks[block_id].sequence != update.sequence:
                continue
            measurements = update.measurements(presented_at)
            logging.info(
                "字幕窗口已提交：序号=%s，段落=%s，音频终点=%.3f秒，阅读模式=%s，耗时=%s",
                update.sequence, block_id, update.audio_end,
                "实时" if getattr(getattr(self, "overlay", None), "_follow_tail", True) else "历史", measurements,
            )
        # 阅读停留期间末尾可能尚未进入视口，不能拿旧的可见前缀冒充新结果。
        # 每段只保留最新版本；离开有界历史的段落才丢弃记录。
        for block_id in set(self._pending_presentations) - set(self._history.blocks):
            self._pending_presentations.pop(block_id, None)

    def _show_source(
        self,
        sequence: int,
        text: str,
        language: str | None,
        block_id: int | None = None,
    ) -> None:
        self._apply_display_event(
            DisplayEvent("source", sequence, text, language, block_id)
        )

    def _show_preview(self, sequence: int, text: str, language: str | None, block_id: int) -> None:
        """显示短窗口即时草稿，完整识别结果到达后覆盖当前文字。"""
        self._apply_display_event(
            DisplayEvent("preview", sequence, text, language, block_id)
        )

    def _show_translation(self, sequence: int, text: str, language: str) -> None:
        self._apply_display_event(
            DisplayEvent("translation", sequence, text, language)
        )

    def _show_error(self, text: str) -> None:
        self._set_status(text)

    def _on_translation_enabled_changed(self, *_args) -> None:
        self._display.set_translation_enabled(self.translation_enabled_var.get())
        self._flush_display()
        self._queue_live_configuration()

    def _on_language_changed(self, *_args) -> None:
        self._update_translation_visibility()
        self._queue_live_configuration()

    def _pipeline_options_from_ui(self) -> PipelineOptions | None:
        """读取并校验当前控件值，供启动和运行中切换共用。"""
        source = language_code(self.source_var.get())
        target = language_code(self.target_var.get(), allow_auto=False)
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
        try:
            beam_size = int(self.beam_size_var.get())
        except ValueError:
            beam_size = RECOGNITION_BEAM_SIZES[0]
        if beam_size not in RECOGNITION_BEAM_SIZES:
            beam_size = RECOGNITION_BEAM_SIZES[0]
        return PipelineOptions(
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
            temperature_schedule=_TEMPERATURE_VALUES_BY_LABEL.get(
                self.temperature_var.get(), "0"
            ),
            **{name: type(choices[0])(self.recognition_vars[name].get())
               for name, choices in RECOGNITION_NUMERIC_OPTIONS.items()},
        )

    def _queue_live_configuration(self, *_args) -> None:
        """把连续的界面修改合并，交给识别线程应用。"""
        if self.pipeline is None or self._closing:
            return
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
        self._live_update_after_id = self.root.after(120, self._apply_live_configuration)

    def _apply_live_configuration(self) -> None:
        self._live_update_after_id = None
        pipeline = self.pipeline
        if pipeline is None:
            return
        options = self._pipeline_options_from_ui()
        if options is None:
            return
        if pipeline.update_options(options):
            self._set_status("正在应用新的设置……")

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
        self.overlay.show()

    def _hide_settings(self) -> None:
        """关闭设置窗＝隐藏；识别和悬浮窗继续运行。"""
        self._schedule_settings_save()
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
        self.root.deiconify()
        self.root.lift()

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

    def _save_settings(self) -> None:
        self.settings.source_language = self.source_var.get()
        self.settings.target_language = self.target_var.get()
        self.settings.recognition_model = self.model_var.get()
        self.settings.recognition_device = self.device_var.get()
        try:
            self.settings.recognition_beam_size = int(self.beam_size_var.get())
        except ValueError:
            self.settings.recognition_beam_size = RECOGNITION_BEAM_SIZES[0]
        self.settings.recognition_condition_on_previous_text = self.context_var.get() == "启用"
        self.settings.recognition_temperature_schedule = _TEMPERATURE_VALUES_BY_LABEL.get(
            self.temperature_var.get(), "0"
        )
        for name, choices in RECOGNITION_NUMERIC_OPTIONS.items():
            setattr(self.settings, "recognition_" + name, type(choices[0])(self.recognition_vars[name].get()))
        self.settings.model_path = self.model_path_var.get()
        self.settings.loopback_device = self.audio_device_var.get()
        self.settings.auto_start = self.auto_start_var.get()
        self.settings.start_minimized = self.start_minimized_var.get()
        self.settings.translation_enabled = self.translation_enabled_var.get()
        self.settings.translation_backend = self.translation_var.get()
        if hasattr(self, "overlay"):
            self.overlay.persist_geometry()
        self.settings.save()

    def _schedule_settings_save(self, *_args) -> None:
        """设置变更后防抖保存，避免每次拖动都写盘。"""
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
        if self._closing:
            return
        try:
            self._save_settings()
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _post(self, callback, *args) -> None:
        if not self._closing:
            self._events.put((callback, args))

    def _dispatch_session(self, session, callback, *args) -> None:
        if session == self._session:
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
                callback(*args)
        finally:
            self._in_event_drain = False
            # 一个定时批次只提交一次悬浮窗快照，避免识别频率直接决定重绘频率。
            self._flush_display()
            if not self._closing:
                self._drain_after_id = self.root.after(10, self._drain_events)

    def _flush_display(self) -> None:
        """将显示模块的脏状态一次性提交给主窗口和悬浮窗。"""
        snapshot = self._display.take_snapshot()
        if snapshot is None:
            return
        self.source_display_var.set(snapshot.latest_source or "等待识别")
        self.translation_display_var.set(snapshot.latest_translation or "等待翻译")
        self._apply_translation_visibility(snapshot.translation_visible)
        self.overlay.set_translation_visible(snapshot.translation_visible)
        self.overlay.set_translation_context(
            snapshot.source_language or snapshot.detected_language, snapshot.target_language,
            automatic=snapshot.source_language is None,
            translation_activity=snapshot.translation_activity,
        )
        self.overlay.set_entries(snapshot.entries, snapshot.revision_modes, snapshot.translation_pending)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if getattr(self, "_tray", None):
            self._tray.close()
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
            self._live_update_after_id = None
        if self._drain_after_id is not None:
            self.root.after_cancel(self._drain_after_id)
            self._drain_after_id = None
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None
        self._save_settings()
        if self._model_manager_window is not None:
            self._model_manager_window.close()
        self.overlay.close()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · {APP_DESCRIPTION}")
    parser.add_argument("--no-autostart", action="store_true", help="本次启动不开始识别，不改变保存的启动选项")
    args = parser.parse_args()
    log_dir = application_data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
    logging.info("应用启动：名称=%s，版本=%s，可执行文件=%s，构建=%s", APP_NAME, app_version(), sys.executable, build_info())
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    root = tk.Tk()
    Application(root, autostart=False if args.no_autostart else None)
    root.mainloop()


if __name__ == "__main__":
    main()
