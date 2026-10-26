"""把固定版本的tiny模型打包为可与主程序合并的独立ZIP。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import zipfile


USAGE_TEXT = """Whisper tiny 离线模型 / Offline model files

本包不含主程序，也不包含默认SenseVoice模型。
1. 先完整解压主程序ZIP。
2. 将本模型ZIP解压到同一父目录，合并两个 AudioSubtitleOverlay 文件夹。
3. 合并后应有 AudioSubtitleOverlay/models/tiny/model.bin 等文件。
4. 打开主程序，在“模型”中手动选择 Whisper Tiny。选择其它模型仍需各自准备文件。

This package contains Whisper tiny only, not the app or the default SenseVoice model.
Extract the portable app first, then extract this ZIP into the same parent folder.
Merge the two AudioSubtitleOverlay folders. You should have
AudioSubtitleOverlay/models/tiny/model.bin beside the app's other files.
Run the app and manually select Whisper Tiny in 模型 (model).

模型来源 / Source: Systran/faster-whisper-tiny
固定提交 / Revision: d90ca5fe260221311c53c58e660288d3deb8d356
程序会校验文件大小与SHA256 / File size and SHA256 are checked before use.
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
