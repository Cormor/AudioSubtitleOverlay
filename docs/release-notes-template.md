# AudioSubtitleOverlay {{VERSION}}

Windows 双语字幕悬浮窗，解压后双击运行。

AudioSubtitleOverlay is a Windows bilingual subtitle overlay. Extract the package and double-click the executable.

## 修复与更新 | Fixes and updates

- 修复本地翻译无法正常启动的问题。
- 本地翻译模型首次使用时自动下载，准备完成后可离线使用。
- 本地翻译模型加载后在进程内复用，减少重复加载和等待。
- 优化运行中配置切换，减少不必要的识别模型和运行库检查。
- 主界面支持上下滚动，较小屏幕也可以查看全部设置。
- 减少音频积压造成的卡顿和重复字幕。

- Fixed local translation startup.
- Local translation models are downloaded on first use and work offline after preparation.
- Loaded translation models are reused within the process.
- Reduced unnecessary model and runtime checks while changing settings.
- Added vertical scrolling to the main settings window.
- Reduced stutter and duplicate subtitles caused by audio backlog.

## 下载与运行 | Download and run

1. 下载并解压 `{{MAIN_NAME}}`。
2. 保留完整的 `AudioSubtitleOverlay` 文件夹及其中的 `_internal` 文件夹。
3. 需要预先携带 tiny 语音模型时，将 `{{TINY_NAME}}` 解压到主程序 ZIP 解压后的同级目录。
4. 双击 `AudioSubtitleOverlay\AudioSubtitleOverlay.exe`。

主程序不要求目标电脑预装 Python。语音识别和本地翻译模型按需准备；本地翻译模型准备完成后，对应语言对可以离线使用。GPU 运行库只在选择 GPU 且本机缺少对应组件时按需准备，CPU 模式不需要该运行库。

The main package does not require Python on the target computer. Speech and local translation models are prepared as needed. After a local translation model is prepared, that language pair can work offline. GPU runtime components are prepared only when GPU mode is selected and they are missing.

## 发布文件 | Assets

- `{{MAIN_NAME}}`：主程序，{{MAIN_SIZE}}。
- `{{TINY_NAME}}`：可选 tiny 语音模型，{{TINY_SIZE}}。
- `SHA256SUMS.txt`：发布文件校验值。

SHA256:

```text
{{MAIN_SHA256}}  {{MAIN_NAME}}
{{TINY_SHA256}}  {{TINY_NAME}}
```

许可证 | License: [`LICENSE`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE)。商业使用及修改、集成、重新打包和再分发请先阅读许可证条款。
