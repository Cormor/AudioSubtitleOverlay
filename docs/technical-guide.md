# 声幕技术指南

[返回中文首页](../README.md) · [English technical guide](technical-guide.en.md)

## 模型与设备

| 方案 | 语言与用途 | 运行方式及注意事项 |
|---|---|---|
| SenseVoice INT8 | 中文、粤语、英语、日语、韩语；新安装默认 | CPU运行，VAD分段及周期预览，不是模型原生流式。 |
| Whisper tiny/base/small/medium/large-v3/Turbo | 多语言；支持词时间的滚动字幕 | CPU INT8或NVIDIA GPU FP16/INT8_FLOAT16。大模型需要更多内存/显存；量化不保证在所有设备上更快。 |
| tiny.en/base.en/distil-large-v3 | 英语专用 | 不用于中文、日语或韩语。 |
| Zipformer Transducer/CTC | 中英或英语，取决于具体选项 | CPU原生流式，实验选项。短句测试仍有错漏；翻译时需明确源语言。 |

模型分组筛选只改变列表，不自动切换当前模型。高级页的“极速、流畅、均衡、精度优先、长上下文”调整Whisper参数，不替换模型或设备。运行详情显示实际设备、处理时间和积压；这些数字不等于完整发声到显示的延迟。

## 系统声音与Beta模式

主程序面向Windows 10/11 x64。普通系统播放来源捕获所选输出设备的回环声音；麦克风来源捕获输入声音。Beta自动跟踪发声应用使用进程回环，需要Windows 10 build 20348或更新版本，并受应用兼容性限制。没有输入时先使用明确的系统播放设备排查。

## 下载、缓存与离线准备

主程序不内置语音/翻译模型。首次按所选方案准备组件；GPU方案缺少兼容运行库时还需下载对应组件。下载器支持进度、暂停与续传，模型按其类型校验文件。CPU SenseVoice不需要CUDA组件。

| 内容 | Windows位置 |
|---|---|
| 配置 | `%APPDATA%/AudioSubtitleOverlay/settings.json` |
| 语音模型 | `%APPDATA%/AudioSubtitleOverlay/models` |
| 本地翻译模型 | `%APPDATA%/AudioSubtitleOverlay/translation_models` |
| GPU运行组件 | `%APPDATA%/AudioSubtitleOverlay/runtime/cuda12` |
| 组件下载断点 | `%APPDATA%/AudioSubtitleOverlay/downloads/components` |
| 运行日志 | `%APPDATA%/AudioSubtitleOverlay/logs` |

离线使用前，在联网环境完成目标模型及翻译语言对的准备，再使用同一用户数据目录。可选tiny ZIP解压到主程序相同父目录，合并为 `AudioSubtitleOverlay/models/tiny`；它仅对应Whisper tiny，需在界面选择tiny模型。默认SenseVoice和其它Whisper模型仍需各自准备。

本地翻译按语言对准备；是否支持某一语言对以实际模型可用性为准。MyMemory在线翻译会发送待译文字；“本地优先”在允许回退时也可能联网。源/目标语言相同时跳过翻译。

## 阅读、启停与恢复

原文与译文按段落关联；最近64段可回看、复制和导出。滚动到历史时，实时识别仍继续；回到最新可恢复跟随。新句末标点需跨窗口确认才用于不可逆分段，即时草稿仍先显示。

停止后先拒绝新输入和旧回调，待工作线程与模型释放后恢复开始按钮。只改翻译设置不清空已经识别的原文。配置窗口获得焦点时浮窗临时让出置顶，离开恢复用户偏好。

普通启动尊重用户的自动开始设置；`AudioSubtitleOverlay.exe --no-autostart` 可仅打开设置。运行日志按30天/30 MiB上限清理；录音和测试证据不当作普通日志删除。

## 本地诊断与延迟测量

普通启动不保存录音或识别全文。显式诊断命令：

```text
AudioSubtitleOverlay.exe --trace-dir <absolute-output-directory> --trace-seconds 120 --trace-audio --trace-text
python tools/trace_live.py reference <run-directory> --output <run-directory>/reference.json
python tools/trace_live.py analyze <run-directory> --reference <run-directory>/reference.json --output <run-directory>/timing-report.json --csv <run-directory>/phrase-timing.csv
```

诊断默认120秒、最多600秒，默认16 MiB且队列有界。录音和全文开关分别选择，达到上限自动结束；记录错误、丢事件和待写事件，写入失败不阻断字幕。参考工具只使用本地已缓存模型，不下载、不上传。分享前自行检查录音与文字内容。

进程回环使用WASAPI QPC包时钟，普通系统回环使用回调估算，二者要分开报告。文字版本与实际可见行按会话/序号关联；记录的是向Windows提交图像后的时间，不是显示器扫描时间。自动参考词边界仍是估计，精确短语匹配会偏向易识别内容，不可当作准确率；准确率需同段人工参考文本。

## 开发与发布

Windows依赖见 [requirements-windows.txt](../requirements-windows.txt)，单元测试依赖见 [requirements-test.txt](../requirements-test.txt)。

```text
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
build_windows.bat
package_windows.bat
```

构建使用PyInstaller；主包按需准备大模型和GPU组件。发布需已提交的干净源码、准确版本和源码指纹；本地开发包的modified标记不得当作正式包发布。完整流程见[发布与安装](releasing.md)，实现记录见[1.4工程说明](v1.4-engineering.md)。第三方许可见[第三方说明](../THIRD_PARTY_NOTICES.md)。
