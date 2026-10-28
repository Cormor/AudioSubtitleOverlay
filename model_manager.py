"""本地语音模型的状态、下载和管理界面。"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable
from urllib.parse import quote

from download_manager import ComponentFile, ComponentSpec, DownloadProgress
from settings import application_data_dir
from engine_models import ONNX_MODELS


@dataclass(frozen=True)
class ModelDefinition:
    """应用支持的模型及其处理能力。"""

    key: str
    repo_id: str
    display_name: str
    description: str
    backend: str = "whisper"
    languages: tuple[str, ...] = ()
    streaming: bool = False

    @property
    def supported_devices(self) -> tuple[str, ...]:
        # These are the providers implemented by this application.
        return ("cpu", "cuda") if self.backend == "whisper" else ("cpu",)

    @property
    def device_label(self) -> str:
        return "CPU / NVIDIA GPU" if "cuda" in self.supported_devices else "仅 CPU"


MODEL_DEFINITIONS = (
    ModelDefinition(
        "tiny",
        "Systran/faster-whisper-tiny",
        "tiny",
        "模型体积较小，启动和识别速度较快，适合验证音频链路。",
    ),
    ModelDefinition(
        "base",
        "Systran/faster-whisper-base",
        "base",
        "在速度、资源占用和识别质量之间保持较均衡的选择。",
    ),
    ModelDefinition(
        "small",
        "Systran/faster-whisper-small",
        "small",
        "通常比 tiny 和 base 提供更稳定的识别质量，但资源占用也更高。",
    ),
    ModelDefinition(
        "medium",
        "Systran/faster-whisper-medium",
        "medium",
        "识别质量通常进一步提高，需要更多内存、显存和计算时间。",
    ),
    ModelDefinition(
        "large-v3",
        "Systran/faster-whisper-large-v3",
        "large-v3",
        "较大的多语言模型，适合对识别质量要求较高且资源充足的设备。",
    ),
) + (
    ModelDefinition("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                    "Whisper Turbo · 多语言", "减少解码层的多语言模型；适合有 GPU 的视频字幕，仍使用滚动窗口。"),
    ModelDefinition("tiny.en", "Systran/faster-whisper-tiny.en", "Whisper Tiny English",
                    "英语专用的小模型；适合轻量英语字幕，不支持中文。", languages=("en",)),
    ModelDefinition("base.en", "Systran/faster-whisper-base.en", "Whisper Base English",
                    "英语专用，体积与计算量高于 tiny.en；准确率需按素材比较。", languages=("en",)),
    ModelDefinition("distil-large-v3", "Systran/faster-distil-whisper-large-v3", "Distil Whisper · 英语",
                    "英语蒸馏模型，适合需要较高英语质量的 GPU 场景。", languages=("en",)),
) + tuple(
    ModelDefinition(key, item["repo_id"],
                    ("SenseVoice INT8 · 中粤英日韩" if key == "sensevoice-int8" else
                     "Zipformer CTC Small · 中英" if "small" in key else
                     "Zipformer CTC Medium · 中英" if "medium" in key else "Zipformer Transducer · 中英"),
                    item["description"], backend="sensevoice" if key == "sensevoice-int8" else "zipformer",
                    languages=tuple(item["languages"]), streaming=item["streaming"])
    for key, item in ONNX_MODELS.items()
)

MODEL_BY_KEY = {item.key: item for item in MODEL_DEFINITIONS}
MODEL_MARKER = ".audio-subtitle-overlay-model.json"


def validate_model_language(model_key: str, language: str | None) -> None:
    """在准备模型之前指出不兼容的语言，自动检测只使用模型支持的范围。"""
    definition = model_definition(model_key)
    if language and definition.languages and language not in definition.languages:
        raise ValueError(f"{definition.display_name} 不支持语言 {language}；请选择兼容的模型或语言。")
    if language == "yue" and definition.backend == "whisper" and model_key not in {"large-v3", "large-v3-turbo"}:
        raise ValueError("该 Whisper 变体未提供粤语支持；请选择 SenseVoice 或 large-v3 / Turbo。")


def model_directory_complete(model_key: str, path: Path) -> bool:
    if model_key in ONNX_MODELS:
        return path.is_dir() and all((path / name).is_file() and (path / name).stat().st_size == info["size"]
                                     for name, info in ONNX_MODELS[model_key]["files"].items())
    return model_is_complete(path)


def model_definition(model_key: str) -> ModelDefinition:
    """按模型名称取得定义，不接受未列出的模型。"""
    try:
        return MODEL_BY_KEY[model_key]
    except KeyError as error:
        raise ValueError(f"不支持的模型：{model_key}") from error


def managed_models_dir() -> Path:
    """返回应用管理的可写模型目录。"""
    path = application_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def managed_model_dir(model_key: str) -> Path:
    """返回指定模型的应用管理目录。"""
    model_definition(model_key)
    return managed_models_dir() / model_key


def directory_size(path: Path) -> int:
    """递归计算目录中的文件大小。"""
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def format_bytes(value: int | None) -> str:
    """把字节数格式化为界面可读的大小。"""
    if value is None or value < 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(number)} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return "未知"


def model_is_complete(path: Path) -> bool:
    """检查模型目录是否包含 faster-whisper 的核心文件。"""
    if not path.is_dir():
        return False
    required = (path / "config.json", path / "model.bin", path / "tokenizer.json")
    return all(item.is_file() for item in required)


def _bundled_model_candidates(model_key: str) -> list[Path]:
    """返回 EXE、PyInstaller 临时目录和源码目录中的模型候选路径。"""
    roots: list[Path] = []
    executable = getattr(sys, "executable", "")
    if executable:
        roots.append(Path(executable).resolve().parent)
    temporary_root = getattr(sys, "_MEIPASS", "")
    if temporary_root:
        roots.append(Path(temporary_root))
    roots.append(Path(__file__).resolve().parent)
    result: list[Path] = []
    for root in roots:
        candidate = root / "models" / model_key
        if candidate not in result:
            result.append(candidate)
    return result


def _local_huggingface_snapshots(model_key: str) -> list[Path]:
    """只查找本机已有的 Hugging Face 快照，不触发网络访问。"""
    cache_value = os.environ.get("HF_HUB_CACHE")
    if cache_value:
        cache_root = Path(cache_value)
    else:
        hf_home = os.environ.get("HF_HOME")
        cache_root = (
            Path(hf_home) / "hub"
            if hf_home
            else Path.home() / ".cache" / "huggingface" / "hub"
        )
    repository = cache_root / ("models--" + model_definition(model_key).repo_id.replace("/", "--"))
    snapshot_root = repository / "snapshots"
    if not snapshot_root.is_dir():
        return []
    try:
        return sorted(snapshot_root.iterdir(), reverse=True)
    except OSError:
        return []


def find_local_model(model_key: str, explicit_path: str = "") -> Path | None:
    """按优先级查找本地模型；该函数绝不下载模型。"""
    model_definition(model_key)
    if explicit_path.strip():
        path = Path(explicit_path.strip()).expanduser()
        return path if model_directory_complete(model_key, path) else None

    candidates = [managed_model_dir(model_key)]
    candidates.extend(_bundled_model_candidates(model_key))
    candidates.extend(_local_huggingface_snapshots(model_key))
    for candidate in candidates:
        if model_directory_complete(model_key, candidate):
            return candidate
    return None


def model_cache_hint() -> str:
    """返回模型查找和管理位置提示。"""
    return f"模型管理目录：{managed_models_dir()}；缺失模型时会按组件清单准备。"


def _remote_model_files(definition: ModelDefinition) -> tuple[str, list[ComponentFile]]:
    """读取固定远端修订版本及文件元数据，供通用下载器使用。"""
    if definition.key in ONNX_MODELS:
        item = ONNX_MODELS[definition.key]
        return item["revision"], [
            ComponentFile(path=name,
                          url=f"https://huggingface.co/{item['repo_id']}/resolve/{item['revision']}/{quote(name)}?download=true",
                          size=info["size"], sha256=info["sha256"])
            for name, info in item["files"].items()
        ]
    from huggingface_hub import HfApi

    info = HfApi().model_info(definition.repo_id, files_metadata=True)
    revision = str(getattr(info, "sha", "") or "main")
    siblings = getattr(info, "siblings", []) or []
    files: list[ComponentFile] = []
    for sibling in siblings:
        name = str(getattr(sibling, "rfilename", "") or "")
        if not name:
            continue
        size_value = getattr(sibling, "size", None)
        size = None if size_value is None else int(size_value)
        lfs = getattr(sibling, "lfs", None)
        sha256 = getattr(lfs, "sha256", None) if lfs is not None else None
        encoded_name = quote(name, safe="/")
        files.append(
            ComponentFile(
                path=name,
                url=(
                    f"https://huggingface.co/{definition.repo_id}/resolve/"
                    f"{revision}/{encoded_name}?download=true"
                ),
                size=size,
                sha256=(str(sha256).lower() if sha256 else None),
            )
        )
    if not files:
        raise RuntimeError(f"远端模型没有可下载文件：{definition.repo_id}")
    return revision, files


def _build_model_component(definition: ModelDefinition) -> ComponentSpec:
    """为指定模型构造固定远端修订版本的组件定义。"""
    revision, files = _remote_model_files(definition)
    return ComponentSpec(
        key=definition.key,
        version=revision,
        target_dir=managed_model_dir(definition.key),
        files=tuple(files),
        marker_name=MODEL_MARKER,
    )


def _remote_model_size(model_key: str) -> int | None:
    """查询模型仓库文件总大小。"""
    definition = model_definition(model_key)
    _, files = _remote_model_files(definition)
    sizes = [item.size for item in files if item.size is not None]
    total = sum(sizes)
    return total or None


@dataclass
class LocalModelStatus:
    """一个模型的本地状态。"""

    state: str
    path: Path | None
    size: int


class ModelManager:
    """管理模型下载任务，并保证下载线程不阻塞 Tk 主线程。"""

    def __init__(self) -> None:
        from runtime_components import component_download_manager

        self._downloader = component_download_manager()

    def local_status(self, model_key: str) -> LocalModelStatus:
        """读取模型本地状态，不访问网络。"""
        managed = managed_model_dir(model_key)
        if model_directory_complete(model_key, managed):
            return LocalModelStatus("已下载", managed, directory_size(managed))
        if managed.exists():
            return LocalModelStatus("下载未完成", managed, directory_size(managed))

        for candidate in _bundled_model_candidates(model_key):
            if model_directory_complete(model_key, candidate):
                return LocalModelStatus("已内置", candidate, directory_size(candidate))
        for candidate in _local_huggingface_snapshots(model_key):
            if model_directory_complete(model_key, candidate):
                return LocalModelStatus("本机缓存", candidate, directory_size(candidate))
        return LocalModelStatus("未下载", None, 0)

    def is_downloading(self, model_key: str) -> bool:
        """返回指定模型是否有下载任务。"""
        return self._downloader.is_active(model_key)

    def pause(self, model_key: str) -> bool:
        """暂停模型下载并保留断点。"""
        return self._downloader.pause(model_key)

    def resume(self, model_key: str) -> bool:
        """继续模型下载。"""
        return self._downloader.resume(model_key)

    def cancel(self, model_key: str) -> bool:
        """请求取消指定模型的下载任务。"""
        return self._downloader.cancel(model_key)

    def delete_managed_model(self, model_key: str) -> None:
        """删除应用管理目录中的模型，不删除内置模型和 Hugging Face 缓存。"""
        if self.is_downloading(model_key):
            raise RuntimeError("模型正在下载，不能删除。")
        path = managed_model_dir(model_key)
        if path.exists():
            shutil.rmtree(path)

    def query_remote_size(self, model_key: str) -> int | None:
        """查询远端模型大小。"""
        return _remote_model_size(model_key)

    def ensure_model(
        self,
        model_key: str,
        on_progress: Callable[[DownloadProgress], None] | None = None,
        cancel_wait: Callable[[], bool] | None = None,
    ) -> Path:
        """同步准备模型；优先使用固定清单，其他模型按远端提交修订下载。"""
        existing = find_local_model(model_key)
        if existing is not None:
            return existing
        definition = model_definition(model_key)
        try:
            from runtime_components import component_spec

            static_spec = component_spec(model_key)
        except (FileNotFoundError, RuntimeError, KeyError):
            static_spec = None
        wait_options = {"cancel_wait": cancel_wait} if cancel_wait is not None else {}
        if static_spec is not None:
            self._downloader.ensure(static_spec, on_progress=on_progress, **wait_options)
        else:
            self._downloader.ensure_factory(
                definition.key,
                lambda: _build_model_component(definition),
                on_progress=on_progress,
                **wait_options,
            )
        result = find_local_model(model_key)
        if result is None:
            raise RuntimeError(f"模型{model_key}下载后未找到完整模型目录。")
        return result

    def start_download(
        self,
        model_key: str,
        on_progress: Callable[[str, int, int | None, str, float | None, str], None],
        on_finished: Callable[[str, str, str], None],
    ) -> None:
        """启动模型下载，回调参数只包含普通数据。"""
        definition = model_definition(model_key)

        def factory() -> ComponentSpec:
            return _build_model_component(definition)

        def progress(snapshot: DownloadProgress) -> None:
            on_progress(
                snapshot.component_key,
                snapshot.completed,
                snapshot.total,
                snapshot.file_name or snapshot.detail,
                snapshot.speed,
                snapshot.state,
            )

        self._downloader.start_factory(
            definition.key,
            factory,
            on_progress=progress,
            on_finished=on_finished,
        )


def ensure_model_available(
    model_key: str,
    on_progress: Callable[[DownloadProgress], None] | None = None,
    cancel_wait: Callable[[], bool] | None = None,
) -> Path:
    """为后台识别准备模型，保持模型管理窗口与启动流程共用下载任务。"""
    return ModelManager().ensure_model(model_key, on_progress=on_progress, cancel_wait=cancel_wait)


class ModelManagerWindow:
    """显示模型状态与说明，并控制模型下载任务。"""

    def __init__(self, master: tk.Misc, manager: ModelManager | None = None) -> None:
        self.manager = manager or ModelManager()
        self._closed = False
        self._remote_sizes: dict[str, int | None] = {}
        self._progress: dict[str, tuple[int, int | None, str, float | None, str]] = {}
        self._runtime_progress: DownloadProgress | None = None
        self._window = tk.Toplevel(master)
        self._window.title("语音模型管理")
        # Tk reports screen dimensions in its own coordinate system. Do not
        # apply a second DPI multiplier on top of Windows scaling.
        width = max(760, min(1000, self._window.winfo_screenwidth() - 64))
        height = max(380, min(760, self._window.winfo_screenheight() - 80))
        self._window.geometry(f"{width}x{height}")
        self._window.minsize(min(760, width), min(520, height))
        self._window.protocol("WM_DELETE_WINDOW", self.close)

        # Keep every action reachable at short window heights and larger text.
        from theme import palette
        colors = palette(getattr(master, "_shengmu_theme_preference", "system"))
        self._scroll_canvas = tk.Canvas(self._window, highlightthickness=0, background=colors["surface"])
        outer_bar = ttk.Scrollbar(self._window, orient="vertical", command=self._scroll_canvas.yview)
        outer_bar.pack(side="right", fill="y")
        self._scroll_canvas.pack(side="left", fill="both", expand=True)
        self._scroll_canvas.configure(yscrollcommand=outer_bar.set)
        container = ttk.Frame(self._scroll_canvas, padding=12)
        self._content = container
        slot = self._scroll_canvas.create_window((0, 0), window=container, anchor="nw")
        def fit_content(_event=None):
            self._scroll_canvas.itemconfigure(slot, width=self._scroll_canvas.winfo_width())
            self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))
        container.bind("<Configure>", fit_content, add="+")
        self._scroll_canvas.bind("<Configure>", fit_content, add="+")
        self._window.bind("<MouseWheel>", self._scroll_content, add="+")
        self._window.bind("<FocusIn>", self._reveal_control, add="+")
        ttk.Label(
            container,
            text="语音模型管理",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        self.intro_label = ttk.Label(
            container,
            text=(
                f"下载目录：{managed_models_dir()}。支持暂停、继续和断点续传；"
                "识别启动时会按配置准备缺失模型。"
            ),
            style="Muted.TLabel",
            wraplength=940,
        )
        self.intro_label.pack(anchor="w", pady=(4, 10))
        container.bind("<Configure>", self._resize_window_text, add="+")

        tree_frame = ttk.Frame(container)
        tree_frame.pack(fill="both", expand=True, pady=(0, 8))
        columns = ("model", "devices", "status", "progress", "size")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse", height=6)
        headings = {
            "model": "模型",
            "devices": "支持设备",
            "status": "状态",
            "progress": "进度",
            "size": "模型大小",
        }
        from tkinter import font as tkfont
        table_font = tkfont.Font(master=self._window, font=ttk.Style(self._window).lookup("Treeview", "font"))
        widths = {"model": max(195, max(table_font.measure(item.display_name) for item in MODEL_DEFINITIONS)+24),
                  "devices": max(135,table_font.measure("CPU / NVIDIA GPU")+24),
                  "status": max(100,table_font.measure("下载未完成")+24),
                  "progress": max(175,table_font.measure("100.0%，128 MB/秒")+24),
                  "size": max(105,table_font.measure("模型大小")+24)}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=widths[column],
                anchor="w",
                stretch=column in {"model", "progress"},
            )
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.grid(row=0,column=0,sticky="nsew")
        scrollbar.grid(row=0,column=1,sticky="ns")
        horizontal.grid(row=1,column=0,sticky="ew")
        def fit_table(event):
            if event.width < sum(widths.values()) + 18:
                horizontal.grid()
            else:
                horizontal.grid_remove()
        tree_frame.bind("<Configure>", fit_table)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

        self.details_frame = ttk.LabelFrame(container, text="所选模型", padding=(10, 7))
        self.details_frame.pack(fill="x")
        self.detail_name_var = tk.StringVar()
        self.detail_capability_var = tk.StringVar()
        self.detail_name = ttk.Label(
            self.details_frame,
            textvariable=self.detail_name_var,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.detail_name.pack(anchor="w")
        self.detail_capability = ttk.Label(
            self.details_frame, textvariable=self.detail_capability_var, style="Muted.TLabel", wraplength=900
        )
        self.detail_capability.pack(anchor="w", pady=(2, 0))
        self.detail_description = ttk.Label(
            self.details_frame, justify="left", anchor="w", wraplength=900
        )
        self.detail_description.pack(fill="x", anchor="w", pady=(4, 0))
        self.details_frame.bind("<Configure>", self._resize_detail_description, add="+")

        action_frame = ttk.Frame(container)
        action_frame.pack(fill="x", pady=(8, 0))
        primary_actions = ttk.Frame(action_frame)
        primary_actions.pack(fill="x")
        self.download_button = ttk.Button(
            primary_actions,
            text="下载模型",
            command=self._download_selected,
            style="Primary.TButton",
        )
        self.download_button.pack(side="left")
        self.pause_button = ttk.Button(
            primary_actions, text="暂停下载", command=self._pause_selected, state="disabled"
        )
        self.pause_button.pack(side="left", padx=(8, 0))
        self.resume_button = ttk.Button(
            primary_actions, text="继续下载", command=self._resume_selected, state="disabled"
        )
        self.resume_button.pack(side="left", padx=(8, 0))

        secondary_actions = ttk.Frame(action_frame)
        secondary_actions.pack(fill="x", pady=(7, 0))
        self.cancel_button = ttk.Button(
            secondary_actions,
            text="取消下载",
            command=self._cancel_selected,
            state="disabled",
        )
        self.cancel_button.pack(side="left")
        self.delete_button = ttk.Button(
            secondary_actions,
            text="删除本地模型",
            command=self._delete_selected,
            state="disabled",
            style="Danger.TButton",
        )
        self.delete_button.pack(side="left", padx=(8, 0))
        ttk.Button(secondary_actions, text="刷新状态", command=self.refresh).pack(
            side="left", padx=(8, 0)
        )
        self.status_var = tk.StringVar(value="正在读取模型状态……")
        self.status_label = ttk.Label(action_frame, textvariable=self.status_var, style="Info.TLabel", wraplength=940)
        self.status_label.pack(fill="x", anchor="w", pady=(5, 0))

        runtime_frame = ttk.LabelFrame(container, text="运行依赖", padding=8)
        runtime_frame.pack(fill="x", pady=(8, 0))
        self.runtime_help_label = ttk.Label(
            runtime_frame,
            text="使用 NVIDIA GPU 识别时需要 CUDA 运行库；仅用 CPU 无需准备。",
            style="Muted.TLabel",
            wraplength=940,
        )
        self.runtime_help_label.pack(fill="x", anchor="w")
        self.runtime_status_var = tk.StringVar(value="GPU运行库：未准备")
        self.runtime_status_label = ttk.Label(runtime_frame, textvariable=self.runtime_status_var, style="Info.TLabel", wraplength=940)
        self.runtime_status_label.pack(fill="x", anchor="w", pady=(3, 0))
        runtime_actions = ttk.Frame(runtime_frame)
        runtime_actions.pack(fill="x", pady=(6, 0))
        self.prepare_gpu_button = ttk.Button(
            runtime_actions,
            text="准备GPU运行库",
            command=self._prepare_gpu_runtime,
            style="Primary.TButton",
        )
        self.prepare_gpu_button.pack(side="left")
        self.pause_gpu_button = ttk.Button(
            runtime_actions,
            text="暂停",
            command=self._pause_gpu_runtime,
            state="disabled",
        )
        self.pause_gpu_button.pack(side="left", padx=(8, 0))
        self.resume_gpu_button = ttk.Button(
            runtime_actions,
            text="继续",
            command=self._resume_gpu_runtime,
            state="disabled",
        )
        self.resume_gpu_button.pack(side="left", padx=(8, 0))

        self._populate_rows()
        self._refresh_remote_sizes()
        self._poll_runtime_status()

    def _scroll_content(self, event):
        # The table keeps its own wheel behaviour; other regions scroll the page.
        if event.widget is not self.tree:
            self._scroll_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

    def _reveal_control(self, event):
        widget = event.widget
        if not hasattr(widget, "winfo_rooty") or self._window.focus_get() is not widget:
            return
        if isinstance(widget, (ttk.Frame, ttk.LabelFrame, tk.Toplevel)):
            return
        def reveal():
            if self._closed or not widget.winfo_exists() or self._window.focus_get() is not widget:
                return
            canvas = self._scroll_canvas
            top = widget.winfo_rooty() - canvas.winfo_rooty() + canvas.canvasy(0)
            bottom = top + widget.winfo_height()
            view_top = canvas.canvasy(0)
            height = canvas.winfo_height()
            total = max(1, self._content.winfo_height())
            if top < view_top:
                canvas.yview_moveto(max(0, top - 8) / total)
            elif bottom > view_top + height:
                canvas.yview_moveto(max(0, bottom - height + 8) / total)
        self._window.after_idle(reveal)

    def _resize_detail_description(self, event=None) -> None:
        width = event.width if event is not None else self.details_frame.winfo_width()
        self.detail_description.configure(wraplength=max(240, width - 28))
        self.detail_capability.configure(wraplength=max(240, width - 28))

    def _resize_window_text(self, event=None) -> None:
        width = event.width if event is not None else self._window.winfo_width()
        wraplength = max(320, width - 48)
        self.intro_label.configure(wraplength=wraplength)
        self.runtime_help_label.configure(wraplength=wraplength)
        self.status_label.configure(wraplength=wraplength)
        self.runtime_status_label.configure(wraplength=wraplength)

    def _populate_rows(self) -> None:
        selected = self._selected_key()
        for definition in MODEL_DEFINITIONS:
            values = self._row_values(definition)
            if self.tree.exists(definition.key):
                self.tree.item(definition.key, values=values)
            else:
                self.tree.insert("", "end", iid=definition.key, values=values)
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        elif MODEL_DEFINITIONS:
            self.tree.selection_set(MODEL_DEFINITIONS[0].key)
        self._selection_changed()

    def _row_values(self, definition: ModelDefinition):
        status = self.manager.local_status(definition.key)
        progress = self._progress.get(definition.key)
        shared = self.manager._downloader.status(definition.key)
        if shared is not None:
            progress = (shared.completed, shared.total, shared.file_name, shared.speed, shared.state)
        if progress is not None:
            completed, total, filename, speed, state = progress
            if total:
                progress_text = f"{completed / total * 100:.1f}%"
            else:
                progress_text = "已下载 " + format_bytes(completed)
            if filename:
                progress_text += f"，{filename}"
            if speed:
                progress_text += f"，{format_bytes(int(speed))}/秒"
            state_text = state
        else:
            progress_text = "—"
            state_text = status.state
        remote_size = self._remote_sizes.get(definition.key)
        return (
            definition.display_name,
            definition.device_label,
            state_text,
            progress_text,
            format_bytes(remote_size),
        )

    def _selected_key(self) -> str | None:
        selection = self.tree.selection()
        return str(selection[0]) if selection else None

    def _selection_changed(self, _event=None) -> None:
        key = self._selected_key()
        definition = MODEL_BY_KEY.get(key) if key else None
        local = self.manager.local_status(key) if key else None
        if definition:
            languages = "、".join(definition.languages) if definition.languages else "多语言"
            recognition = "支持流式识别" if definition.streaming else "滚动窗口识别"
            self.detail_name_var.set("已选中：" + definition.display_name)
            self.detail_capability_var.set(
                f"{definition.device_label}  ·  {languages}  ·  {recognition}  ·  "
                f"本地：{local.state}（{format_bytes(local.size)}）"
            )
            self.detail_description.configure(text=definition.description)
        else:
            self.detail_name_var.set("未选择模型")
            self.detail_capability_var.set("")
            self.detail_description.configure(text="")

        shared = self.manager._downloader.status(key) if key else None
        active = bool(key and self.manager.is_downloading(key))
        state = shared.state if shared is not None else ""
        paused = state in {"已暂停", "暂停中"}
        cancelling = state in {"取消中", "正在取消"}
        can_download = bool(
            key and not active and local and local.state in {"未下载", "下载未完成"}
        )
        self.download_button.configure(state="normal" if can_download else "disabled")
        self.cancel_button.configure(state="normal" if active and not cancelling else "disabled")
        self.pause_button.configure(
            state="normal" if active and not paused and not cancelling else "disabled"
        )
        self.resume_button.configure(
            state="normal" if active and paused and not cancelling else "disabled"
        )
        if key:
            self.delete_button.configure(
                state="normal" if managed_model_dir(key).exists() and not active else "disabled"
            )
        else:
            self.delete_button.configure(state="disabled")

    def _refresh_remote_sizes(self) -> None:
        # The UI owns query state. Repeated refreshes join the current request.
        if getattr(self, "_size_query_active", False):
            self._size_query_message = f"正在读取模型大小（{self._size_query_done}/{len(MODEL_DEFINITIONS)}）……"
            self.status_var.set(self._size_query_message)
            return
        self._size_query_active = True
        self._size_query_done = 0
        self._size_query_known = 0
        self._size_query_message = f"正在读取模型大小（0/{len(MODEL_DEFINITIONS)}）……"
        self.status_var.set(self._size_query_message)
        def worker():
            for definition in MODEL_DEFINITIONS:
                try:
                    size = self.manager.query_remote_size(definition.key)
                except Exception:
                    size = None
                self._post(self._remote_size_ready, definition.key, size)

        threading.Thread(target=worker, name="模型大小查询", daemon=True).start()

    def _remote_size_ready(self, model_key: str, size: int | None) -> None:
        if self._closed:
            return
        self._remote_sizes[model_key] = size
        self._populate_rows()
        self._size_query_done += 1
        self._size_query_known += int(size is not None)
        if self._size_query_done >= len(MODEL_DEFINITIONS):
            self._size_query_active = False
            message = (f"模型状态已更新：{self._size_query_known} 项已取得大小，"
                       f"{len(MODEL_DEFINITIONS)-self._size_query_known} 项未取得远端大小；本地状态仍可查看。")
        else:
            message = f"正在读取模型大小（{self._size_query_done}/{len(MODEL_DEFINITIONS)}）……"
        # Download/error notices supersede optional metadata progress.
        if self.status_var.get() == self._size_query_message:
            self.status_var.set(message)
        self._size_query_message = message

    def refresh(self) -> None:
        """刷新本地状态并重新查询远端大小。"""
        self._progress.clear()
        self._populate_rows()
        self.status_var.set("正在刷新模型大小……")
        self._refresh_remote_sizes()

    def _download_selected(self) -> None:
        key = self._selected_key()
        if not key:
            return
        try:
            self.manager.start_download(key, self._progress_callback, self._finished_callback)
        except Exception as error:
            messagebox.showerror("模型下载", str(error), parent=self._window)
            return
        self._progress[key] = (0, self._remote_sizes.get(key), "准备下载", 0.0, "准备下载")
        self.status_var.set(f"正在下载模型 {key}……")
        self._populate_rows()

    def _pause_selected(self) -> None:
        key = self._selected_key()
        if key and self.manager.pause(key):
            self.status_var.set(f"正在暂停模型 {key} 的下载……")

    def _resume_selected(self) -> None:
        key = self._selected_key()
        if key and self.manager.resume(key):
            self.status_var.set(f"正在继续模型 {key} 的下载……")

    def _cancel_selected(self) -> None:
        key = self._selected_key()
        if key and self.manager.cancel(key):
            self.status_var.set(f"正在取消模型 {key} 的下载……")

    def _delete_selected(self) -> None:
        key = self._selected_key()
        if not key:
            return
        managed_path = managed_model_dir(key)
        if not managed_path.exists():
            return
        if not messagebox.askyesno(
            "删除模型",
            f"只删除应用管理目录中的 {key} 模型，不删除内置模型和系统缓存。是否继续？",
            parent=self._window,
        ):
            return
        try:
            self.manager.delete_managed_model(key)
        except Exception as error:
            messagebox.showerror("删除模型", str(error), parent=self._window)
            return
        self.status_var.set(f"已删除应用管理目录中的模型 {key}。")
        self._populate_rows()

    def _prepare_gpu_runtime(self) -> None:
        """在后台准备GPU运行库，避免阻塞模型管理窗口。"""
        from gpu_runtime import gpu_runtime_available
        from runtime_components import prepare_component

        if self.manager._downloader.is_active("runtime.cuda12"):
            return

        def worker() -> None:
            try:
                if gpu_runtime_available():
                    self._post(self._apply_runtime_finished, "完成", "已找到可用的本机运行库。")
                    return
                prepare_component("runtime.cuda12", on_progress=self._runtime_progress_callback)
                if not gpu_runtime_available(refresh=True):
                    raise RuntimeError("已准备的NVIDIA运行库无法加载。")
            except Exception as error:
                self._post(self._apply_runtime_finished, "失败", str(error))
            else:
                self._post(self._apply_runtime_finished, "完成", "GPU运行库已准备。")

        threading.Thread(target=worker, name="GPU运行库准备", daemon=True).start()
        self.runtime_status_var.set("GPU运行库：准备中……")
        self._refresh_runtime_controls()

    def _pause_gpu_runtime(self) -> None:
        if self.manager._downloader.pause("runtime.cuda12"):
            self.runtime_status_var.set("GPU运行库：正在暂停……")

    def _resume_gpu_runtime(self) -> None:
        if self.manager._downloader.resume("runtime.cuda12"):
            self.runtime_status_var.set("GPU运行库：正在继续……")

    def _runtime_progress_callback(self, progress: DownloadProgress) -> None:
        self._post(self._apply_runtime_progress, progress)

    def _apply_runtime_progress(self, progress: DownloadProgress) -> None:
        self._runtime_progress = progress
        percentage = ""
        if progress.fraction is not None:
            percentage = f"，{progress.fraction * 100:.1f}%"
        speed = f"，{format_bytes(int(progress.speed))}/秒" if progress.speed else ""
        total = f" / {format_bytes(progress.total)}" if progress.total else ""
        file_name = f"，文件={progress.file_name}" if progress.file_name else ""
        self.runtime_status_var.set(
            f"GPU运行库：{progress.state}{percentage}，已下载"
            f"{format_bytes(progress.completed)}{total}{speed}{file_name}"
        )
        self._refresh_runtime_controls()

    def _apply_runtime_finished(self, state: str, detail: str) -> None:
        self.runtime_status_var.set(f"GPU运行库：{state}。{detail}")
        self._refresh_runtime_controls()

    def _poll_runtime_status(self) -> None:
        """显示共享队列，包括识别切换在后台留下的下载任务。"""
        if self._closed:
            return
        self._populate_rows()
        status = self.manager._downloader.status("runtime.cuda12")
        if status is not None:
            self._apply_runtime_progress(status)
        try:
            self._window.after(500, self._poll_runtime_status)
        except (tk.TclError, RuntimeError):
            pass

    def _refresh_runtime_controls(self) -> None:
        active = self.manager._downloader.is_active("runtime.cuda12")
        shared = self.manager._downloader.status("runtime.cuda12")
        progress = shared if shared is not None else self._runtime_progress
        paused = bool(progress and progress.state in {"已暂停", "暂停中"})
        self.prepare_gpu_button.configure(state="disabled" if active else "normal")
        self.pause_gpu_button.configure(state="normal" if active and not paused else "disabled")
        self.resume_gpu_button.configure(state="normal" if active and paused else "disabled")

    def _progress_callback(
        self,
        model_key: str,
        completed: int,
        total: int | None,
        filename: str,
        speed: float | None,
        state: str,
    ) -> None:
        self._post(self._apply_progress, model_key, completed, total, filename, speed, state)

    def _apply_progress(
        self,
        model_key: str,
        completed: int,
        total: int | None,
        filename: str,
        speed: float | None,
        state: str,
    ) -> None:
        self._progress[model_key] = (completed, total, filename, speed, state)
        self.status_var.set(
            f"模型 {model_key}：{state}，已下载 {format_bytes(completed)}"
            + (f" / {format_bytes(total)}" if total else "")
        )
        self._populate_rows()

    def _finished_callback(self, model_key: str, state: str, detail: str) -> None:
        self._post(self._apply_finished, model_key, state, detail)

    def _apply_finished(self, model_key: str, state: str, detail: str) -> None:
        self._progress.pop(model_key, None)
        if state == "完成":
            self.status_var.set(f"模型 {model_key} 下载完成。")
        elif state == "已取消":
            self.status_var.set(f"模型 {model_key} 已取消：{detail}")
        else:
            self.status_var.set(f"模型 {model_key} 下载失败：{detail}")
        self._populate_rows()

    def _post(self, callback, *args) -> None:
        if self._closed:
            return
        try:
            self._window.after(0, callback, *args)
        except (tk.TclError, RuntimeError):
            pass

    def close(self) -> None:
        """关闭管理窗口，不自动取消正在进行的下载。"""
        self._closed = True
        if self._window.winfo_exists():
            self._window.destroy()
