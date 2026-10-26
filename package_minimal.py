"""把Windows精简版目录打包为可直接解压运行的ZIP。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import zipfile


USAGE_TEXT = """声幕 · 实时字幕与翻译 / Shengmu · Live Captions and Translation

中文使用指南
1. 完整解压主程序ZIP，打开 AudioSubtitleOverlay 文件夹，双击 AudioSubtitleOverlay.exe。
   无需安装Python；保留 _internal 文件夹，它是程序运行所需的文件。
2. 在“音源”选择电脑正在使用的扬声器/耳机，或选择麦克风。
3. 选择“声音语言”。首次使用可保留默认 SenseVoice INT8（中、粤、英、日、韩，CPU运行）。
4. 需要翻译时选择目标语言；只看原文可取消“显示译文”。相同源/目标语言不会调用翻译。
5. 点击“开始字幕”。首次会按所选方案下载缺少的模型；准备完成后本地识别可离线使用。
6. 在“字幕外观”调整字号、颜色、透明度。关闭/最小化设置窗口会收进托盘，字幕继续运行。
7. 点击托盘图标恢复设置；Ctrl+Enter开始/停止，Ctrl+Q退出（设置窗口获得焦点时）。
8. 主窗口支持复制或导出最近64段字幕。实时文字会先出草稿，再随语音修订。
9. Beta自动跟踪发声应用有系统版本和兼容限制；无声音时改选明确的系统播放设备。
10. 本地翻译首次需下载对应语言模型；MyMemory在线翻译会发送待译文字。
11. 可选tiny模型包仅用于手动选择Whisper tiny，不是默认SenseVoice的离线包。
12. 用户配置、模型和运行组件保存在用户应用数据目录，升级会保留；详细位置见技术文档。

English quick start (the app interface is currently Chinese)
1. Extract the whole portable ZIP. Run AudioSubtitleOverlay/AudioSubtitleOverlay.exe.
   No Python installation is required. Keep the _internal folder with the executable.
2. Select your playback device or microphone in 音源 (audio source).
3. Set 声音语言 (spoken language). The default SenseVoice INT8 runs on CPU and supports
   Mandarin Chinese, Cantonese, English, Japanese, and Korean.
4. Choose a translation language, or turn off 显示译文 to show only the original text.
5. Click 开始字幕 (start captions). Missing models are downloaded on first use.
   Local recognition can run offline once its model has been prepared.
6. Use 字幕外观 (appearance) to adjust font size, colors, and transparency.
7. Closing/minimizing settings keeps captions running in the tray. Click the tray icon
   to restore settings. With settings focused: Ctrl+Enter starts/stops; Ctrl+Q exits.
8. Copy or export the latest 64 subtitle segments from the main window.
9. Online MyMemory translation sends translation text to that service. Local translation
   needs its language models first. The optional tiny ZIP is for Whisper tiny only.

中文图文指南 / Illustrated guides:
https://github.com/Cormor/AudioSubtitleOverlay
English: https://github.com/Cormor/AudioSubtitleOverlay/blob/main/README.en.md
Technical details: https://github.com/Cormor/AudioSubtitleOverlay/blob/main/docs/technical-guide.md
"""


def _iter_files(source: Path):
    """按稳定顺序枚举需要写入压缩包的文件。"""
    for item in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        # 使用包内说明替换构建目录可能残留的旧说明，避免ZIP出现重名条目。
        if item.relative_to(source).as_posix() in {"使用说明.txt", "版本.txt", "LICENSE", "THIRD_PARTY_NOTICES.md", "COMMERCIAL-LICENSE.md"}:
            continue
        yield item


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
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "COMMERCIAL-LICENSE.md"):
            archive.writestr(f"AudioSubtitleOverlay/{name}", Path(__file__).resolve().with_name(name).read_text(encoding="utf-8"))
        version_file = Path(__file__).resolve().with_name("VERSION")
        version = version_file.read_text(encoding="utf-8").strip()
        if not version:
            raise ValueError(f"版本文件为空：{version_file}")
        archive.writestr("AudioSubtitleOverlay/版本.txt", f"版本：v{version}\n")


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
