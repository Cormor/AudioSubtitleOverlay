"""把Windows精简版目录打包为可直接解压运行的ZIP。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import zipfile


USAGE_TEXT = """AudioSubtitleOverlay 精简版使用说明

1. 解压后保持 AudioSubtitleOverlay 文件夹结构不变。
2. 双击 AudioSubtitleOverlay\\AudioSubtitleOverlay.exe 即可运行，不需要安装Python。
3. 不要单独移动或删除 _internal 文件夹；它包含应用自身的Windows运行时文件。
4. 首次开始识别时，如果用户目录没有模型，应用会自动下载默认的 tiny 模型。
5. 检测到 NVIDIA 驱动且选择自动GPU时，应用会自动准备 CUDA 12 用户态运行库。
   GPU运行库约1.36GB，放在用户目录，不占用本压缩包体积；CPU模式不需要它。
6. 下载界面会显示进度、速度，并支持暂停、继续和HTTP断点续传。网络中断后重新运行会继续保留的断点。
7. 用户级文件位置：
   - 模型：%APPDATA%\\AudioSubtitleOverlay\\models
   - GPU运行库：%APPDATA%\\AudioSubtitleOverlay\\runtime\\cuda12
   - 下载断点：%APPDATA%\\AudioSubtitleOverlay\\downloads\\components
8. 应用会校验组件的文件大小和SHA256；校验不通过的文件不会安装。
"""


def _iter_files(source: Path):
    """按稳定顺序枚举需要写入压缩包的文件。"""
    yield from sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix())


def build_archive(source: Path, output: Path) -> None:
    """写出包含应用目录和中文使用说明的ZIP文件。"""
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"构建目录不存在：{source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in _iter_files(source):
            archive.write(path, Path("AudioSubtitleOverlay") / path.relative_to(source))
        archive.writestr("AudioSubtitleOverlay/使用说明.txt", USAGE_TEXT)


def main() -> int:
    """解析命令行并创建压缩包。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="创建AudioSubtitleOverlay精简版Windows压缩包")
    parser.add_argument("--source", type=Path, default=Path("dist_minimal/AudioSubtitleOverlay"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d")
        output = Path("releases") / f"AudioSubtitleOverlay_精简版_{stamp}.zip"
    build_archive(args.source, output)
    print(f"压缩包：{output.resolve()}")
    print(f"大小：{output.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
