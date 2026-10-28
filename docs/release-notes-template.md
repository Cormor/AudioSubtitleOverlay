# 声幕 / Shengmu {{VERSION}}

[中文图文指南](https://github.com/Cormor/AudioSubtitleOverlay#readme) · [English guide](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/README.en.md)

## 中文

### 本次更新

- 新安装默认SenseVoice INT8：中文、粤语、英语、日语、韩语，CPU即可运行；已有用户的模型和设置保留。
- 增加Whisper Turbo、英语专用模型及实验Zipformer流式选项，按语言、资源和用途选择。
- 主窗口改为紧凑首屏，常用设置、输入电平和最近字幕更容易找到；增加准备、等待声音、识别和停止等明确状态及排查引导。
- 模型分组筛选保留当前模型；设置获得焦点时字幕窗临时让位，支持Tab与翻页键浏览。
- 改善跨窗口句界确认，减少一次错误标点造成的字幕碎片；修复边界重复单字与焦点文字颜色。
- 最近64段字幕可复制、导出TXT；增加显式开启、限定时长和容量的本地延迟诊断。
- 首页重写为中英图文下载与使用指南，技术细节移至独立文档。

### 下载与首次使用

1. 下载 **`{{MAIN_NAME}}`**，完整解压。
2. 打开 `AudioSubtitleOverlay` 文件夹，双击 **`AudioSubtitleOverlay.exe`**，保留旁边的 `_internal` 文件夹。无需安装Python。
3. 选择系统播放设备或麦克风、声音语言与译文语言，点击“开始字幕”。首次会按需下载缺少的模型。

**`{{TINY_NAME}}`是可选Whisper tiny模型包，不是主程序，也不包含默认SenseVoice模型。** 使用它时解压到主程序相同父目录，并手动选择tiny模型。GitHub的“Source code”下载是源码。

适用于Windows 10/11 x64；Beta进程跟踪需Windows 10 build 20348或更新系统，部分应用可能不支持。程序界面目前为中文。模型准备后本地识别可离线；MyMemory在线翻译会发送待译文字。Zipformer仍属实验选项，识别文字可能有错漏。

## English

### What's new

- SenseVoice INT8 is the default for new setups: Mandarin Chinese, Cantonese, English, Japanese, and Korean on CPU. Existing model choices and settings are preserved.
- Added Whisper Turbo, English-only choices, and experimental native-streaming Zipformer options.
- A more compact main window, clearer capture/setup/stop states, model filters that retain the active choice, keyboard navigation, and an overlay that yields while Settings has focus.
- More stable sentence-boundary confirmation, duplicate-boundary fixes, and readable focused dropdown values.
- Copy/export the latest 64 segments; optional bounded local timing traces.
- Illustrated Chinese and English download/start guides, with technical details in separate documents.

### Download and start

1. Download **`{{MAIN_NAME}}`** and extract the whole ZIP.
2. Run **`AudioSubtitleOverlay/AudioSubtitleOverlay.exe`**, keeping the `_internal` folder alongside it. No Python installation is required.
3. Select playback or microphone audio, the spoken language, and optional translation language; click **开始字幕** (start captions). Missing models download on first use.

**`{{TINY_NAME}}` is an optional Whisper tiny model, not the app or an offline SenseVoice bundle.** Extract it beside the app folder and manually select tiny. The automatic “Source code” downloads are source archives.

For Windows 10/11 x64. Beta app tracking requires Windows 10 build 20348 or later and has compatibility limits. **The UI is currently Chinese.** Local recognition can work offline after setup; online MyMemory translation sends translation text to that service. Zipformer is experimental and can omit or misrecognize words.

## Assets / 发布文件

| File | Contents | Size |
|---|---|---|
| `{{MAIN_NAME}}` | Windows app / 主程序 | {{MAIN_SIZE}} |
| `{{TINY_NAME}}` | Optional Whisper tiny / 可选模型 | {{TINY_SIZE}} |
| `SHA256SUMS.txt` | Download checksums / 校验值 | — |

```text
{{MAIN_SHA256}}  {{MAIN_NAME}}
{{TINY_SHA256}}  {{TINY_NAME}}
```

项目自有代码为非商业源码可见许可；商业使用及修改、再分发须遵守[LICENSE](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE)。第三方组件适用各自条款。

The project's own code is non-commercial source-available. See [LICENSE](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE) for commercial-use, modification, and redistribution terms. Third-party components retain their own terms.
