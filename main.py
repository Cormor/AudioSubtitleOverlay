"""Windows 音频识别翻译悬浮窗应用入口。"""

from __future__ import annotations

import argparse
import logging
from log_retention import 保留日志处理器
import queue
import sys
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

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
from model_manager import MODEL_DEFINITIONS, ModelManagerWindow, validate_model_language
from runtime_trace import get_runtime_trace, add_trace_arguments
from ui_guidance import (MODEL_FILTERS, compact_status, empty_transcript_hint, finite_nonnegative,
                         model_group, model_guidance, model_label, runtime_view)
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
        self._trace_versions: dict[int, SourceUpdate] = {}
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
        self._create_variables()
        self._build_ui()
        self.root.report_callback_exception = self._report_ui_exception
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
        self.root.bind("<FocusIn>", self._queue_overlay_focus, add="+")
        self.root.bind("<FocusOut>", self._queue_overlay_focus, add="+")
        self._queue_overlay_focus()
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
        self.transcript_notice_var = tk.StringVar(value="最近 64 段可复制或导出；实时草稿会继续修订。")
        self.model_hint_var = tk.StringVar()
        self.translation_hint_var = tk.StringVar()
        self.model_display_var = tk.StringVar()
        self._model_labels = {
            item.key: model_label(item.key, item.display_name)
            for item in MODEL_DEFINITIONS
        }
        self._model_definitions = {item.key: item for item in MODEL_DEFINITIONS}
        self.source_display_var = tk.StringVar(value="等待识别")
        self.translation_display_var = tk.StringVar(value="等待翻译")

    def _update_audio_selection(self, *_args) -> None:
        """选择项变化只更新选择状态，不冒充已打开的采集设备。"""
        self.audio_selection_var.set(f"已选择：{self.audio_device_var.get() or '未选择'}")

    def _scroll_page(self, notebook, title):
        page = ttk.Frame(notebook)
        notebook.add(page, text=title)
        canvas = tk.Canvas(page, highlightthickness=0, background="#f3f6f8")
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
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground="#173247")
        style.configure("TFrame", background="#ffffff")
        style.configure("Surface.TFrame", background="#f3f6f8")
        style.configure("TLabel", background="#ffffff", foreground="#173247")
        style.configure("Muted.TLabel", foreground="#526776")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("State.TLabel", font=("Microsoft YaHei UI", 12, "bold"), foreground="#087b8b")
        style.configure("TLabelframe", background="#ffffff", bordercolor="#dde5eb", relief="solid")
        style.configure("TLabelframe.Label", background="#ffffff", foreground="#173247", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", padding=(10, 5), background="#eaf0f4", borderwidth=0)
        style.map("TButton", background=[("active", "#dbe7ed")])
        style.configure("Primary.TButton", background="#087f91", foreground="white", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#086b7b"), ("disabled", "#bacbd0")])
        style.configure("SelectedProfile.TButton", background="#087f91", foreground="white")
        style.map("SelectedProfile.TButton", background=[("active", "#086b7b")])
        style.configure("TCheckbutton", background="#ffffff", padding=4)
        style.configure("TNotebook", background="#f3f6f8", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(15, 7))
        style.configure("TCombobox", fieldbackground="#ffffff", background="#edf2f5", bordercolor="#d3dee6", padding=3)
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#edf2f5")],
                  foreground=[("disabled", "#526776"), ("readonly", "#173247")],
                  selectbackground=[("readonly", "#e0f2f5")], selectforeground=[("readonly", "#173247")])
        style.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("!selected", "#e8eef2")],
                  foreground=[("selected", "#087b8b"), ("!selected", "#526776")])
        style.configure("Input.Horizontal.TProgressbar", background="#0793a2", troughcolor="#e9f0f3", borderwidth=0)
        self.root.configure(background="#f3f6f8")
        outer = ttk.Frame(self.root, padding=10, style="Surface.TFrame")
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, padding=(10, 5))
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"  v{app_version()}", style="Muted.TLabel").pack(side="left")
        ttk.Button(header, text="收进托盘", command=self._hide_settings).pack(side="right")
        ttk.Button(header, text="暂藏字幕", command=lambda: self.overlay.hide()).pack(side="right", padx=6)
        ttk.Button(header, text="字幕窗", command=self._show_overlay).pack(side="right")

        runtime = ttk.Frame(outer, padding=(10, 7))
        runtime.pack(fill="x", pady=(0, 6))
        actions = ttk.Frame(runtime)
        actions.pack(fill="x")
        ttk.Label(actions, textvariable=self.runtime_state_var, style="State.TLabel").pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="right", padx=(6, 0))
        self.start_button = ttk.Button(actions, text="开始字幕", command=self._start, style="Primary.TButton")
        self.start_button.pack(side="right")
        ttk.Checkbutton(actions, text="运行详情", variable=self.runtime_details_var,
                        command=self._toggle_runtime_details).pack(side="right", padx=10)
        meters = ttk.Frame(runtime)
        meters.pack(fill="x", pady=(5, 0))
        meters.columnconfigure(2, weight=1)
        ttk.Progressbar(meters, variable=self.input_level_var, maximum=100, length=64,
                        style="Input.Horizontal.TProgressbar").grid(row=0, column=0, padx=(0, 6))
        ttk.Label(meters, textvariable=self.input_hint_var, style="Muted.TLabel").grid(row=0, column=1, sticky="w")
        self._speed_label = ttk.Label(meters, textvariable=self.runtime_speed_var, style="Muted.TLabel", wraplength=540)
        self._speed_label.grid(row=0, column=2, sticky="e", padx=(10, 0))
        self._guidance_label = ttk.Label(runtime, textvariable=self.runtime_guidance_var, style="Muted.TLabel", wraplength=850)
        self._guidance_label.pack(anchor="w", fill="x", pady=(4, 0))
        self._runtime_details_frame = details = ttk.Frame(runtime, padding=(0, 5))
        self._status_label = ttk.Label(details, textvariable=self.status_var, wraplength=850, style="Muted.TLabel")
        self._status_label.pack(anchor="w", fill="x")
        ttk.Label(details, textvariable=self.runtime_device_var, style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(details, textvariable=self.audio_default_var, style="Muted.TLabel", wraplength=850).pack(anchor="w")
        ttk.Label(details, textvariable=self.audio_selection_var, style="Muted.TLabel", wraplength=850).pack(anchor="w")
        ttk.Label(details, text="处理：最近一次模型处理耗时；积压：尚未处理的音频。两者均不是从声卡到屏幕的总延迟。",
                  style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(3, 0))
        if self.runtime_details_var.get():
            details.pack(fill="x")
        runtime.bind("<Configure>", self._resize_runtime)
        shortcuts = ttk.Frame(outer, style="Surface.TFrame")
        shortcuts.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Label(shortcuts, text="Ctrl+Enter 开始 / 停止    Esc 收进托盘    Ctrl+Q 退出",
                  background="#f3f6f8", foreground="#526776").pack(side="left")

        self.notebook = notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self._scroll_pages = []
        container = self._scroll_page(notebook, "实时字幕")
        appearance = self._scroll_page(notebook, "字幕外观")
        advanced = self._scroll_page(notebook, "高级设置")
        help_page = self._scroll_page(notebook, "开始使用 / 排查")
        self._scroll_canvas = self._scroll_pages[0]
        self.root.bind_all("<MouseWheel>", self._on_settings_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_settings_scroll_button, add="+")
        self.root.bind_all("<Button-5>", self._on_settings_scroll_button, add="+")

        audio_frame = ttk.LabelFrame(container, text="字幕设置", padding=(10, 7))
        audio_frame.pack(fill="x", pady=(0, 6))
        audio_frame.columnconfigure(1, weight=2)
        audio_frame.columnconfigure(3, weight=3)
        ttk.Label(audio_frame, text="音源").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.audio_combo = ttk.Combobox(audio_frame, textvariable=self.audio_device_var, state="readonly", width=42)
        self.audio_combo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Button(audio_frame, text="刷新", command=self._refresh_devices).grid(row=0, column=4, padx=(8, 0))
        ttk.Label(audio_frame, text="声音语言").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.source_combo = ttk.Combobox(audio_frame, textvariable=self.source_var, values=source_language_names(), state="readonly", width=13)
        self.source_combo.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(audio_frame, text="模型").grid(row=1, column=2, sticky="w", padx=(14, 8))
        self.model_combo = ttk.Combobox(audio_frame, textvariable=self.model_display_var, values=list(self._model_labels.values()), state="readonly", width=28)
        self.model_combo.grid(row=1, column=3, columnspan=2, sticky="ew", pady=3)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Label(audio_frame, text="运行设备").grid(row=2, column=0, sticky="w", padx=(0, 8))
        self.device_combo = ttk.Combobox(audio_frame, textvariable=self.device_var,
            values=["自动（优先GPU）", "CPU（int8）", "NVIDIA GPU（float16）", "NVIDIA GPU（int8_float16）"], state="readonly", width=18)
        self.device_combo.grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(audio_frame, text="筛选").grid(row=2, column=2, sticky="w", padx=(14, 8))
        model_filter = ttk.Combobox(audio_frame, textvariable=self.model_filter_var, values=MODEL_FILTERS, state="readonly", width=18)
        model_filter.grid(row=2, column=3, sticky="ew", pady=3)
        model_filter.bind("<<ComboboxSelected>>", self._filter_models)
        ttk.Button(audio_frame, text="管理", command=self._show_model_manager).grid(row=2, column=4, padx=(8, 0))
        ttk.Label(audio_frame, textvariable=self.model_hint_var, style="Muted.TLabel", wraplength=820, justify="left").grid(row=3, column=0, columnspan=5, sticky="w", pady=(5, 0))
        self.model_var.trace_add("write", self._update_model_description)
        self._update_model_description()

        translation_frame = ttk.LabelFrame(container, text="翻译", padding=(10, 6))
        translation_frame.pack(fill="x", pady=(0, 6))
        translation_frame.columnconfigure(2, weight=1)
        ttk.Checkbutton(translation_frame, text="显示译文", variable=self.translation_enabled_var).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.target_combo = ttk.Combobox(translation_frame, textvariable=self.target_var, values=target_language_names(), state="readonly", width=13)
        self.target_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.translation_combo = ttk.Combobox(translation_frame, textvariable=self.translation_var,
            values=["仅使用本地翻译", "本地优先，失败转在线", "仅使用免费在线 MyMemory"], state="readonly", width=28)
        self.translation_combo.grid(row=0, column=2, sticky="ew")
        ttk.Label(translation_frame, textvariable=self.translation_hint_var, foreground="#526776", wraplength=820).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))
        self.translation_var.trace_add("write", self._update_translation_notice)
        self._update_translation_notice()

        preview_frame = ttk.LabelFrame(container, text="最近字幕", padding=10)
        preview_frame.pack(fill="both", expand=True)
        self.source_preview = ttk.Label(preview_frame, textvariable=self.source_display_var, wraplength=740, justify="left", font=("Microsoft YaHei UI", 11))
        self.source_preview.pack(anchor="w", fill="x")
        self.translation_preview = ttk.Label(preview_frame, textvariable=self.translation_display_var, wraplength=740, justify="left", foreground="#087b8b")
        self.translation_preview.pack(anchor="w", fill="x", pady=(8, 0))
        self.preview_separator = ttk.Separator(preview_frame)
        self.preview_separator.pack(fill="x", pady=10)
        preview_actions = ttk.Frame(preview_frame)
        preview_actions.pack(fill="x")
        ttk.Button(preview_actions, text="显示字幕窗", command=self._show_overlay).pack(side="left")
        ttk.Button(preview_actions, text="复制最近字幕", command=self._copy_transcript).pack(side="left", padx=8)
        ttk.Button(preview_actions, text="导出最近字幕 TXT", command=self._export_transcript).pack(side="left")
        ttk.Label(preview_frame, textvariable=self.transcript_notice_var, style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(6, 0))
        preview_frame.bind("<Configure>", lambda e: self._resize_preview(e.width))
        overlay_frame = ttk.LabelFrame(appearance, text="字幕窗外观", padding=14)
        overlay_frame.pack(fill="x", pady=(0, 10))
        self._build_overlay_controls(overlay_frame)
        appearance_actions = ttk.Frame(appearance)
        appearance_actions.pack(anchor="w", pady=(0, 8))
        ttk.Button(appearance_actions, text="显示字幕窗", command=self._show_overlay).pack(side="left")
        ttk.Button(appearance_actions, text="找回字幕窗", command=lambda: self.overlay.restore_position()).pack(side="left", padx=8)

        profile = ttk.LabelFrame(advanced, text="识别参数预设 · 保留当前模型", padding=12)
        profile.pack(fill="x", pady=(0, 10))
        buttons = ttk.Frame(profile)
        buttons.pack(fill="x")
        self._profile_buttons = {}
        for column, name in enumerate(PROFILES):
            buttons.columnconfigure(column, weight=1, uniform="preset")
            button = ttk.Button(buttons, text=name, command=lambda selected=name: self._apply_recognition_profile(selected))
            button.grid(row=0, column=column, padx=(0, 6), sticky="ew")
            self._profile_buttons[name] = button
        ttk.Label(profile, textvariable=self.current_profile_var, foreground="#087b8b").pack(anchor="w", pady=(6, 0))
        self._profile_hint_label = ttk.Label(profile, textvariable=self.profile_hint_var, wraplength=680, style="Muted.TLabel")
        self._profile_hint_label.pack(anchor="w", fill="x")
        profile.bind("<Configure>", lambda e: self._profile_hint_label.configure(wraplength=max(200, e.width - 24)))
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


        model_details = ttk.LabelFrame(advanced, text="自定义模型与应用音频", padding=12)
        model_details.pack(fill="x", pady=(0, 10))
        model_details.columnconfigure(1, weight=1)
        ttk.Label(model_details, text="本地模型目录").grid(row=0, column=0, padx=(0, 8), pady=5, sticky="w")
        self.model_path_entry = ttk.Entry(model_details, textvariable=self.model_path_var, width=52)
        self.model_path_entry.grid(row=0, column=1, pady=5, sticky="ew")
        self.model_path_entry.bind("<Return>", self._queue_live_configuration)
        self.model_path_entry.bind("<FocusOut>", self._queue_live_configuration)
        ttk.Label(model_details, text="留空使用模型管理中的模型。自定义目录须符合所选模型的格式。", style="Muted.TLabel", wraplength=650).grid(row=1, column=1, sticky="w")
        ttk.Label(model_details, text="Beta 自动跟踪当前有声音的应用需要 Windows 10 build 20348 或更高版本，部分应用不支持；音源列表仍可选择普通系统播放。", style="Muted.TLabel", wraplength=650).grid(row=2, column=1, sticky="w", pady=(8, 0))
        self._build_help(help_page)
        for page in (container, appearance, advanced, help_page):
            self._bind_card_wrapping(page)
            self._protect_control_wheel(page)
        for variable in self.recognition_vars.values():
            variable.trace_add("write", self._queue_live_configuration)
            variable.trace_add("write", self._schedule_settings_save)
        for variable in (*self.recognition_vars.values(), self.beam_size_var,
                         self.context_var, self.temperature_var):
            variable.trace_add("write", self._update_current_profile)

        self.root.bind_all("<FocusIn>", self._reveal_focused_control, add="+")
        self.root.bind_all("<Prior>", self._keyboard_scroll, add="+")
        self.root.bind_all("<Next>", self._keyboard_scroll, add="+")
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
        for label in event.widget.winfo_children():
            if not isinstance(label, ttk.Label) or not float(label.cget("wraplength") or 0):
                continue
            info = label.grid_info()
            inset = 160 if info and int(info.get("column", 0)) > 0 else 40
            label.configure(wraplength=max(220, event.width - inset))

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
        self.model_combo.configure(values=[label for key, label in self._model_labels.items()
                                           if selected_group == MODEL_FILTERS[0] or model_group(key) == selected_group])
        self._update_model_description()

    def _update_model_description(self, *_args) -> None:
        key = self.model_var.get()
        self.model_display_var.set(self._model_labels.get(key, key))
        definition = self._model_definitions.get(key)
        cpu_only = bool(definition and definition.backend != "whisper")
        self.device_combo.configure(state="disabled" if cpu_only else "readonly")
        description = model_guidance(key) if definition else "当前保存的模型不在列表中；请打开管理确认模型。"
        selected_group = self.model_filter_var.get()
        if selected_group != MODEL_FILTERS[0] and model_group(key) != selected_group:
            description = "当前模型保留；不在筛选结果中，选“全部模型”可恢复列表。 " + description
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
                                 + " 运行中自动应用，无需手动停止再开始。")
        for name, button in getattr(self, "_profile_buttons", {}).items():
            button.configure(style="SelectedProfile.TButton" if name == matched else "TButton")

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
            self._set_status(f"已保存“{name}”参数预设；模型保持不变。")

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
                "音源已刷新，确认选择后即可开始字幕。" if devices and self.pipeline is None
                else "音源已刷新。当前识别会应用所选音源。" if devices
                else "没有读取到普通音频设备。请检查 Windows 播放设备后刷新，或尝试 Beta 应用音频。"
            )
        except Exception as error:
            self.audio_combo.configure(state="readonly")
            self.audio_default_var.set(f"Windows 默认播放：读取失败（{error}）")
            self._set_status(f"音频设备不可用：{error}。请在 Windows Python 环境运行并安装 soundcard。")

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
        def post(callback, *args):
            self._post(self._dispatch_session, session, callback, *args)
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
            on_metrics=lambda metrics: post(self._show_metrics, metrics),
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
        if pipeline and not self._stopping:
            self._stopping = True
            self._set_runtime_phase("stopping")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self._set_status("正在释放识别资源，请稍候……")
            pipeline.stop()

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
            return
        if not self._in_event_drain:
            self._flush_display()

    def _show_timed_source(self, update: SourceUpdate) -> None:
        """保留各段最新结果的时间记录，不把被覆盖的中间版本算作上屏。"""
        self._pending_presentations[update.block_id] = update
        if get_runtime_trace().enabled:
            self._trace_versions[update.block_id] = update
            while len(self._trace_versions) > 65:
                self._trace_versions.pop(next(iter(self._trace_versions)))
        if update.final:
            self._show_source(update.sequence, update.text, update.language, update.block_id)
        else:
            self._show_preview(update.sequence, update.text, update.language, update.block_id)

    def _display_presented(self, presented_at: float, source_indices: set[int]) -> None:
        """仅记录实际绘制的原文段落；提交完成不等同于显示器扫描完成。"""
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
        get_runtime_trace().record("translation", sequence=sequence, text=text, language=language)
        self._apply_display_event(
            DisplayEvent("translation", sequence, text, language)
        )

    def _show_error(self, text: str) -> None:
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
        self._flush_display()
        self._queue_live_configuration()

    def _on_language_changed(self, *_args) -> None:
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
        if self.pipeline is None or self._closing or getattr(self, "_stopping", False) or getattr(self, "_applying_profile", False):
            return
        if self._live_update_after_id is not None:
            self.root.after_cancel(self._live_update_after_id)
        self._live_update_after_id = self.root.after(120, self._apply_live_configuration)

    def _apply_live_configuration(self) -> None:
        self._live_update_after_id = None
        pipeline = self.pipeline
        if pipeline is None or getattr(self, "_stopping", False):
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

    def _hide_settings(self) -> None:
        """关闭设置窗＝隐藏；识别和悬浮窗继续运行。"""
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
        self.settings.show_runtime_details = self._runtime_details_preference
        self.settings.translation_enabled = self.translation_enabled_var.get()
        self.settings.translation_backend = self.translation_var.get()
        if hasattr(self, "overlay"):
            self.overlay.persist_geometry()
        self.settings.save()

    def _schedule_settings_save(self, *_args) -> None:
        """设置变更后防抖保存，避免每次拖动都写盘。"""
        if getattr(self, "_applying_profile", False):
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
        if self._closing:
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
        if not self._closing:
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
        self.source_display_var.set(snapshot.latest_source or "字幕会显示在这里。选择音源和语言，点击“开始字幕”。")
        self.translation_display_var.set(snapshot.latest_translation or "启用翻译后，译文会显示在原文下方。")
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
        if self.pipeline:
            self.pipeline.stop()
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
    return parser


def main() -> None:
    args = argument_parser().parse_args()
    log_dir = application_data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s pid=%(process)d %(message)s",
                        handlers=[保留日志处理器(log_dir / "app.log")])
    logging.info("应用启动：名称=%s，版本=%s，可执行文件=%s，构建=%s", APP_NAME, app_version(), sys.executable, build_info())
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    root = tk.Tk()
    Application(root, autostart=False if args.no_autostart else None)
    root.mainloop()


if __name__ == "__main__":
    main()
