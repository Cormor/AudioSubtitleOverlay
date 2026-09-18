# AudioSubtitleOverlay {{VERSION}}

Windows 系统音频识别与双语字幕悬浮窗。

AudioSubtitleOverlay is a Windows desktop overlay for local speech recognition and bilingual subtitles.

## 下载与运行 | Download and run

1. 下载并解压 `AudioSubtitleOverlay_Portable_20260918.zip`。
2. 保留完整的 `AudioSubtitleOverlay` 文件夹及其中的 `_internal` 文件夹。
3. 需要离线携带 tiny 模型时，将 `AudioSubtitleOverlay_tiny_model_20260918.zip` 解压到同一父目录。
4. 双击 `AudioSubtitleOverlay\AudioSubtitleOverlay.exe`。

主程序包约 106 MiB，不要求目标电脑预装 Python。主程序不内置模型；联网时应用会按需准备模型，也可以使用 tiny 模型包预先准备默认模型。GPU 模式首次使用时可能需要额外准备运行库，CPU 模式不需要该运行库。

The main package is about 106 MiB and does not require Python on the target computer. It does not include a speech model; the application can prepare a missing model when network access is available, or the optional tiny-model package can be used for offline distribution. GPU mode may prepare additional runtime components on first use.

## 发布文件 | Assets

- `AudioSubtitleOverlay_Portable_20260918.zip`：主程序，约 106 MiB。
- `AudioSubtitleOverlay_tiny_model_20260918.zip`：可选 tiny 模型包，约 67 MiB。
- `SHA256SUMS.txt`：发布文件校验值。

[项目主页与源码](https://github.com/Cormor/AudioSubtitleOverlay) · [截图](https://raw.githubusercontent.com/Cormor/AudioSubtitleOverlay/main/docs/screenshots/main-window.png)

## 许可证 | License

项目自有代码采用 `AudioSubtitleOverlay Non-Commercial Source-Available License 1.0`。非商业使用默认允许；修改、集成、重新打包或再分发，必须公开完整对应源码和修改说明，或事先取得书面授权；任何商业使用均须事先取得商业授权。第三方组件适用各自的许可条款。

The original code is provided under the `AudioSubtitleOverlay Non-Commercial Source-Available License 1.0`. Non-commercial use is allowed by default. Modifications, integration, repackaging, and redistribution require public corresponding source with change notes or prior written authorization. All commercial use requires prior written authorization. Third-party components remain under their own terms.

完整条款：[`LICENSE`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/LICENSE) · 商业授权：[`COMMERCIAL-LICENSE.md`](https://github.com/Cormor/AudioSubtitleOverlay/blob/main/COMMERCIAL-LICENSE.md)
