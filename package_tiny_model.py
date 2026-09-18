"""把固定版本的tiny模型打包为可与主程序合并的独立ZIP。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import zipfile


USAGE_TEXT = """tiny模型离线文件包

将本压缩包解压到主程序压缩包解压后的同级目录，保持两个压缩包都使用 AudioSubtitleOverlay 文件夹。
合并后目录应为 AudioSubtitleOverlay\\models\\tiny\\model.bin 等文件。
再次双击 AudioSubtitleOverlay\\AudioSubtitleOverlay.exe 时，程序会优先使用该目录中的模型，不再下载 tiny。

模型来源：Systran/faster-whisper-tiny
固定提交：d90ca5fe260221311c53c58e660288d3deb8d356
程序启动时仍会校验文件大小和SHA256。
"""


def build_archive(source: Path, output: Path) -> None:
    """写出包含模型和离线使用说明的ZIP文件。"""
    source = source.resolve()
    output = output.resolve()
    required = (source / "config.json", source / "model.bin", source / "tokenizer.json", source / "vocabulary.txt")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"模型文件不存在：{', '.join(missing)}")
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
        for path in required:
            archive.write(path, Path("AudioSubtitleOverlay") / "models" / "tiny" / path.name)
        archive.writestr("AudioSubtitleOverlay/使用说明_tiny模型.txt", USAGE_TEXT)


def main() -> int:
    """解析命令行并创建tiny模型压缩包。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="创建tiny模型离线文件包")
    parser.add_argument("--source", type=Path, default=Path("assets/tiny"))
    parser.add_argument("--output", type=Path, default=Path("releases/AudioSubtitleOverlay_tiny模型_20260918.zip"))
    args = parser.parse_args()
    build_archive(args.source, args.output)
    print(f"压缩包：{args.output.resolve()}")
    print(f"大小：{args.output.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
