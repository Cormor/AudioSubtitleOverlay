# 声幕 {{VERSION}}

## 更新

- 应用更名为“声幕”，增加声波与字幕图标；保留原有配置和模型目录。
- 常用操作固定在顶部，识别、字幕外观和高级设置分别展示；增加高对比外观预设、从快到准的五档模型组合，以及可单独调整的识别参数。
- 关闭或最小化设置窗口后收进右下角托盘；托盘可恢复设置、显示／隐藏字幕、开始／停止识别及退出。
- 原文与译文成对显示，修复首行被裁掉的问题；有过译文后原文停更，译文区保持显示。
- 翻译后端严格按选择分流，在线模式不先探测本地模型清单。
- 减少字幕合成中的重复缩放，并保留未确认语音及重叠上下文，减少重复识别已经确认的内容。
- 本地打包入口会同步更新安装版，保留旧版本和用户数据；发布完成后可安装 GitHub 上同一份校验通过的正式包。

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
