"""Windows 音频识别翻译悬浮窗应用入口。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import queue
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

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
    RECOGNITION_BEAM_SIZES,
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

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = Settings.load()
        self.pipeline: LivePipeline | None = None
        self._session = 0
        self._pending_presentations: dict[int, SourceUpdate] = {}
        self._events = queue.SimpleQueue()
        self._display = DisplayModule(
            language_code(self.settings.source_language),
            language_code(self.settings.target_language, allow_auto=False) or "zh",
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

        self.root.title("系统音频识别与翻译悬浮窗")
        # 首次打开尽量多展示内容；屏幕较小时由滚动区域承载剩余内容。
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        initial_width = max(760, min(960, screen_width - 80))
        initial_height = max(640, min(960, screen_height - 100))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(760, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
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
        # 等待界面进入事件循环后自动开始识别，启动时即可接收连续结果。
        self.root.after_idle(self._start)
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
        self.model_path_var = tk.StringVar(value=self.settings.model_path)
        self.audio_device_var = tk.StringVar(value=self.settings.loopback_device)
        self.audio_default_var = tk.StringVar(value="Windows 默认播放：读取中……")
        self.audio_selection_var = tk.StringVar()
        self.audio_device_var.trace_add("write", self._update_audio_selection)
        self._update_audio_selection()
        self.translation_var = tk.StringVar(value=self.settings.translation_backend)
        self.status_var = tk.StringVar(value="准备就绪")
        self.source_display_var = tk.StringVar(value="等待识别")
        self.translation_display_var = tk.StringVar(value="等待翻译")

    def _update_audio_selection(self, *_args) -> None:
        """选择项变化只更新选择状态，不冒充已打开的采集设备。"""
        self.audio_selection_var.set(f"已选择：{self.audio_device_var.get() or '未选择'}")

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        self._scroll_canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        self._scroll_canvas.grid(row=0, column=0, sticky="nsew")
        self._scrollbar = ttk.Scrollbar(
            outer,
            orient="vertical",
            command=self._scroll_canvas.yview,
        )
        self._scrollbar.grid(row=0, column=1, sticky="ns")
        self._scroll_canvas.configure(yscrollcommand=self._scrollbar.set)

        container = ttk.Frame(self._scroll_canvas)
        self._scroll_window = self._scroll_canvas.create_window(
            (0, 0),
            window=container,
            anchor="nw",
        )
        container.bind("<Configure>", self._update_scroll_region)
        self._scroll_canvas.bind("<Configure>", self._resize_scroll_content)
        self.root.bind_all("<MouseWheel>", self._on_settings_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_settings_scroll_button, add="+")
        self.root.bind_all("<Button-5>", self._on_settings_scroll_button, add="+")

        title = ttk.Label(
            container,
            text="系统音频识别与翻译悬浮窗",
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        title.pack(anchor="w")
        ttk.Label(
            container,
            text="识别电脑播放或麦克风输入的声音，并显示连续字幕。",
        ).pack(anchor="w", pady=(4, 14))

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
        ttk.Label(audio_frame, text="本地模型目录（可选）").grid(
            row=5, column=0, padx=(0, 8), pady=5, sticky="w"
        )
        self.model_path_entry = ttk.Entry(audio_frame, textvariable=self.model_path_var, width=52)
        self.model_path_entry.grid(
            row=5, column=1, columnspan=2, pady=5, sticky="ew"
        )
        self.model_path_entry.bind("<Return>", self._queue_live_configuration)
        self.model_path_entry.bind("<FocusOut>", self._queue_live_configuration)
        ttk.Label(
            audio_frame,
            text="可在“模型管理”中下载或查看本地模型。",
            foreground="#5c6773",
        ).grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Label(
            audio_frame,
            text="Beta 会尝试跟踪当前有声音的应用；需要 Windows 10 build 20348 或更高版本，部分应用可能不支持。",
            foreground="#5c6773",
        ).grid(row=7, column=1, columnspan=2, sticky="w", pady=(2, 0))
        audio_frame.columnconfigure(1, weight=1)

        quality_frame = ttk.LabelFrame(container, text="识别质量", padding=12)
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

        translation_frame = ttk.LabelFrame(container, text="翻译", padding=12)
        translation_frame.pack(fill="x", pady=(0, 10))
        self._add_labeled_combo(
            translation_frame,
            0,
            "翻译后端",
            self.translation_var,
            ["本地优先，失败转在线", "仅使用本地翻译", "仅使用免费在线 MyMemory"],
            width=32,
        )
        ttk.Label(
            translation_frame,
            text="本地翻译首次使用时会下载所需语言模型；下载后可离线使用。在线后端会把识别文本发送到公共接口。",
            foreground="#5c6773",
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))

        overlay_frame = ttk.LabelFrame(container, text="悬浮窗外观", padding=12)
        overlay_frame.pack(fill="x", pady=(0, 10))
        self._build_overlay_controls(overlay_frame)

        action_frame = ttk.Frame(container)
        action_frame.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(action_frame, text="开始识别", command=self._start)
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
        ttk.Button(action_frame, text="保存设置", command=self._save_settings).pack(
            side="right"
        )

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
        ttk.Label(preview_frame, textvariable=self.status_var, foreground="#245b9e").pack(
            anchor="w"
        )

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

    def _update_scroll_region(self, _event=None) -> None:
        """根据内容高度更新主窗口的可滚动范围。"""
        self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))

    def _resize_scroll_content(self, event) -> None:
        """让内容宽度跟随窗口，避免出现不必要的横向滚动。"""
        self._scroll_canvas.itemconfigure(self._scroll_window, width=event.width)

    def _in_scroll_content(self, widget) -> bool:
        """判断鼠标事件是否来自主窗口的可滚动内容。"""
        current = widget
        while current is not None:
            if current is self._scroll_canvas:
                return True
            current = getattr(current, "master", None)
        return False

    def _on_settings_mousewheel(self, event):
        """在主窗口内容区域响应鼠标滚轮。"""
        if not self._in_scroll_content(event.widget):
            return
        # 下拉框、输入框和滑块保留控件自身的滚轮行为。
        if event.widget.winfo_class() in {"TCombobox", "TSpinbox", "Entry", "TScale"}:
            return
        delta = int(event.delta / 120)
        self._scroll_canvas.yview_scroll(-delta if delta else (-1 if event.delta < 0 else 1), "units")
        return "break"

    def _on_settings_scroll_button(self, event):
        """兼容使用 Button-4/Button-5 发送滚轮事件的环境。"""
        if not self._in_scroll_content(event.widget):
            return
        self._scroll_canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
        return "break"

    def _build_overlay_controls(self, parent) -> None:
        ttk.Button(parent, text="选择背景颜色", command=self._choose_background).grid(
            row=0, column=0, padx=(0, 8), pady=4, sticky="w"
        )
        ttk.Button(parent, text="选择原文描边颜色", command=self._choose_outline).grid(
            row=0, column=1, padx=(0, 8), pady=4, sticky="w"
        )
        ttk.Button(parent, text="选择原文颜色", command=self._choose_text_color).grid(
            row=0, column=2, padx=(0, 8), pady=4, sticky="w"
        )
        ttk.Button(parent, text="选择译文描边颜色", command=self._choose_translation_outline).grid(
            row=0, column=3, padx=(0, 8), pady=4, sticky="w"
        )
        ttk.Button(parent, text="选择译文颜色", command=self._choose_translation_text_color).grid(
            row=0, column=4, padx=(0, 8), pady=4, sticky="w"
        )
        self._topmost_var = tk.BooleanVar(value=self.settings.overlay_topmost)
        ttk.Checkbutton(
            parent,
            text="窗口置顶",
            command=self._apply_overlay_settings,
            variable=self._topmost_var,
        ).grid(row=0, column=5, padx=(0, 8), pady=4, sticky="w")
        ttk.Label(parent, text="原文字大小").grid(row=1, column=0, sticky="w", pady=4)
        self.font_scale = ttk.Scale(
            parent,
            from_=14,
            to=48,
            value=self.settings.overlay_font_size,
            command=self._font_scale_changed,
        )
        self.font_scale.grid(row=1, column=1, columnspan=4, sticky="ew", pady=4)
        ttk.Label(parent, text="译文字大小").grid(row=2, column=0, sticky="w", pady=4)
        self.translation_font_scale = ttk.Scale(
            parent,
            from_=12,
            to=48,
            value=self.settings.overlay_translation_font_size,
            command=self._translation_font_scale_changed,
        )
        self.translation_font_scale.grid(row=2, column=1, columnspan=4, sticky="ew", pady=4)
        ttk.Label(parent, text="背景不透明度（0＝透明）").grid(row=3, column=0, sticky="w", pady=4)
        self.background_opacity_scale = ttk.Scale(
            parent,
            from_=0.0,
            to=1.0,
            value=self.settings.overlay_background_opacity,
            command=self._background_opacity_scale_changed,
        )
        self.background_opacity_scale.grid(row=3, column=1, columnspan=4, sticky="ew", pady=4)
        ttk.Label(parent, text="文字不透明度（0＝透明）").grid(row=4, column=0, sticky="w", pady=4)
        self.text_opacity_scale = ttk.Scale(
            parent,
            from_=0.0,
            to=1.0,
            value=self.settings.overlay_text_opacity,
            command=self._text_opacity_scale_changed,
        )
        self.text_opacity_scale.grid(row=4, column=1, columnspan=4, sticky="ew", pady=4)
        milliseconds = round(self.settings.overlay_scroll_interval * 1000)
        self.scroll_interval_label = ttk.Label(parent, text="每行停留最低时间（毫秒）")
        self.scroll_interval_label.grid(row=5, column=0, sticky="w", pady=4)
        self.scroll_interval_var = tk.StringVar(value=str(milliseconds))
        self.scroll_interval_scale = ttk.Scale(
            parent, from_=0, to=8000, value=milliseconds,
            command=self._scroll_interval_changed,
        )
        self.scroll_interval_scale.grid(row=5, column=1, columnspan=3, sticky="ew", pady=4)
        interval_input = ttk.Spinbox(
            parent, from_=0, to=8000, increment=1, width=8,
            textvariable=self.scroll_interval_var,
            command=self._scroll_interval_committed,
        )
        interval_input.grid(row=5, column=4, sticky="w", padx=8)
        interval_input.bind("<Return>", self._scroll_interval_committed)
        interval_input.bind("<FocusOut>", self._scroll_interval_committed)

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
                "字幕窗口已提交：序号=%s，段落=%s，音频终点=%.3f秒，耗时=%s",
                update.sequence, block_id, update.audio_end, measurements,
            )
        # 未进入可见区域的中间结果不算上屏，也不长期保留计时记录。
        self._pending_presentations.clear()

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
            beam_size=beam_size,
            condition_on_previous_text=self.context_var.get() == "启用",
            temperature_schedule=_TEMPERATURE_VALUES_BY_LABEL.get(
                self.temperature_var.get(), "0"
            ),
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
            self._set_status("正在应用新的识别设置……")

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

    def _scroll_interval_committed(self, *_args) -> None:
        """整数输入支持逐毫秒调整，空值或非法输入恢复已保存的值。"""
        try:
            milliseconds = int(self.scroll_interval_var.get())
        except ValueError:
            milliseconds = round(self.settings.overlay_scroll_interval * 1000)
        self.scroll_interval_scale.set(max(0, min(8000, milliseconds)))

    def _scroll_interval_changed(self, value) -> None:
        """界面使用毫秒；沿用秒单位配置字段，兼容旧配置。"""
        milliseconds = max(0, min(8000, round(float(value))))
        self.settings.overlay_scroll_interval = milliseconds / 1000
        self.scroll_interval_var.set(str(milliseconds))
        if hasattr(self, "overlay"):
            self.overlay._cancel_auto_scroll()
            self.overlay._schedule_auto_scroll()

    def _font_scale_changed(self, value) -> None:
        self.settings.overlay_font_size = int(float(value))
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()

    def _translation_font_scale_changed(self, value) -> None:
        self.settings.overlay_translation_font_size = int(float(value))
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()

    def _background_opacity_scale_changed(self, value) -> None:
        self.settings.overlay_background_opacity = float(value)
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()

    def _text_opacity_scale_changed(self, value) -> None:
        self.settings.overlay_text_opacity = float(value)
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()

    def _apply_overlay_settings(self) -> None:
        if hasattr(self, "_topmost_var"):
            self.settings.overlay_topmost = self._topmost_var.get()
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()

    def _show_overlay(self) -> None:
        self.overlay.show()

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
        self.settings.model_path = self.model_path_var.get()
        self.settings.loopback_device = self.audio_device_var.get()
        self.settings.translation_backend = self.translation_var.get()
        if hasattr(self, "overlay"):
            self.settings.overlay_geometry = self.overlay.window.geometry()
        self.settings.save()
        self._set_status("设置已保存。")

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
        self.overlay.set_entries(snapshot.entries, snapshot.revision_modes)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
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
    log_dir = application_data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
