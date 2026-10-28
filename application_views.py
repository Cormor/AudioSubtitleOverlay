"""Task-oriented Tk views. Runtime work stays in the application controller/services."""
from tkinter import ttk
from brand import APP_NAME, app_version
from languages import source_language_names, target_language_names
from model_manager import MODEL_DEFINITIONS
from recognition_profiles import PROFILES
from settings import RECOGNITION_BEAM_SIZES, RECOGNITION_NUMERIC_OPTIONS
from theme import THEME_LABELS
from ui_guidance import DEVICE_MODE_LABELS, MODEL_FILTERS, DEVICE_FILTERS

_TEMPERATURE_LABELS = {"0": "仅使用 0", "0,0.2,0.4": "低温回退（0、0.2、0.4）"}

def build_workspace(self) -> None:
    outer = ttk.Frame(self.root, padding=10, style="Surface.TFrame")
    self._workspace = outer
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer, padding=(10, 5))
    header.pack(fill="x", pady=(0, 6))
    header.columnconfigure(1, weight=1)
    brand = ttk.Frame(header)
    brand.grid(row=0, column=0, sticky="w")
    ttk.Label(brand, text=APP_NAME, style="Title.TLabel").pack(side="left")
    ttk.Label(brand, text=f"  v{app_version()}", style="Muted.TLabel").pack(side="left")
    utilities = ttk.Frame(header)
    utilities.grid(row=0, column=1, sticky="e", padx=8)
    ttk.Button(utilities, text="字幕窗", command=self._show_overlay).pack(side="left")
    ttk.Button(utilities, text="暂藏字幕", command=lambda: self.overlay.hide()).pack(side="left", padx=8)
    ttk.Button(utilities, text="收进托盘", command=self._hide_settings).pack(side="left")
    theme_control = ttk.Combobox(header, textvariable=self.theme_label_var, values=tuple(THEME_LABELS.values()), state="readonly", width=12)
    theme_control.grid(row=0, column=2, sticky="e")
    theme_control.bind("<<ComboboxSelected>>", self._on_theme_selected)
    def fit_header(event):
        narrow = event.width < brand.winfo_reqwidth()+utilities.winfo_reqwidth()+theme_control.winfo_reqwidth()+48
        utilities.grid_configure(row=1 if narrow else 0, column=0 if narrow else 1,
                                 columnspan=3 if narrow else 1, pady=(8,0) if narrow else 0)
    header.bind("<Configure>", fit_header)

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
              style="Muted.TLabel").pack(side="left")

    self.notebook = notebook = ttk.Notebook(outer)
    notebook.pack(fill="both", expand=True)
    self._scroll_pages = []
    container = self._scroll_page(notebook, "实时字幕")
    appearance = self._scroll_page(notebook, "字幕外观")
    models = self._scroll_page(notebook, "模型与资源")
    advanced = self._scroll_page(notebook, "高级设置")
    help_page = self._scroll_page(notebook, "帮助与诊断")
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
    summary = ttk.Frame(container, padding=(10, 8))
    summary.pack(fill="x", pady=(0, 8))
    ttk.Label(summary, textvariable=self.model_hint_var, style="Muted.TLabel", wraplength=680).pack(side="left", fill="x", expand=True)
    ttk.Button(summary, text="更换识别方案", command=lambda: notebook.select(2)).pack(side="right", padx=(12, 0))

    model_frame = ttk.LabelFrame(models, text="识别方案与运行设备", padding=14)
    model_frame.pack(fill="x", pady=(0, 10))
    model_frame.columnconfigure(1, weight=1)
    ttk.Label(model_frame, text="当前模型").grid(row=0, column=0, sticky="w", padx=(0,12))
    self.model_combo = ttk.Combobox(model_frame, textvariable=self.model_display_var, values=list(self._model_labels.values()), state="readonly", width=28)
    self.model_combo.grid(row=0, column=1, sticky="ew", pady=4)
    self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
    ttk.Button(model_frame, text="管理", command=self._show_model_manager).grid(row=0,column=2,padx=(8,0))
    ttk.Label(model_frame, text="运行设备").grid(row=1, column=0, sticky="w", padx=(0,12))
    self.device_combo = ttk.Combobox(model_frame, textvariable=self.device_display_var,
        values=tuple(DEVICE_MODE_LABELS.values()), state="readonly", width=22)
    self.device_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
    self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
    ttk.Label(model_frame, text="模型分类").grid(row=2,column=0,sticky="w",padx=(0,12))
    model_filter = ttk.Combobox(model_frame, textvariable=self.model_filter_var, values=MODEL_FILTERS, state="readonly", width=18)
    model_filter.grid(row=2,column=1,columnspan=2,sticky="ew",pady=4)
    model_filter.bind("<<ComboboxSelected>>", self._filter_models)
    ttk.Label(model_frame, text="设备筛选").grid(row=3,column=0,sticky="w",padx=(0,12))
    self.model_device_filter = ttk.Combobox(model_frame, textvariable=self.model_device_filter_var, values=DEVICE_FILTERS, state="readonly", width=18)
    self.model_device_filter.grid(row=3,column=1,columnspan=2,sticky="ew",pady=4)
    self.model_device_filter.bind("<<ComboboxSelected>>", self._filter_models)
    for row,var in ((4,self.device_hint_var),(5,self.model_hint_var)):
        ttk.Label(model_frame,textvariable=var,style="Muted.TLabel",wraplength=820,justify="left").grid(row=row,column=0,columnspan=3,sticky="w",pady=(8,0))
    self.model_var.trace_add("write", self._update_model_description)
    self.device_var.trace_add("write", self._update_model_description)
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
    ttk.Label(translation_frame, textvariable=self.translation_hint_var, style="Muted.TLabel", wraplength=820).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))
    ttk.Label(translation_frame, textvariable=self.translation_status_var, style="Accent.TLabel", wraplength=820).grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 0))
    self.translation_var.trace_add("write", self._update_translation_notice)
    self._update_translation_notice()

    preview_frame = ttk.LabelFrame(container, text="最近字幕", padding=10)
    preview_frame.pack(fill="both", expand=True)
    self.source_preview = ttk.Label(preview_frame, textvariable=self.source_display_var, wraplength=740, justify="left", font=("Microsoft YaHei UI", 11))
    self.source_preview.pack(anchor="w", fill="x")
    self.translation_preview = ttk.Label(preview_frame, textvariable=self.translation_display_var, wraplength=740, justify="left", style="Accent.TLabel")
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
    ttk.Label(profile, textvariable=self.current_profile_var, style="Accent.TLabel").pack(anchor="w", pady=(6, 0))
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
        style="Muted.TLabel",
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
        style="Muted.TLabel",
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
        style="Muted.TLabel",
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
        ttk.Label(tuning, text=hint, style="Muted.TLabel", wraplength=620).grid(row=row*2+1, column=1, columnspan=2, sticky="w", pady=(0,10))
    tuning.columnconfigure(1, weight=1)


    model_details = ttk.LabelFrame(models, text="自定义模型与应用音频", padding=12)
    model_details.pack(fill="x", pady=(0, 10))
    model_details.columnconfigure(1, weight=1)
    ttk.Label(model_details, text="本地模型目录").grid(row=0, column=0, padx=(0, 8), pady=5, sticky="w")
    self.model_path_entry = ttk.Entry(model_details, textvariable=self.model_path_var, width=52)
    self.model_path_entry.grid(row=0, column=1, pady=5, sticky="ew")
    self.model_path_entry.bind("<Return>", self._queue_live_configuration)
    self.model_path_entry.bind("<FocusOut>", self._queue_live_configuration)
    ttk.Label(model_details, text="留空使用模型管理中的模型。自定义目录须符合所选模型的格式。", style="Muted.TLabel", wraplength=650).grid(row=1, column=1, sticky="w")
    ttk.Label(model_details, text="Beta 自动跟踪当前有声音的应用需要 Windows 10 build 20348 或更高版本，部分应用不支持；音源列表仍可选择普通系统播放。", style="Muted.TLabel", wraplength=650).grid(row=2, column=1, sticky="w", pady=(8, 0))
    diagnostic = ttk.LabelFrame(help_page, text="诊断与恢复", padding=14)
    diagnostic.pack(fill="x", pady=(0, 10))
    ttk.Label(diagnostic, text="诊断报告只包含版本、运行状态和经过筛选的设置，不含录音与字幕正文。", style="Muted.TLabel", wraplength=740).pack(anchor="w")
    actions = ttk.Frame(diagnostic)
    actions.pack(fill="x", pady=(10, 0))
    ttk.Button(actions, text="导出诊断报告", command=self._export_diagnostics).pack(side="left")
    ttk.Button(actions, text="找回字幕窗", command=lambda: self.overlay.restore_position()).pack(side="left", padx=10)
    self._build_help(help_page)
    for page in (container, appearance, models, advanced, help_page):
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
