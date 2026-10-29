# 声幕 / Shengmu 2.2.1

## 中文

本次更新改善设置页切换与字幕绘制，保留现有界面、控件和功能。

- 固定底色的按钮、输入框和页签在创建时完成图片合成，减少 Windows Tk 重绘时的透明混合。
- 页面重新显示时跳过已完成的输入绑定，减少重复控件遍历。
- 主线程回调批次增加协作时间预算，保留消息顺序，让输入与绘制获得执行机会。
- 字幕背景与圆角蒙版只缓存当前一组，减少每帧重复生成图像。

同一 Windows 机器、五个页面、深浅主题的固定场景中，切页处理的中位耗时从 80.9/76.0ms 降至 52.1/49.7ms，降低约 35%；常规字幕 CPU 图像合成从 5.97ms 降至 4.70ms，降低约 21%。这些是 Tk 处理和图像合成的测量，不是声卡到屏幕或物理点击到显示的总延迟；实际效果随系统负载变化。

验证覆盖 586 项 Windows 测试，以及八类字幕绘制场景共 344 帧完整 RGBA 比较。控件状态、鼠标/键盘切页、下拉展开与取消、滚动及返回页面的位置保留均已检查。升级保留已有模型、音源、语言与个人样式设置。

### 下载与升级

1. 下载 **`AudioSubtitleOverlay_Portable_v2.2.1.zip`** 并完整解压。
2. 退出旧版，打开新文件夹中的 **`AudioSubtitleOverlay/AudioSubtitleOverlay.exe`**，保留旁边的 `_internal` 文件夹。
3. 可选的 **`AudioSubtitleOverlay_tiny_model_v2.2.1.zip`** 仅包含 Whisper tiny 模型；它不是主程序，也不包含默认 SenseVoice 模型。模型按需准备。

适用于 Windows 10/11 x64，程序界面为中文。文件校验值见同页 `SHA256SUMS.txt`。项目自有代码和第三方组件继续遵守各自[许可证](https://github.com/Cormor/AudioSubtitleOverlay/blob/v2.2.1/LICENSE)。

## English

This release improves settings-page switching and caption rendering while preserving the existing interface, controls, and features.

- Precompose fixed-background button, input-field, and tab images to reduce Windows Tk alpha-blending work.
- Skip already-installed input bindings when pages are mapped again.
- Give each main-thread callback batch a cooperative time budget while retaining message order.
- Cache one current caption background and rounded mask instead of recreating them for every frame.

In a fixed benchmark on the same Windows PC, median processing time across five pages fell from 80.9/76.0ms to 52.1/49.7ms in dark/light themes, about 35%. Median regular CPU caption-image composition fell from 5.97ms to 4.70ms, about 21%. These measure Tk processing and image composition, not total audio-to-screen or physical click-to-display latency. Results depend on system load.

Validation covers 586 Windows tests and exact RGBA comparisons across 344 frames in eight rendering scenarios. Control states, mouse/keyboard page switching, dropdown opening/canceling, scrolling, and page scroll-position retention were checked. Existing model, audio-source, language, and appearance settings are preserved during upgrade.

Download **`AudioSubtitleOverlay_Portable_v2.2.1.zip`**, extract the entire archive, close the previous version, and run **`AudioSubtitleOverlay/AudioSubtitleOverlay.exe`** with `_internal` alongside it. The optional **`AudioSubtitleOverlay_tiny_model_v2.2.1.zip`** contains only Whisper tiny, not the application or the default SenseVoice model. See `SHA256SUMS.txt` for checksums. Windows 10/11 x64; the interface is Chinese.
