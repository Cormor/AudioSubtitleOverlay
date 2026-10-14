# AudioSubtitleOverlay {{VERSION}}

## 更新 | Update

- 改善双语字幕阅读：固定文字基线和原文、译文分区，修复换行续排及重叠识别窗口造成的重复追加。
- 原文与译文均支持一行最小高度，扩大缩放手柄；滚动条支持拖动、轨道点击和回到最新字幕。
- 手动指定源语言时按源语言与目标语言是否相同启用译文区；自动检测时，原文持续更新且连续 5 秒没有译文输出才收起，译文恢复后重新展开。
- 浮窗保留最近 64 段确认字幕，复用历史换行缓存；修复过时译文挂接到新原文及窗口关闭后程序未退出的问题。
- 优先复用已安装的本地翻译模型，支持语言模型清单缓存，并减少连续翻译失败时的重复报错。

- Improves bilingual reading with fixed text baselines and separate language areas; fixes wrapping and repeated text from overlapping recognition windows.
- Supports one-row minimum heights, a larger resize grip, and a draggable scrollbar for history review.
- Uses the selected language pair to enable translation in manual mode. Automatic mode waits for five seconds of source updates without translation output before collapsing the area, and restores it when translation resumes.
- Keeps the latest 64 confirmed blocks and reuses wrapping caches; fixes stale translation assignment and application shutdown.
- Reuses installed local translation models, caches the model index, and reduces repeated error reporting.

## 下载与运行 | Download and run

1. 下载并解压主程序 ZIP。
2. 需要离线使用 tiny 语音模型时，将 tiny 模型 ZIP 解压到同一父目录。
3. 双击 `AudioSubtitleOverlay\AudioSubtitleOverlay.exe`。

主程序不要求预装 Python。主程序和 tiny 模型包的校验值见 `SHA256SUMS.txt`。商业使用及修改、再分发请先阅读 [`LICENSE`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE)。

The main package does not require Python to be installed. SHA-256 checksums for both packages are in `SHA256SUMS.txt`. See [`LICENSE`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE) before commercial use or redistribution.

## 发布文件 | Assets

- `{{MAIN_NAME}}`：主程序，{{MAIN_SIZE}}。
- `{{TINY_NAME}}`：可选 tiny 语音模型，{{TINY_SIZE}}。
- `SHA256SUMS.txt`：发布文件校验值。

```text
{{MAIN_SHA256}}  {{MAIN_NAME}}
{{TINY_SHA256}}  {{TINY_NAME}}
```
