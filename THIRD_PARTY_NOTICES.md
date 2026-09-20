# 第三方组件说明 | Third-party notices

本文件只说明仓库或发布包中使用的第三方组件。第三方组件不受
`LICENSE` 中 AudioSubtitleOverlay 自有代码许可证的重新授权，使用和分发
时必须同时遵守各自的许可证、版权声明和发布条款。

## 已固定或直接使用的组件

- `faster-whisper`：项目地址为
  <https://github.com/SYSTRAN/faster-whisper>；其源码仓库包含自己的 MIT
  许可证文本。
- `Systran/faster-whisper-tiny`：`assets/tiny` 中的固定提交文件来自
  <https://huggingface.co/Systran/faster-whisper-tiny>，当前固定提交为
  `d90ca5fe260221311c53c58e660288d3deb8d356`。模型仓库的许可证和模型卡
  以该来源仓库为准。
- `numpy`、`soundcard`、`requests`、`pyinstaller`、`Pillow`、`opencc`
  以及可选的 `argostranslate`：版本范围位于 requirements 文件；每个包
  的许可证以对应发布版本中携带的 LICENSE、NOTICE 或元数据为准。
- NVIDIA CUDA 12 用户态运行库：由组件清单按固定版本和 SHA256 下载；其
  许可证、版权声明和再分发条件以对应 NVIDIA/PyPI 发布包为准。
- 识别提示词表：`assets/lexicon` 中的日常、游戏和计算机词表来自
  Cassotis Lexicon 与 THUOCL；来源提交、许可证和整理脚本见
  `assets/lexicon/NOTICE.md`。

## 发布时的保留要求

重新打包或发布包含第三方组件的版本时，不得删除第三方许可证和版权声明。
如果某个发布包没有携带对应的上游许可证文本，应在发布包中补充，或在发布
说明中提供稳定的上游许可证链接。

AudioSubtitleOverlay 的自有代码、第三方库、语音模型和 GPU 运行库属于
不同的版权和许可证范围；本文件不把它们合并为同一个许可证。
