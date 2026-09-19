"""查找并加载本机已有的 NVIDIA 用户态运行库。"""

import os
from pathlib import Path
import sys

from settings import application_data_dir

_GPU_PACKAGES = ('cublas', 'cudnn', 'cuda_nvrtc')
_REQUIRED_DLLS = (
    'cublas64_12.dll',
    'cublasLt64_12.dll',
    'cudnn64_9.dll',
    'nvrtc64_120_0.dll',
    'nvrtc-builtins64_129.dll',
)
_dll_handles: dict[str, object] = {}
_runtime_directories: tuple[str, ...] | None = None
_runtime_available: bool | None = None


def _append_unique(paths: list[Path], candidate: Path) -> None:
    """加入存在的目录，并保持搜索顺序稳定。"""
    try:
        resolved = candidate.expanduser().resolve()
    except OSError:
        return
    if resolved.is_dir() and resolved not in paths:
        paths.append(resolved)


def _candidate_roots() -> list[Path]:
    """收集应用目录、共享目录、Python环境和系统 CUDA 目录。"""
    roots: list[Path] = []
    temporary_root = getattr(sys, '_MEIPASS', '')
    if temporary_root:
        _append_unique(roots, Path(temporary_root))
    executable = getattr(sys, 'executable', '')
    if executable:
        executable_path = Path(executable).resolve()
        _append_unique(roots, executable_path.parent)
        for parent in executable_path.parents:
            # 便携版常位于项目目录的 dist_minimal 子目录中；只在本机存在时复用相邻环境。
            _append_unique(roots, parent / '.venv' / 'Lib' / 'site-packages')
            _append_unique(roots, parent / 'venv' / 'Lib' / 'site-packages')
    module_root = Path(__file__).resolve().parent
    _append_unique(roots, module_root)
    for parent in module_root.parents:
        _append_unique(roots, parent / '.venv' / 'Lib' / 'site-packages')
        _append_unique(roots, parent / 'venv' / 'Lib' / 'site-packages')

    # 应用自己的共享安装位置放在本机环境之后，避免重复下载。
    _append_unique(roots, application_data_dir() / 'runtime' / 'cuda12')

    for variable in ('CUDA_PATH', 'CUDA_HOME'):
        value = os.environ.get(variable, '').strip()
        if value:
            _append_unique(roots, Path(value))

    for value in os.environ.get('PATH', '').split(os.pathsep):
        if not value.strip():
            continue
        path = Path(value)
        try:
            has_gpu_dll = any((path / name).is_file() for name in _REQUIRED_DLLS)
        except OSError:
            has_gpu_dll = False
        if has_gpu_dll:
            _append_unique(roots, path)
    for value in sys.path:
        if value:
            _append_unique(roots, Path(value))
    return roots


def runtime_dll_directories(refresh: bool = False) -> list[str]:
    """返回可能包含 CUDA DLL 的目录，供当前进程加载。"""
    global _runtime_directories
    if _runtime_directories is not None and not refresh:
        return list(_runtime_directories)
    directories: list[Path] = []
    for root in _candidate_roots():
        _append_unique(directories, root)
        _append_unique(directories, root / 'bin')
        for package in _GPU_PACKAGES:
            _append_unique(directories, root / 'nvidia' / package / 'bin')
    _runtime_directories = tuple(str(path) for path in directories)
    return list(_runtime_directories)


def configure_gpu_runtime(refresh: bool = False) -> list[str]:
    """只调整当前进程的 DLL 搜索路径，不修改系统环境变量或驱动。"""
    global _runtime_available
    if refresh:
        _runtime_available = None
    paths = runtime_dll_directories(refresh=refresh)
    if sys.platform == 'win32':
        for directory in paths:
            if directory not in _dll_handles:
                _dll_handles[directory] = os.add_dll_directory(directory)
        # CTranslate2 及 cuDNN 的延迟加载也需要在当前进程的 PATH 中找到相邻 DLL。
        if paths:
            existing = os.environ.get('PATH', '').split(os.pathsep)
            merged = paths + [item for item in existing if item and item not in paths]
            os.environ['PATH'] = os.pathsep.join(merged)
    return paths


def gpu_runtime_available(refresh: bool = False) -> bool:
    """实际尝试加载所需 DLL，确认当前进程可以使用本机 GPU 运行库。"""
    global _runtime_available
    if sys.platform != 'win32':
        return False
    if _runtime_available is not None and not refresh:
        return _runtime_available
    configure_gpu_runtime(refresh=refresh)
    try:
        import ctypes

        for name in _REQUIRED_DLLS:
            ctypes.WinDLL(name)
    except OSError:
        _runtime_available = False
        return False
    _runtime_available = True
    return True
