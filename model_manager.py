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


@dataclass(frozen=True)
class ModelDefinition:
    """应用支持的一个 faster-whisper 模型。"""

    key: str
    repo_id: str
    display_name: str
    description: str


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
)

MODEL_BY_KEY = {item.key: item for item in MODEL_DEFINITIONS}
MODEL_MARKER = ".audio-subtitle-overlay-model.json"


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
    repository = cache_root / f"models--Systran--faster-whisper-{model_key}"
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
        return path if model_is_complete(path) else None

    candidates = [managed_model_dir(model_key)]
    candidates.extend(_bundled_model_candidates(model_key))
    candidates.extend(_local_huggingface_snapshots(model_key))
    for candidate in candidates:
        if model_is_complete(candidate):
            return candidate
    return None


def model_cache_hint() -> str:
    """返回模型查找和管理位置提示。"""
    return f"模型管理目录：{managed_models_dir()}；缺失模型时会按组件清单准备。"


def _remote_model_files(definition: ModelDefinition) -> tuple[str, list[ComponentFile]]:
    """读取固定远端修订版本及文件元数据，供通用下载器使用。"""
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
        if model_is_complete(managed):
            return LocalModelStatus("已下载", managed, directory_size(managed))
        if managed.exists():
            return LocalModelStatus("下载未完成", managed, directory_size(managed))

        for candidate in _bundled_model_candidates(model_key):
            if model_is_complete(candidate):
                return LocalModelStatus("已内置", candidate, directory_size(candidate))
        for candidate in _local_huggingface_snapshots(model_key):
            if model_is_complete(candidate):
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
        if static_spec is not None:
            self._downloader.ensure(static_spec, on_progress=on_progress)
        else:
            self._downloader.ensure_factory(
                definition.key,
                lambda: _build_model_component(definition),
                on_progress=on_progress,
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
) -> Path:
    """为后台识别准备模型，保持模型管理窗口与启动流程共用下载任务。"""
    return ModelManager().ensure_model(model_key, on_progress=on_progress)


class ModelManagerWindow:
    """显示模型说明、大小、下载进度并控制下载任务。"""

    def __init__(self, master: tk.Misc, manager: ModelManager | None = None) -> None:
        self.manager = manager or ModelManager()
        self._closed = False
        self._remote_sizes: dict[str, int | None] = {}
        self._progress: dict[str, tuple[int, int | None, str, float | None, str]] = {}
        self._runtime_progress: DownloadProgress | None = None
        self._window = tk.Toplevel(master)
        self._window.title("语音模型管理")
        self._window.geometry("1120x600")
        self._window.minsize(920, 480)
        self._window.protocol("WM_DELETE_WINDOW", self.close)

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(
            container,
            text="语音模型管理",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            container,
            text=(
                f"下载目录：{managed_models_dir()}。支持暂停、继续和断点续传；识别启动时会按配置准备缺失模型。"
            ),
            foreground="#5c6773",
        ).pack(anchor="w", pady=(4, 10))

        tree_frame = ttk.Frame(container)
        tree_frame.pack(fill="both", expand=True)
        columns = ("model", "status", "progress", "size", "local", "description")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "model": "模型",
            "status": "状态",
            "progress": "进度",
            "size": "模型大小",
            "local": "本地大小",
            "description": "说明",
        }
        widths = {"model": 90, "status": 100, "progress": 190, "size": 110, "local": 110, "description": 430}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

        action_frame = ttk.Frame(container)
        action_frame.pack(fill="x", pady=(10, 0))
        self.download_button = ttk.Button(action_frame, text="下载选中模型", command=self._download_selected)
        self.download_button.pack(side="left")
        self.pause_button = ttk.Button(action_frame, text="暂停下载", command=self._pause_selected, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.resume_button = ttk.Button(action_frame, text="继续下载", command=self._resume_selected, state="disabled")
        self.resume_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(action_frame, text="取消下载", command=self._cancel_selected, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.delete_button = ttk.Button(action_frame, text="删除本地下载", command=self._delete_selected)
        self.delete_button.pack(side="left", padx=(8, 0))
        ttk.Button(action_frame, text="刷新状态", command=self.refresh).pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="正在读取模型状态……")
        ttk.Label(action_frame, textvariable=self.status_var, foreground="#245b9e").pack(
            side="left", padx=(16, 0)
        )

        runtime_frame = ttk.LabelFrame(container, text="运行依赖", padding=8)
        runtime_frame.pack(fill="x", pady=(10, 0))
        self.runtime_status_var = tk.StringVar(value="GPU运行库：未准备")
        ttk.Label(runtime_frame, textvariable=self.runtime_status_var).pack(side="left")
        self.prepare_gpu_button = ttk.Button(
            runtime_frame,
            text="准备GPU运行库",
            command=self._prepare_gpu_runtime,
        )
        self.prepare_gpu_button.pack(side="left", padx=(12, 0))
        self.pause_gpu_button = ttk.Button(
            runtime_frame,
            text="暂停",
            command=self._pause_gpu_runtime,
            state="disabled",
        )
        self.pause_gpu_button.pack(side="left", padx=(8, 0))
        self.resume_gpu_button = ttk.Button(
            runtime_frame,
            text="继续",
            command=self._resume_gpu_runtime,
            state="disabled",
        )
        self.resume_gpu_button.pack(side="left", padx=(8, 0))

        self._populate_rows()
        self._refresh_remote_sizes()
        self._poll_runtime_status()

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
        local_size = status.size
        return (
            definition.display_name,
            state_text,
            progress_text,
            format_bytes(remote_size),
            format_bytes(local_size) if local_size else "—",
            definition.description,
        )

    def _selected_key(self) -> str | None:
        selection = self.tree.selection()
        return str(selection[0]) if selection else None

    def _selection_changed(self, _event=None) -> None:
        key = self._selected_key()
        downloading = bool(key and self.manager.is_downloading(key))
        progress = self._progress.get(key) if key else None
        paused = bool(progress and progress[4] in {"已暂停", "暂停中"})
        self.download_button.configure(state="disabled" if downloading else "normal")
        self.cancel_button.configure(state="normal" if downloading else "disabled")
        self.pause_button.configure(state="normal" if downloading and not paused else "disabled")
        self.resume_button.configure(state="normal" if downloading and paused else "disabled")
        if key:
            self.delete_button.configure(
                state="normal" if managed_model_dir(key).exists() else "disabled"
            )
        else:
            self.delete_button.configure(state="disabled")

    def _refresh_remote_sizes(self) -> None:
        def worker():
            for definition in MODEL_DEFINITIONS:
                try:
                    size = self.manager.query_remote_size(definition.key)
                except Exception:
                    size = None
                self._post(self._remote_size_ready, definition.key, size)

        threading.Thread(target=worker, name="模型大小查询", daemon=True).start()

    def _remote_size_ready(self, model_key: str, size: int | None) -> None:
        self._remote_sizes[model_key] = size
        self._populate_rows()

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
        from runtime_components import prepare_component

        if self.manager._downloader.is_active("runtime.cuda12"):
            return

        def worker() -> None:
            try:
                prepare_component("runtime.cuda12", on_progress=self._runtime_progress_callback)
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
        """轮询自动启动流程产生的GPU运行库下载状态。"""
        if self._closed:
            return
        status = self.manager._downloader.status("runtime.cuda12")
        if status is not None:
            self._apply_runtime_progress(status)
        try:
            self._window.after(500, self._poll_runtime_status)
        except (tk.TclError, RuntimeError):
            pass

    def _refresh_runtime_controls(self) -> None:
        active = self.manager._downloader.is_active("runtime.cuda12")
        progress = self._runtime_progress or self.manager._downloader.status("runtime.cuda12")
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
