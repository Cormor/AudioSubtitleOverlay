# AudioSubtitleOverlay {{VERSION}}

## 更新 | Update

- 新增 Beta 应用音频来源，尝试跟踪正在发声的应用并直接采集声音。需要 Windows 10 build 20348 或更高版本；部分应用可能不支持。
- Added a beta audio source that attempts to follow an app producing sound and capture it directly. Requires Windows 10 build 20348 or later; some apps may not be supported.

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
