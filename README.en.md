# Shengmu · Live Captions and Translation

The working source version is 2.2.0. The uploaded 2.1 source checkpoint and the published 1.4.1 download retain their own version history. See [controls, custom parameters, and mouse passthrough](docs/controls-input-2.2.0.md), the [streaming translation guide](docs/streaming-translation.md), and [engineering notes](docs/refactor-2.0.md). Source changes and installed-build verification are recorded separately.

<p align="center"><img src="assets/brand/icon.png" alt="Shengmu icon" width="88"></p>

<p align="center"><strong>Turn the sound on your PC into captions you can read.</strong></p>
<p align="center"><a href="README.md">简体中文</a> · English · <a href="https://github.com/Cormor/AudioSubtitleOverlay/releases/latest">Download</a> · <a href="docs/technical-guide.en.md">Technical guide</a></p>

Follow videos, livestreams, and online classes, or transcribe speech from a microphone. Shengmu displays live captions in a floating Windows desktop window, with optional translations below the original text. You do not need to transcribe a video file first or install Python.

**The app interface is currently in Chinese.** This guide includes the Chinese labels you will see. The executable and repository retain the name AudioSubtitleOverlay.

![Download and extract, choose audio and language, then start captions](docs/images/quick-start.svg)

## What you can do

- **Read while listening:** transcribe PC playback or microphone input. Draft captions are revised as more speech arrives.
- **Read translations as speech continues:** translation starts from source drafts, updates the same segment as speech continues, and aligns with the final source text. Choose local or online translation, or turn it off.
- **Make captions comfortable to read:** move and resize the floating window; adjust fonts, colors, and background opacity, or use a preset. Enable mouse passthrough to operate the window underneath; restore mouse operation from Settings or the tray.
- **Use custom parameters:** choose recommended values or enter numbers within the allowed ranges. Recognition keeps the committed value while you type a draft.
- **Keep useful text:** review the latest 64 segments, copy them, or export a TXT file.
- **Keep your screen clear:** closing or minimizing Settings leaves captions running. Restore Settings from the Windows tray icon.

![Main window with audio, language, model, translation, and start controls](docs/images/overview-2.0.png)

*This is a 2.0 layout example captured with separate settings before recognition starts. Its final-only translation hint belongs to that older version; 2.1 updates translations while source drafts are still changing. Device names depend on your audio hardware.*

## Which file should I download?

**[Open the latest release →](https://github.com/Cormor/AudioSubtitleOverlay/releases/latest)** and expand **Assets**:

| File | What it is for |
|---|---|
| **`AudioSubtitleOverlay_Portable_v1.4.1.zip`** | **The app. Download this first.** For 64-bit Windows 10/11 PCs. |
| `AudioSubtitleOverlay_tiny_model_v1.4.1.zip` | Optional Whisper tiny model, for users who manually select it and want to prepare it offline. |
| `SHA256SUMS.txt` | Checksums for checking download integrity. |

The automatic **Source code (zip/tar.gz)** downloads contain source, not the ready-to-run app. The optional tiny package does **not** contain the default SenseVoice model; that model is prepared separately on first use.

## First-time setup

### 1. Extract and open

Right-click the portable ZIP and choose **Extract All**. Open the extracted `AudioSubtitleOverlay` folder and run **`AudioSubtitleOverlay.exe`**. Keep the `_internal` folder alongside it; it contains files the app needs.

### 2. Choose the sound to transcribe

On **实时字幕** (live captions), set:

| Chinese label | Choose this |
|---|---|
| **音源** — audio source | A **系统播放** (system playback) speaker/headphone device for PC sound, or a **麦克风** (microphone) for your voice. |
| **声音语言** — spoken language | The language being spoken, or **自动检测** (automatic detection). |
| **模型** — model | Start with **SenseVoice · CPU 五语**. It runs on CPU and supports Mandarin Chinese, Cantonese, English, Japanese, and Korean. No dedicated GPU is needed. |
| **显示译文** — show translation | Enable it and choose the output language, or disable it for original captions only. Matching source/target languages skip translation. |

For more languages or NVIDIA GPU options, see the [model guide](docs/technical-guide.en.md#models-and-devices).

### 3. Click 开始字幕 — Start captions

On first use, the app downloads missing models for your selection. Local translation also needs models for the selected language pair. Wait for setup to finish, then play content with speech. Captions appear in the floating window; the main window shows recent text and input level.

Local recognition works offline after its model is ready. **MyMemory online translation**, including fallback to online translation when enabled, sends the text to that service.

## Follow translations as speech continues

Enable **显示译文** (show translation) and choose the target language and translation mode. **草稿** marks a translation matching the current source draft; **更新中** means newer source text is waiting for an updated translation. The final translation is paired with the completed source segment.

Translation settings can change while recognition continues on the selected audio source. Online modes may send revised segment snapshots to MyMemory; local-only mode processes text on this PC. Matching source and target languages skip translation.

## Adjust the appearance

Open **字幕外观** (caption appearance). Try **深色高对比** (dark high contrast), **浅色清晰** (light), or **透明描边** (transparent with outlines), then adjust font sizes and opacity. Drag the overlay to move it, or drag its lower-right corner to resize. **找回字幕窗** brings the overlay back onto the screen.

![Appearance settings with presets, font sizes, colors, opacity, and restore controls](docs/images/appearance-2.0.png)

*This image shows the 2.0 appearance page. Version 2.2 adds a mouse-operation card while retaining personal caption styles.*

On **高级设置** (advanced settings), edit the numeric field and press Enter or leave it to apply a valid value; Esc restores the committed value. The arrow lists recommendations. See the [input ranges and engine-specific behavior](docs/controls-input-2.2.0.md).

Enable **让鼠标穿过字幕窗** to pass all overlay mouse input to the window underneath. Choose **恢复鼠标操作** in Settings or the tray to restore it. The restore button remains available when the native state cannot be confirmed.

## Common questions

**No captions after starting?** Check that the content contains speech and watch the input meter. If there is no input, choose the playback device you are actually using. Beta automatic app tracking has OS and compatibility limits; use an explicit system-playback device while troubleshooting.

**Captions are slow or contain mistakes?** Start with the default model. If you use a large Whisper model while the PC is busy, try a smaller model or a suitable GPU option. Live drafts may change; names, accents, and background audio can still cause errors.

**Where did Settings go?** Look in the Windows notification area, including the hidden-icons menu. Click the Shengmu icon to restore Settings. With Settings focused, `Ctrl+Enter` starts/stops and `Ctrl+Q` quits. The tray's right-click menu can also quit.

**Will an update keep my settings?** Settings and models are stored in your user app-data directory. Extract the new version and run its executable; exit the old version to avoid running two copies.

See the **[technical guide](docs/technical-guide.en.md)** for troubleshooting, model details, offline preparation, and development instructions.

## License

The project's own code uses the [Non-Commercial Source-Available License 1.0](LICENSE). Non-commercial use is allowed; commercial use requires prior authorization. Modification, integration, repackaging, and redistribution must meet the license's corresponding-source and authorization terms. Third-party libraries, models, and runtimes have their own terms: see [third-party notices](THIRD_PARTY_NOTICES.md) and [commercial licensing](COMMERCIAL-LICENSE.md).


In 2.0, choose models and compute devices on **模型与资源** (Models and resources). The main window supports system, dark gold and light gold themes; overlay preferences remain independent. **帮助与诊断** exports a local diagnostic report without recordings or transcript text. Cantonese recognition retains its language identity while written text uses the Chinese translation model; colloquial translation quality remains approximate.
