# AudioSubtitleOverlay {{VERSION}}

Windows system-audio speech recognition and bilingual subtitle overlay.

Windows 系统音频识别与双语字幕悬浮窗。

## 中文说明

AudioSubtitleOverlay 是 Windows 桌面应用，可采集系统播放回环或麦克风音频，使用本地语音识别，并在可拖动、可调整大小的悬浮窗中显示源文本和译文。

### 主要功能

- 支持系统播放回环和麦克风输入。
- 使用本地语音识别模型处理音频。
- 在悬浮窗中同时显示源文本和译文。
- 支持选择音频来源、源语言、目标语言和识别模型。
- 支持 CPU 模式；检测到 NVIDIA 环境并选择 GPU 时，可自动准备所需运行库。
- 组件下载支持进度、暂停、继续、断点续传和完整性校验。
- 提供独立的 tiny 模型包，便于离线携带默认识别模型。

### 快速开始

1. 下载 `AudioSubtitleOverlay_Portable_20260918.zip`。
2. 解压后保留完整的 `AudioSubtitleOverlay` 文件夹。
3. 需要离线携带 tiny 模型时，再下载 `AudioSubtitleOverlay_tiny_model_20260918.zip`，将它解压到与主程序包相同的父目录，并合并同名的 `AudioSubtitleOverlay` 文件夹。
4. 双击 `AudioSubtitleOverlay\AudioSubtitleOverlay.exe`。
5. 在程序中选择音频来源、语言和模型。

主程序包已经包含应用所需的 Windows 运行文件，不需要在目标电脑预装 Python。请保持主程序目录中的 `_internal` 文件夹完整且原位。

如果本地没有模型，应用会按清单下载并校验默认模型。使用 tiny 模型包可以预先准备识别模型；首次使用 NVIDIA GPU 时，GPU 用户态运行库仍可能需要单独准备，约占用 1.36 GB 用户目录空间，CPU 模式不需要该运行库。

### 下载资产

| 文件 | 用途 | 大小 | SHA256 |
| --- | --- | ---: | --- |
| `AudioSubtitleOverlay_Portable_20260918.zip` | 主程序、可直接双击运行 | {{MAIN_SIZE}} | `{{MAIN_SHA256}}` |
| `AudioSubtitleOverlay_tiny_model_20260918.zip` | 可选 tiny 模型离线包 | {{TINY_SIZE}} | `{{TINY_SHA256}}` |

两个压缩包是独立资产。主程序包约 105.89 MiB，低于 150 MiB；tiny 模型包按需下载，不强制放入主程序包。

### 截图

![AudioSubtitleOverlay 主窗口 / main window](https://raw.githubusercontent.com/Cormor/AudioSubtitleOverlay/main/docs/screenshots/main-window.png)

## English

AudioSubtitleOverlay is a Windows desktop application that captures system playback or microphone audio, performs local speech recognition, and displays source text and translation in a movable, resizable overlay window.

### Highlights

- Supports system playback loopback and microphone input.
- Runs speech recognition with a local model.
- Shows source text and translation in the overlay window.
- Lets users select the audio source, source language, target language, and recognition model.
- Supports CPU mode. When an NVIDIA environment is detected and GPU mode is selected, the application can prepare the required runtime components.
- Component downloads provide progress, pause, resume, HTTP range continuation, and integrity verification.
- Includes a separate tiny-model package for offline distribution of the default recognition model.

### Quick start

1. Download `AudioSubtitleOverlay_Portable_20260918.zip`.
2. Extract it and keep the complete `AudioSubtitleOverlay` directory.
3. For offline distribution of the tiny model, also download `AudioSubtitleOverlay_tiny_model_20260918.zip`, extract it under the same parent directory, and merge the `AudioSubtitleOverlay` directory.
4. Double-click `AudioSubtitleOverlay\AudioSubtitleOverlay.exe`.
5. Select the audio source, languages, and model in the application.

The main package includes the Windows runtime files required by the application; Python does not need to be installed on the target computer. Keep the `_internal` directory in place.

If no local model is available, the application downloads and verifies the default model according to its manifest. The tiny-model package can be used to prepare the recognition model in advance. First-time NVIDIA GPU use may additionally prepare approximately 1.36 GB of user-mode GPU runtime files; CPU mode does not require that runtime.

### Assets

| File | Purpose | Size | SHA256 |
| --- | --- | ---: | --- |
| `AudioSubtitleOverlay_Portable_20260918.zip` | Main portable application | {{MAIN_SIZE}} | `{{MAIN_SHA256}}` |
| `AudioSubtitleOverlay_tiny_model_20260918.zip` | Optional offline tiny-model package | {{TINY_SIZE}} | `{{TINY_SHA256}}` |

The two ZIP files are separate release assets. The main package is about 105.89 MiB and stays below the 150 MiB target; the tiny-model package is optional.

### Screenshot

![AudioSubtitleOverlay main window](https://raw.githubusercontent.com/Cormor/AudioSubtitleOverlay/main/docs/screenshots/main-window.png)

## License and commercial use

The original code is provided under the `AudioSubtitleOverlay Non-Commercial Source-Available License 1.0`. This is a source-available license, not an OSI-approved Open Source License.

- Personal, educational, research, testing, hobby, and other non-commercial use is permitted by default.
- Modifying, integrating, repackaging, or redistributing the software requires either publicly available complete corresponding source code with change notes or prior written authorization.
- Selling, paid distribution, commercial integration, internal business use, revenue-generating services, and other commercial use require prior written authorization.
- Third-party libraries, models, and GPU runtimes remain under their own terms.

See [`LICENSE`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE), [`COMMERCIAL-LICENSE.md`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/COMMERCIAL-LICENSE.md), and [`THIRD_PARTY_NOTICES.md`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/THIRD_PARTY_NOTICES.md).
