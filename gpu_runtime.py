"""为本应用加载GPU用户态运行库。"""

import os
from pathlib import Path
import sys

from settings import application_data_dir

_dll_handles = []
_initialized = False


def configure_gpu_runtime() -> list[str]:
    """只调整当前进程的 DLL 搜索路径，不修改系统环境变量或驱动。"""
    global _initialized
    roots = [Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))]
    executable = getattr(sys, 'executable', '')
    if executable:
        roots.append(Path(executable).resolve().parent)
    roots.append(application_data_dir() / 'runtime' / 'cuda12')
    roots.extend(Path(path) for path in sys.path if path and Path(path).is_dir())
    paths = []
    for root in roots:
        for package in ('cublas', 'cudnn', 'cuda_nvrtc'):
            directory = root / 'nvidia' / package / 'bin'
            if directory.is_dir() and str(directory) not in paths:
                paths.append(str(directory))
    if not _initialized and sys.platform == 'win32':
        for directory in paths:
            _dll_handles.append(os.add_dll_directory(directory))
        # CTranslate2 及 cuDNN 的延迟加载也需要能在当前进程中找到相邻运行库。
        if paths:
            os.environ['PATH'] = os.pathsep.join(paths + [os.environ.get('PATH', '')])
        _initialized = True
    return paths
