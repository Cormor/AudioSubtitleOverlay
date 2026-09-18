"""运行时模型和外部依赖组件的准备入口。"""

from __future__ import annotations

import ctypes
from functools import lru_cache
import logging
from pathlib import Path
import sys
from typing import Callable

from download_manager import (
    ComponentSpec,
    DownloadManager,
    DownloadProgress,
    load_component_manifest,
)
from settings import application_data_dir


LOGGER = logging.getLogger(__name__)


def _manifest_candidates() -> list[Path]:
    """返回打包目录、EXE目录和源码目录中的组件清单候选路径。"""
    roots: list[Path] = []
    temporary_root = getattr(sys, "_MEIPASS", "")
    if temporary_root:
        roots.append(Path(temporary_root))
    executable = getattr(sys, "executable", "")
    if executable:
        roots.append(Path(executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)
    result: list[Path] = []
    for root in roots:
        candidate = root / "component_manifest.json"
        if candidate not in result:
            result.append(candidate)
    return result


def _manifest_path() -> Path:
    """取得当前交付版本的组件清单。"""
    for candidate in _manifest_candidates():
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("未找到component_manifest.json。")


@lru_cache(maxsize=1)
def component_specs() -> dict[str, ComponentSpec]:
    """加载组件清单并解析到当前用户的可写目录。"""
    return load_component_manifest(_manifest_path(), application_data_dir())


@lru_cache(maxsize=1)
def component_download_manager() -> DownloadManager:
    """返回共享下载管理器，避免模型和GPU组件重复建立任务。"""
    return DownloadManager(application_data_dir() / "downloads" / "components")


def component_spec(key: str) -> ComponentSpec:
    """取得指定组件定义。"""
    try:
        return component_specs()[key]
    except KeyError as error:
        raise RuntimeError(f"组件清单中不存在：{key}") from error


def prepare_component(
    key: str,
    on_progress: Callable[[DownloadProgress], None] | None = None,
) -> Path:
    """同步准备组件，调用方应位于后台线程。"""
    spec = component_spec(key)
    return component_download_manager().ensure(spec, on_progress=on_progress)


def nvidia_driver_available() -> bool:
    """只检测NVIDIA驱动接口，不把驱动接口当作CUDA用户态运行库。"""
    if sys.platform != "win32":
        return False
    try:
        ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return False
    return True
